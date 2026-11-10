"""When is a subscription due? One pure decision for channels and podcasts (issue #79).

`decide()` takes everything that matters (the subscription's effective settings, when it was last
checked and whether that went wrong, and "now") and answers whether a check is due and since/at
when. It reads no database and no clock, so the poller only has to call it, and a smarter mode
(#80) can extend this one place instead of the poller.

The settings
* `minutes`: the interval. **0 = paused** (never checked by the schedule; "check now" still works).
  Otherwise `MIN_MINUTES[kind]`..`MAX_MINUTES`: 15 for channels, 30 for podcasts (the same limits
  as `SUBSCRIPTION_POLL_MINUTES` / `PODCAST_POLL_MINUTES`).
* `at`: an optional start time of day, "HH:MM" in the server's local time zone (the container's
  `TZ`), the anchor of the interval.

The rules
* **Never checked** (a new subscription): due at once, so the first look happens right away.
* **After a failed check** the channel is tried again after `RETRY_MINUTES` (or the interval, if
  that is shorter), with or without a start time.
* **Without a start time** it is due once the interval has passed since the last check. The
  interval is multiplied by a fixed per-subscription factor in [0.8, 1.2] (derived from `key`), so
  subscriptions drift apart instead of all going at the same moment.
* **With a start time** the due times are fixed clock times: every local day restarts at the start
  time and steps by the interval until the next start time. "Every 6 h from 06:00" is 06:00,
  12:00, 18:00 and 00:00; "every 24 h from 18:00" is once a day at 18:00; "every 5 h from 06:00"
  is 06:00, 11:00, 16:00, 21:00, 02:00 and then 06:00 again (an interval that does not divide
  24 h is cut off at the next start time, so the times of a day never depend on the day before).
  A subscription is due when the latest of these times is later than its last check. The steps
  are wall-clock steps, so on a day with a daylight-saving change the times stay the same on
  the clock (06/12/18/00); a clock time that does not exist that day (02:30 when the clocks jump
  from 02:00 to 03:00) is taken as the hour after the jump, an ambiguous one at its first
  occurrence.
"""

from __future__ import annotations

import hashlib
import os
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path

from .smart_schedule import Pattern, windows

RETRY_MINUTES = 10  # after a failed check, try again this soon (or the interval if shorter)
RETRY_MAX_MINUTES = 120  # ... doubling with every failure in a row, up to this (#80)
MIN_GAP_MINUTES = 5  # no feed is looked at twice within this, whatever the mode (#80)
MAX_MINUTES = 1440
MIN_MINUTES = {"channel": 15, "podcast": 30}
KINDS = tuple(MIN_MINUTES)
MODES = ("smart", "fixed")  # how a subscription is scheduled (#80)

_AT = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")

RANGE_HELP = {
    kind: f"poll_minutes must be 0 (paused) or between {minimum} and {MAX_MINUTES} minutes"
    for kind, minimum in MIN_MINUTES.items()
}
MODE_HELP = "poll_mode must be " + " or ".join(MODES) + " (or null for the server default)"
AT_HELP = 'poll_at must be a time of day like "06:00" (24-hour clock), "" for none, or null'


@dataclass(frozen=True)
class Schedule:
    """The effective settings of one subscription (its own values, else the server's)."""

    minutes: int  # 0 = paused
    at: str | None = None  # "HH:MM", the anchor of the interval; None = none
    mode: str = "fixed"  # "smart": around the learned publishing times (#80); else the interval


@dataclass(frozen=True)
class Smart:
    """What the smart mode needs besides the schedule (#80): the learned pattern, the intensive
    window around each slot (`before` / `after` minutes), the check every `step` minutes inside
    it, the slow `base` interval outside it, and when the newest item was published."""

    pattern: Pattern
    before: int = 10
    after: int = 60
    step: int = 5
    base: int = 360
    last_published: datetime | None = None


@dataclass(frozen=True)
class Decision:
    due: bool
    # When it is (was) due: in the past or now if `due`; None if it never will be (paused).
    next_due: datetime | None
    # What decided: paused | new | gap | retry | window | base | fixed. Not part of the identity.
    phase: str = field(default="fixed", compare=False)


