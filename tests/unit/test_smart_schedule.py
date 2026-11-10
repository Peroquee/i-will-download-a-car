# ruff: noqa: E501
"""Learning a publishing pattern from synthetic histories (issue #80)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ytp import smart_schedule as sm
from ytp.smart_schedule import learn, windows

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=BERLIN).astimezone(UTC)  # a Tuesday
TUE, WED, THU, FRI = 1, 2, 3, 4


def at(day: date, hour: int, minute: int = 0, zone=BERLIN) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)


def weekly(
    weekdays, hour, minute=0, weeks=12, jitter=8, seed=1, end=None, skip=0.0
) -> list[datetime]:
    """One publication per given weekday and week, `weeks` weeks back from `end` (default: NOW)."""
    rng = random.Random(seed)
    last = (end or NOW).astimezone(BERLIN).date()
    found = []
    for back in range(weeks * 7):
        day = last - timedelta(days=back)
        if day.weekday() in weekdays and rng.random() >= skip:
            wobble = rng.randint(-jitter, jitter)
            found.append(at(day, hour, minute) + timedelta(minutes=wobble))
    return found


def slots_of(pattern):
    return [(s.weekday, s.minute) for s in pattern.slots]


def near(minute: int, hour: int, tolerance: int = 15) -> bool:
    return abs(minute - hour * 60) <= tolerance


# --- what it learns ----------------------------------------------------------------------------------


def test_a_weekly_schedule_gives_one_slot_and_a_high_confidence():
    pattern = learn(weekly({TUE}, 17), BERLIN, NOW)
    assert pattern.regular
    assert [s.weekday for s in pattern.slots] == [TUE]
    assert near(pattern.slots[0].minute, 17)
    assert pattern.confidence >= 0.9
    assert pattern.events == 12


def test_two_slots_a_week_at_the_same_time_are_both_found():
    pattern = learn(weekly({TUE, FRI}, 17), BERLIN, NOW)
    assert pattern.regular
    assert [s.weekday for s in pattern.slots] == [TUE, FRI]
    assert all(near(s.minute, 17) for s in pattern.slots)


def test_two_slots_at_different_times_are_both_found():
    history = weekly({TUE}, 17, seed=2) + weekly({FRI}, 10, seed=3)
    pattern = learn(history, BERLIN, NOW)
    assert pattern.regular
    by_day = {s.weekday: s.minute for s in pattern.slots}
    assert set(by_day) == {TUE, FRI}
    assert near(by_day[TUE], 17)
    assert near(by_day[FRI], 10)


def test_a_daily_uploader_gets_a_slot_every_day():
    history = weekly(set(range(7)), 18, weeks=3)
    pattern = learn(history, BERLIN, NOW)
    assert pattern.regular
    assert sorted(s.weekday for s in pattern.slots) == list(range(7))


def test_a_weekday_uploader_gets_no_weekend_slot():
    pattern = learn(weekly({0, 1, 2, 3, 4}, 9, weeks=5), BERLIN, NOW)
    assert pattern.regular
    assert sorted(s.weekday for s in pattern.slots) == [0, 1, 2, 3, 4]


def test_the_fast_start_a_feed_of_15_entries_is_enough():
    """A channel feed lists the last 15 uploads: Tue and Fri for about 7 weeks."""
    history = sorted(weekly({TUE, FRI}, 17, weeks=8), reverse=True)[:15]
    pattern = learn(history, BERLIN, NOW)
    assert pattern.regular
    assert [s.weekday for s in pattern.slots] == [TUE, FRI]


def test_an_occasional_miss_or_off_schedule_upload_does_not_hide_the_pattern():
    history = [*weekly({TUE}, 17, skip=0.25, seed=5), at(date(2026, 10, 14), 3, 40)]
    pattern = learn(history, BERLIN, NOW)
    assert pattern.regular
    assert [s.weekday for s in pattern.slots] == [TUE]
    assert 0.6 <= pattern.confidence < 1.0


# --- what it refuses to learn --------------------------------------------------------------------------


def test_too_few_publications_learn_nothing():
    pattern = learn(weekly({TUE}, 17)[: sm.MIN_EVENTS - 1], BERLIN, NOW)
    assert not pattern.regular
    assert pattern.slots == ()
    assert pattern.events == sm.MIN_EVENTS - 1


def test_irregular_times_learn_nothing():
    rng = random.Random(7)
    history = [
        NOW - timedelta(days=rng.randint(1, 80), minutes=rng.randint(0, 1439)) for _ in range(20)
    ]
    pattern = learn(history, BERLIN, NOW)
    assert not pattern.regular
    assert pattern.confidence < sm.MIN_CONFIDENCE or pattern.slots == ()


def test_a_regular_time_on_random_weekdays_is_not_a_weekly_pattern_unless_it_repeats_per_weekday():
    """Six uploads at 17:00 on six different weekdays of one week: each weekday has one hit."""
    history = [at(date(2026, 11, 2) + timedelta(days=d), 17) for d in range(6)] + [
        at(date(2026, 10, 26) + timedelta(days=d), 17) for d in range(1)
    ]
    assert not learn(history, BERLIN, NOW).regular


def test_nothing_in_nothing_out():
    assert learn([], BERLIN, NOW) == sm.Pattern(False, (), 0.0, 0)


def test_publications_older_than_the_horizon_are_forgotten():
    old = weekly({TUE}, 17, weeks=12, end=NOW - timedelta(days=sm.MAX_AGE_DAYS + 30))
    pattern = learn(old, BERLIN, NOW)
    assert pattern.events == 0
    assert not pattern.regular


# --- a changed schedule ----------------------------------------------------------------------------------


def test_a_changed_schedule_drops_the_old_slot_and_picks_up_the_new_one():
    recent_start = NOW - timedelta(weeks=4)
    old = weekly({TUE}, 17, weeks=20, end=recent_start, seed=1)
    new = weekly({THU}, 9, weeks=4, end=NOW, seed=2)
    pattern = learn(old + new, BERLIN, NOW)
    assert pattern.regular
    by_day = {s.weekday: s.minute for s in pattern.slots}
    assert THU in by_day
    assert near(by_day[THU], 9)
    assert TUE not in by_day, "four Tuesdays in a row without an upload: the old slot is gone"


def test_a_time_change_of_the_same_weekday_follows_the_new_time():
    old = weekly({TUE}, 17, weeks=20, end=NOW - timedelta(weeks=5), seed=1)
    new = weekly({TUE}, 19, weeks=5, end=NOW, seed=2)
    pattern = learn(old + new, BERLIN, NOW)
    tuesdays = [s.minute for s in pattern.slots if s.weekday == TUE]
    assert tuesdays, "still a Tuesday slot"
    assert any(near(m, 19) for m in tuesdays), "at the new time"


# --- time of day is local wall-clock time --------------------------------------------------------------------


def test_an_upload_time_stays_the_same_across_a_daylight_saving_change():
    """Sundays 20:00 Berlin from September to December: 18:00 UTC in summer, 19:00 in winter."""
    sundays = [date(2026, 9, 6) + timedelta(weeks=k) for k in range(14)]
    history = [at(d, 20) for d in sundays]
    assert {t.hour for t in history} == {18, 19}
    pattern = learn(history, BERLIN, datetime(2026, 12, 10, 12, tzinfo=UTC))
    assert pattern.regular
    assert slots_of(pattern) == [(6, 20 * 60)]


def test_the_zone_decides_the_hour():
    history = weekly(set(range(7)), 17, weeks=2)  # all in winter time: 17:00 Berlin = 16:00 UTC
    utc_pattern = learn(history, UTC, NOW)
    assert near(utc_pattern.slots[0].minute, 16, 20), "17:00 Berlin is 16:00 UTC in winter"


def test_uploads_around_midnight_form_one_cluster_and_keep_their_weekday():
    history = [at(date(2026, 11, 3) - timedelta(weeks=k), 23, 55) for k in range(10)]
    pattern = learn(history, BERLIN, NOW)
    assert pattern.regular
    assert slots_of(pattern) == [(TUE, 23 * 60 + 55)]
    mixed = [at(date(2026, 11, 3) - timedelta(weeks=k), 23, 55) for k in range(5)] + [
        at(date(2026, 11, 4) - timedelta(weeks=k), 0, 5) for k in range(5)
    ]  # Tue 23:55 and Wed 00:05: two weekdays, one cluster of times
    got = learn(mixed, BERLIN, NOW)
    assert (TUE, 23 * 60 + 55) in slots_of(got)
    assert (WED, 5) in slots_of(got)


# --- the windows -------------------------------------------------------------------------------------------


def test_windows_run_from_before_to_after_the_slot_in_local_time():
    pattern = learn(weekly({TUE}, 17, jitter=0), BERLIN, NOW)
    found = windows(pattern, NOW, BERLIN, before=10, after=60)
    today = next(
        w for w in found if w.expected.astimezone(BERLIN).date() == NOW.astimezone(BERLIN).date()
    )
    assert today.expected == at(date(2026, 11, 10), 17)
    assert today.start == at(date(2026, 11, 10), 16, 50)
    assert today.end == at(date(2026, 11, 10), 18, 0)
    assert [w.start for w in found] == sorted(w.start for w in found)
    assert {w.expected.astimezone(BERLIN).weekday() for w in found} == {TUE}


def test_windows_follow_the_wall_clock_over_a_daylight_saving_change():
    pattern = sm.Pattern(True, (sm.Slot(6, 20 * 60, 10),), 0.9, 12)  # Sundays 20:00
    before = datetime(2026, 10, 20, 12, tzinfo=UTC)
    found = windows(pattern, before, BERLIN, 10, 60, days=10)
    sunday = next(w for w in found if w.expected.astimezone(BERLIN).date() == date(2026, 10, 25))
    assert sunday.expected.astimezone(BERLIN).hour == 20
    assert sunday.expected == datetime(2026, 10, 25, 19, 0, tzinfo=UTC), (
        "winter time since the night"
    )


def test_an_irregular_pattern_has_no_windows():
    assert windows(sm.NO_PATTERN, NOW, BERLIN, 10, 60) == []
    assert windows(sm.Pattern(False, (sm.Slot(1, 100, 3),), 0.3, 8), NOW, BERLIN, 10, 60) == []


def test_the_confidence_is_between_zero_and_one_and_grows_with_regularity():
    tidy = learn(weekly({TUE}, 17, jitter=2), BERLIN, NOW)
    noisy_history = weekly({TUE}, 17, jitter=2, skip=0.0)
    rng = random.Random(3)
    noisy_history += [
        NOW - timedelta(days=rng.randint(1, 80), minutes=rng.randint(0, 1439)) for _ in range(6)
    ]
    noisy = learn(noisy_history, BERLIN, NOW)
    assert 0 <= noisy.confidence < tidy.confidence <= 1
    assert noisy.regular, "a few strays do not break it"


@pytest.mark.parametrize("weekday", range(7))
def test_every_weekday_can_be_a_slot(weekday):
    pattern = learn(weekly({weekday}, 12), BERLIN, NOW)
    assert [s.weekday for s in pattern.slots] == [weekday]
