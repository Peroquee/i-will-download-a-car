"""Learning when a channel or podcast usually publishes (issue #80). Pure: no database, no clock.

`learn()` reads the publication times of a subscription and returns a `Pattern`: the weekly slots
(weekday + time of day, in the server's time zone) it publishes in, and how sure that is. The
poller then checks intensively around those slots (`windows()`, used by `poll_schedule.decide`) and
rarely outside them; without a regular pattern it keeps to the interval of #79.

How it learns (stdlib arithmetic only):

* Each time of day is a point on the 24-hour circle. Times that lie within `CLUSTER_GAP` minutes of
  the next one form a **cluster** ("around 17:00"), whatever the weekday.
* A cluster is a slot on weekday D when the subscription published in it on at least `MIN_HITS`
  dates of that weekday (two must be consecutive, three or more must make up at least `MIN_RATE`
  of the dates of that weekday since the first hit, the newest dates weighing more:
  `HALF_LIFE_DAYS`), and at least one of the last `RECENT_DATES` dates of that weekday was a hit.
  So "Tuesday and Friday at 17:00" is two slots; a channel that publishes every weekday has five; a
  schedule that changed drops the old slot as soon as it is missed a few times and adds the new one
  as soon as it repeats.
* A slot's time is the weighted median of its cluster. The **confidence** is the weighted share of
  all publications that fall inside one of the slots' windows (`LEARN_BEFORE` / `LEARN_AFTER`).
  The pattern is **regular** (usable) with at least `MIN_EVENTS` publications, one slot and a
  confidence of at least `MIN_CONFIDENCE`; otherwise it is not, and the caller falls back.

Few publications or irregular ones give no slots: the fallback is the normal answer then, not an
error. All times of day are wall-clock times of the zone (a 17:00 upload stays 17:00 across a
daylight-saving change).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

HALF_LIFE_DAYS = 56.0
MIN_EVENTS = 6  # fewer publications than this: nothing is learned
MIN_HITS = 2  # dates of one weekday a slot needs (see `DAILY_DATES`)
DAILY_DATES = 6  # a time of day published in on this many dates lets two hits per weekday count
MIN_RATE = 0.5  # share of the observed dates of that weekday it must have been published on
RECENT_DATES = 3  # of the newest dates of that weekday at least one was a hit
CLUSTER_GAP = 45  # minutes: neighbours closer than this are one time of day
LEARN_BEFORE = 20  # minutes before / after a slot's time that count as "on schedule"
LEARN_AFTER = 60
MIN_CONFIDENCE = 0.6
MAX_AGE_DAYS = 400  # older publications are forgotten
MAX_EVENTS = 100

DAY = 1440


@dataclass(frozen=True)
class Slot:
    weekday: int  # 0 = Monday ... 6 = Sunday (local)
    minute: int  # minute of the local day, 0..1439
    hits: int  # dates it was published on in this slot


@dataclass(frozen=True)
class Pattern:
    regular: bool  # usable: at least one slot and enough confidence
    slots: tuple[Slot, ...]
    confidence: float  # 0..1: the weighted share of publications on schedule
    events: int  # publications looked at


NO_PATTERN = Pattern(False, (), 0.0, 0)


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC
    end: datetime  # UTC
    expected: datetime  # UTC: the slot's time on that day
    slot: Slot


# ------------------------------------------------------------------------------- learning


def _weight(age_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS)


def _clusters(points: list[tuple[int, float, int]]) -> list[list[tuple[int, float, int]]]:
    """Group `(minute, weight, index)` points on the 24-hour circle (single link)."""
    ordered = sorted(points)
    groups: list[list[tuple[int, float, int]]] = []
    for point in ordered:
        if groups and point[0] - groups[-1][-1][0] <= CLUSTER_GAP:
            groups[-1].append(point)
        else:
            groups.append([point])
    if len(groups) > 1 and groups[0][0][0] + DAY - groups[-1][-1][0] <= CLUSTER_GAP:
        groups[0] = groups.pop() + groups[0]  # the cluster wraps around midnight
    return groups


def _unwrapped(group: list[tuple[int, float, int]]) -> list[tuple[int, float, int]]:
    """The cluster's minutes on one line: a wrapped cluster (23:50 and 00:10) gets 1440 added to
    its early side, so its median is computed where it really is."""
    low, high = min(p[0] for p in group), max(p[0] for p in group)
    if high - low > DAY // 2:
        return [(m + DAY if m < DAY // 2 else m, w, i) for m, w, i in group]
    return list(group)


def _median(group: list[tuple[int, float, int]]) -> int:
    """The weighted median minute of a cluster (0..1439)."""
    line = sorted(_unwrapped(group))
    half = sum(w for _, w, _ in line) / 2
    seen = 0.0
    for minute, weight, _ in line:
        seen += weight
        if seen >= half:
            return minute % DAY
    return line[-1][0] % DAY


def _inside(minute: int, centre: int) -> bool:
    """Is `minute` within [centre - LEARN_BEFORE, centre + LEARN_AFTER] on the circle?"""
    offset = (minute - centre + DAY // 2) % DAY - DAY // 2
    return -LEARN_BEFORE <= offset <= LEARN_AFTER


def learn(times: Sequence[datetime], zone: tzinfo, now: datetime) -> Pattern:
    """The publishing pattern of a subscription from its publication times (aware datetimes)."""
    horizon = now - timedelta(days=MAX_AGE_DAYS)
    kept = sorted((t for t in times if t >= horizon), reverse=True)[:MAX_EVENTS]
    if len(kept) < MIN_EVENTS:
        return Pattern(False, (), 0.0, len(kept))

    local = [t.astimezone(zone) for t in kept]
    weights = [_weight((now - t).total_seconds() / 86400) for t in kept]
    minutes = [t.hour * 60 + t.minute for t in local]
    dates = [t.date() for t in local]
    last = max(dates)

    def opportunities(start: date) -> list[date]:
        """Every date of the weekday of `start` from `start` to the newest publication, newest first
        (a slot is judged from the day it first showed up: a schedule that began recently is not
        held against the months before)."""
        found = []
        day = start
        while day <= last:
            found.append(day)
            day += timedelta(days=7)
        return found[::-1]

    def day_weight(day: date) -> float:
        return _weight((now.astimezone(zone).date() - day).days)

    slots: list[Slot] = []
    covered = [False] * len(kept)
    since: date | None = None  # when the pattern began: the oldest first hit of its slots
    for group in _clusters([(minutes[i], weights[i], i) for i in range(len(kept))]):
        cluster_dates = {dates[i] for _, _, i in group}
        for weekday in range(7):
            mine = [point for point in group if dates[point[2]].weekday() == weekday]
            hit_dates = {dates[i] for _, _, i in mine}
            # three dates of a weekday, or two of them in a time of day that is published in on
            # many dates (a daily uploader has only two Tuesdays in a fortnight's feed)
            if len(hit_dates) < MIN_HITS or (
                len(hit_dates) < 3 and len(cluster_dates) < DAILY_DATES
            ):
                continue
            days = opportunities(min(hit_dates))
            rate = sum(day_weight(d) for d in hit_dates) / sum(day_weight(d) for d in days)
            # two hits are a pattern only when they are consecutive: not a miss in between
            needed = MIN_RATE if len(hit_dates) >= 3 else 1.0
            if rate < needed or not any(d in hit_dates for d in days[:RECENT_DATES]):
                continue
            centre = _median(mine)  # per weekday: a cluster can hold Tue 17:00 and Fri 17:30
            slots.append(Slot(weekday, centre, len(hit_dates)))
            since = min(hit_dates) if since is None else min(since, min(hit_dates))
            for minute, _, i in mine:
                if _inside(minute, centre):
                    covered[i] = True
    # Judged on the publications since the pattern began: what came before a changed schedule
    # says nothing about the new one.
    counted = [since is not None and d >= since for d in dates]
    total = sum(w for w, c in zip(weights, counted, strict=True) if c)
    on_time = sum(w for w, c, ok in zip(weights, covered, counted, strict=True) if c and ok)
    confidence = on_time / total if total else 0.0
    slots.sort(key=lambda s: (s.weekday, s.minute))
    regular = bool(slots) and confidence >= MIN_CONFIDENCE
    return Pattern(regular, tuple(slots), round(confidence, 3), len(kept))


# ------------------------------------------------------------------------------- windows


def windows(
    pattern: Pattern, now: datetime, zone: tzinfo, before: int, after: int, days: int = 8
) -> list[Window]:
    """The intensive-check windows around `now`: from yesterday to `days` days ahead, ascending.
    A window runs from `before` minutes before a slot's time to `after` minutes after it."""
    if not pattern.regular:
        return []
    today = now.astimezone(zone).date()
    found: list[Window] = []
    for offset in range(-1, days + 1):
        day = today + timedelta(days=offset)
        for slot in pattern.slots:
            if slot.weekday != day.weekday():
                continue
            wall = datetime.combine(day, time(slot.minute // 60, slot.minute % 60))
            expected = wall.replace(tzinfo=zone).astimezone(UTC)
            found.append(
                Window(
                    expected - timedelta(minutes=before),
                    expected + timedelta(minutes=after),
                    expected,
                    slot,
                )
            )
    return sorted(found, key=lambda w: w.start)