# ------------------------------------------------------------------------------- validation


def minutes_valid(kind: str, value: object) -> bool:
    """0 (paused) or an integer in the kind's range; `True`/`False` are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value == 0 or MIN_MINUTES[kind] <= value <= MAX_MINUTES


def at_valid(value: object) -> bool:
    return isinstance(value, str) and _AT.fullmatch(value) is not None


def parse_at(value: str) -> time:
    match = _AT.fullmatch(value)
    if match is None:
        raise ValueError(AT_HELP)
    return time(int(match.group(1)), int(match.group(2)))


def check_poll_fields(kind: str, data: dict[str, object]) -> dict[str, object]:
    """The `poll_minutes` / `poll_at` entries of a PATCH body, validated for this kind of
    subscription (a ValueError says what is allowed). None = follow the server default; a start
    time of "" = none, even if the server has one."""
    values: dict[str, object] = {}
    if "poll_mode" in data:
        value = data["poll_mode"]
        if value is not None and value not in MODES:
            raise ValueError(MODE_HELP)
        values["poll_mode"] = value
    if "poll_minutes" in data:
        value = data["poll_minutes"]
        if value is not None and not minutes_valid(kind, value):
            raise ValueError(RANGE_HELP[kind])
        values["poll_minutes"] = value
    if "poll_at" in data:
        value = data["poll_at"]
        if value not in (None, "") and not at_valid(value):
            raise ValueError(AT_HELP)
        values["poll_at"] = value
    return values


def effective(
    own_minutes: int | None,
    own_at: str | None,
    default_minutes: int,
    default_at: str | None,
    own_mode: str | None = None,
    default_mode: str = "fixed",
) -> Schedule:
    """A subscription's own value wins; None follows the server's. An own start time of "" means
    "no start time" even when the server has one."""
    minutes = default_minutes if own_minutes is None else own_minutes
    at = default_at if own_at is None else own_at
    return Schedule(minutes, at or None, own_mode or default_mode)


# ---------------------------------------------------------------------------------- the zone


def local_zone() -> tzinfo:
    """The server's time zone: `TZ` (what the compose file sets), else the system's, else the
    offset the clock has right now (no daylight-saving rules known then)."""
    try:
        from zoneinfo import ZoneInfo

        name = os.environ.get("TZ", "").strip().lstrip(":")
        if not name:
            link = Path("/etc/localtime")
            if link.is_symlink():
                target = str(link.resolve())
                name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        if name:
            return ZoneInfo(name)
    except Exception:  # unknown zone name, no tz database: fall through to the plain offset
        pass
    return datetime.now().astimezone().tzinfo or UTC


def zone_name(zone: tzinfo) -> str:
    return getattr(zone, "key", None) or zone.tzname(None) or str(zone)


# --------------------------------------------------------------------------------- the rules


def _fraction(key: str) -> float:
    """A stable number in [0, 1] derived from `key`: what spreads feeds apart."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:2], "big") / 0xFFFF


def retry_wait(streak: int, interval_minutes: int) -> timedelta:
    """How long to wait after a failed check: `RETRY_MINUTES` (or the interval, if shorter) the
    first time, then twice as long for every failure in a row, up to `RETRY_MAX_MINUTES` (#80:
    an unreachable host or a 429 is not asked every ten minutes for hours)."""
    if streak <= 1:
        return timedelta(minutes=min(RETRY_MINUTES, interval_minutes))
    return timedelta(minutes=min(RETRY_MINUTES * 2 ** (streak - 1), RETRY_MAX_MINUTES))


def spread_interval(
    key: str, last_error: str | None, base_minutes: int, streak: int = 1
) -> timedelta:
    """How long to wait after the last check: the base interval times a factor in [0.8, 1.2]
    (stable, derived from `key`), or the retry time after an error."""
    if last_error:
        return retry_wait(streak, base_minutes)
    return timedelta(minutes=base_minutes * (0.8 + 0.4 * _fraction(key)))


def _day_slots(day: date, start: time, minutes: int, zone: tzinfo) -> list[datetime]:
    """The due times of the cycle that begins at `start` on `day` (UTC, ascending)."""
    first = datetime.combine(day, start)
    end = datetime.combine(day + timedelta(days=1), start)
    step = timedelta(minutes=minutes)
    slots = []
    moment = first
    while moment < end:  # wall-clock arithmetic: the same clock times on a daylight-saving day
        slots.append(moment.replace(tzinfo=zone).astimezone(UTC))
        moment += step
    return slots


def slots_around(now: datetime, schedule: Schedule, zone: tzinfo) -> list[datetime]:
    """The due times of yesterday's, today's and tomorrow's cycle: enough to find the latest one
    at or before `now` and the first one after it."""
    assert schedule.at is not None
    start = parse_at(schedule.at)
    today = now.astimezone(zone).date()
    slots: list[datetime] = []
    for offset in (-1, 0, 1):
        slots += _day_slots(today + timedelta(days=offset), start, schedule.minutes, zone)
    return sorted(set(slots))


def decide(
    *,
    now: datetime,
    last_checked: datetime | None,
    last_error: str | None,
    schedule: Schedule,
    key: str,
    zone: tzinfo,
    smart: Smart | None = None,
    error_streak: int = 1,
) -> Decision:
    """Is this subscription due now, and since/at when? `key` is the stable identity that spreads
    the intervals (the channel id or the feed address).

    Order of the rules: paused never; a new subscription at once; no feed twice within
    `MIN_GAP_MINUTES`; after an error the retry wait (`retry_wait`); then, in the smart mode with
    a regular pattern, around the learned times (`_decide_smart`), else the interval of #79."""
    if schedule.minutes == 0:
        return Decision(False, None, "paused")
    if last_checked is None:
        return Decision(True, now, "new")
    gap = last_checked + timedelta(minutes=MIN_GAP_MINUTES)
    if now < gap:
        return Decision(False, gap, "gap")
    if last_error:
        due_at = last_checked + retry_wait(error_streak, schedule.minutes)
        return Decision(due_at <= now, due_at, "retry")
    if schedule.mode == "smart" and smart is not None and smart.pattern.regular:
        return _decide_smart(now, last_checked, smart, key, zone)
    if schedule.at is None:
        due_at = last_checked + spread_interval(key, None, schedule.minutes)
        return Decision(due_at <= now, due_at)
    slots = slots_around(now, schedule, zone)
    after_check = bisect_right(slots, last_checked)
    if after_check < len(slots) and slots[after_check] <= now:
        # a due time has passed since the last check: it is due since the latest such time
        return Decision(True, slots[bisect_right(slots, now) - 1])
    if after_check < len(slots):
        return Decision(False, slots[after_check])
    return Decision(False, None)  # unreachable with three days of slots; stay quiet, not due


def _decide_smart(
    now: datetime, last_checked: datetime, smart: Smart, key: str, zone: tzinfo
) -> Decision:
    """Around the learned publishing times: every `step` minutes inside a window (from `before`
    minutes before the slot's time to `after` after it), the slow `base` interval outside, and a
    check as soon as a window begins. Once the item expected in a window was found (something was
    published since the window began) the window is done: the rest of it is checked at the base
    interval. Spread by a stable factor per feed so feeds do not all fire at the same second: the
    step is stretched by up to a quarter (never below the step), the base by 0.8 to 1.2."""
    found = windows(smart.pattern, now, zone, smart.before, smart.after)
    current = next((w for w in found if w.start <= now <= w.end), None)
    upcoming = next((w for w in found if w.start > now), None)
    published = smart.last_published
    if current is not None and not (published is not None and published >= current.start):
        if last_checked < current.start:
            return Decision(True, current.start, "window")  # the window began: look now
        step = timedelta(minutes=smart.step * (1.0 + 0.25 * _fraction(key)))
        due_at = last_checked + step
        return Decision(due_at <= now, due_at, "window")
    due_at = last_checked + timedelta(minutes=smart.base * (0.8 + 0.4 * _fraction(key)))
    if upcoming is not None:
        due_at = min(due_at, upcoming.start)
    return Decision(due_at <= now, due_at, "base")
