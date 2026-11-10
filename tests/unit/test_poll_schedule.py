# ruff: noqa: E501
"""When is a subscription due? The pure decision behind the poller (issue #79)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ytp import poll_schedule
from ytp.poll_schedule import Decision, Schedule, decide, effective, slots_around

BERLIN = ZoneInfo("Europe/Berlin")
KEY = "UCSMOQeBJ2RAnuFungnQOxLg"


def at(hour: int, minute: int = 0, day: int = 10, month: int = 11, zone=BERLIN) -> datetime:
    """A moment on the clock of the server's zone (2026), as the UTC instant it is."""
    return datetime(2026, month, day, hour, minute, tzinfo=zone).astimezone(UTC)


def run(schedule, now, last=None, error=None, zone=BERLIN) -> Decision:
    return decide(
        now=now, last_checked=last, last_error=error, schedule=schedule, key=KEY, zone=zone
    )


def local_slots(schedule, now, zone=BERLIN) -> list[str]:
    """The due times around `now` as local "day HH:MM", for reading in assertions."""
    return [s.astimezone(zone).strftime("%d %H:%M") for s in slots_around(now, schedule, zone)]


# --- the limits -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "value", "ok"),
    [
        ("channel", 0, True), ("channel", 15, True), ("channel", 1440, True), ("channel", 14, False),
        ("channel", 1441, False), ("channel", -1, False), ("channel", 60.0, False),
        ("channel", "60", False), ("channel", True, False), ("channel", None, False),
        ("podcast", 0, True), ("podcast", 30, True), ("podcast", 1440, True), ("podcast", 29, False),
        ("podcast", 15, False), ("podcast", 1441, False), ("podcast", False, False),
    ],
)  # fmt: skip
def test_the_interval_is_paused_or_in_the_range_of_its_kind(kind, value, ok):
    assert poll_schedule.minutes_valid(kind, value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [("00:00", True), ("06:00", True), ("23:59", True), ("24:00", False), ("6:00", False),
     ("06:60", False), ("06:00:00", False), ("0600", False), ("", False), (600, False),
     (None, False), ("06:00\n", False), (" 06:00", False)],
)  # fmt: skip
def test_a_start_time_is_hh_mm_on_a_24_hour_clock(value, ok):
    assert poll_schedule.at_valid(value) is ok


def test_check_poll_fields_validates_for_the_kind_and_keeps_none_and_empty():
    assert poll_schedule.check_poll_fields("channel", {"poll_minutes": None, "poll_at": ""}) == {
        "poll_minutes": None,
        "poll_at": "",
    }
    assert poll_schedule.check_poll_fields("podcast", {"poll_minutes": 30}) == {"poll_minutes": 30}
    assert poll_schedule.check_poll_fields("channel", {"max_height": 720}) == {}
    with pytest.raises(ValueError, match="between 30 and 1440"):
        poll_schedule.check_poll_fields("podcast", {"poll_minutes": 15})
    with pytest.raises(ValueError, match="time of day"):
        poll_schedule.check_poll_fields("channel", {"poll_at": "25:00"})


def test_own_values_override_the_server_default_and_none_follows_it():
    assert effective(None, None, 60, "06:00") == Schedule(60, "06:00")
    assert effective(120, None, 60, "06:00") == Schedule(120, "06:00")
    assert effective(None, "18:30", 60, None) == Schedule(60, "18:30")
    assert effective(0, "18:30", 60, "06:00") == Schedule(0, "18:30"), "paused is its own value"
    assert effective(None, "", 60, "06:00") == Schedule(60, None), "'' = none, not the server's"


# --- without a start time: as before -----------------------------------------------------------------


def test_a_paused_subscription_is_never_due_and_ignores_the_start_time():
    for schedule in (Schedule(0), Schedule(0, "06:00")):
        assert run(schedule, at(12)) == Decision(False, None)
        assert run(schedule, at(12), last=at(1)) == Decision(False, None)
        assert run(schedule, at(12), last=at(1), error="down") == Decision(False, None)


def test_a_subscription_that_was_never_checked_is_due_at_once():
    now = at(12)
    assert run(Schedule(60), now) == Decision(True, now)
    assert run(Schedule(1440, "06:00"), now) == Decision(True, now), (
        "a start time does not delay it"
    )


def test_without_a_start_time_it_is_due_when_the_interval_has_passed():
    last = at(10)
    early = run(Schedule(60), last + timedelta(minutes=47), last=last)  # under 0.8 * 60
    assert not early.due
    assert early.next_due is not None
    assert last + timedelta(minutes=48) <= early.next_due <= last + timedelta(minutes=72)
    assert run(Schedule(60), last + timedelta(minutes=73), last=last).due, "over 1.2 * 60"
    assert run(Schedule(60), early.next_due, last=last).due
    assert not run(Schedule(60), early.next_due - timedelta(seconds=1), last=last).due


def test_the_spread_factor_is_stable_and_differs_between_subscriptions():
    base = poll_schedule.spread_interval(KEY, None, 60)
    assert base == poll_schedule.spread_interval(KEY, None, 60)
    keys = [f"UC{i:022d}" for i in range(30)]
    factors = {poll_schedule.spread_interval(k, None, 100).total_seconds() / 6000 for k in keys}
    assert len(factors) > 10
    assert all(0.8 <= f <= 1.2 for f in factors)


@pytest.mark.parametrize("start", [None, "06:00"])
def test_after_an_error_it_is_tried_again_after_ten_minutes(start):
    schedule = Schedule(1440, start)
    last = at(10)
    early = run(schedule, last + timedelta(minutes=9), last=last, error="down")
    assert early == Decision(False, last + timedelta(minutes=poll_schedule.RETRY_MINUTES))
    assert run(schedule, last + timedelta(minutes=10), last=last, error="down").due


def test_the_retry_is_never_longer_than_the_interval():
    last = at(10)
    assert run(Schedule(15), last + timedelta(minutes=10), last=last, error="x").due
    assert poll_schedule.spread_interval(KEY, "x", 15) == timedelta(minutes=10)
    assert poll_schedule.spread_interval(KEY, "x", 1440) == timedelta(minutes=10)


# --- with a start time ----------------------------------------------------------------------------------


def test_every_6_hours_from_06_00_is_06_12_18_and_00():
    schedule = Schedule(360, "06:00")
    assert local_slots(schedule, at(12, day=10)) == [
        "09 06:00", "09 12:00", "09 18:00", "10 00:00",
        "10 06:00", "10 12:00", "10 18:00", "11 00:00",
        "11 06:00", "11 12:00", "11 18:00", "12 00:00",
    ]  # fmt: skip


def test_every_24_hours_from_18_00_is_once_a_day():
    schedule = Schedule(1440, "18:00")
    assert local_slots(schedule, at(12)) == ["09 18:00", "10 18:00", "11 18:00"]
    last = at(18, 0, day=9) + timedelta(seconds=20)  # checked right after yesterday's 18:00
    before = run(schedule, at(17, 59), last=last)
    assert before == Decision(False, at(18))
    assert run(schedule, at(18), last=last) == Decision(True, at(18))
    assert run(schedule, at(23), last=last) == Decision(True, at(18)), "still due since 18:00"


def test_an_interval_that_does_not_divide_24_hours_restarts_at_the_start_time_each_day():
    schedule = Schedule(300, "06:00")  # every 5 h from 06:00
    assert local_slots(schedule, at(12)) == [
        "09 06:00", "09 11:00", "09 16:00", "09 21:00", "10 02:00",
        "10 06:00", "10 11:00", "10 16:00", "10 21:00", "11 02:00",
        "11 06:00", "11 11:00", "11 16:00", "11 21:00", "12 02:00",
    ]  # fmt: skip


def test_the_due_time_is_the_latest_one_that_passed_since_the_last_check():
    schedule = Schedule(360, "06:00")
    last = at(6, 1)
    assert run(schedule, at(11, 59), last=last) == Decision(False, at(12))
    assert run(schedule, at(12, 0), last=last) == Decision(True, at(12))
    # the app was down over 12:00 and 18:00: due once, since the latest of them
    assert run(schedule, at(19, 30), last=last) == Decision(True, at(18))


def test_a_check_just_after_a_due_time_waits_for_the_next_one():
    schedule = Schedule(360, "06:00")
    last = at(12, 0, day=10) + timedelta(seconds=3)
    assert run(schedule, at(17, 59), last=last) == Decision(False, at(18))
    assert run(schedule, at(18), last=last).due


def test_the_times_cross_midnight():
    schedule = Schedule(360, "06:00")
    last = at(18, 2, day=10)
    assert run(schedule, at(23, 59), last=last) == Decision(False, at(0, 0, day=11))
    assert run(schedule, at(0, 0, day=11), last=last) == Decision(True, at(0, 0, day=11))
    # a start time late in the evening: yesterday's cycle is still the one running after midnight
    late = Schedule(240, "22:00")  # 22:00, 02:00, 06:00, 10:00, 14:00, 18:00
    assert local_slots(late, at(1, day=10)) == [
        "09 22:00", "10 02:00", "10 06:00", "10 10:00", "10 14:00", "10 18:00",
        "10 22:00", "11 02:00", "11 06:00", "11 10:00", "11 14:00", "11 18:00",
        "11 22:00", "12 02:00", "12 06:00", "12 10:00", "12 14:00", "12 18:00",
    ]  # fmt: skip
    assert run(late, at(1, 59), last=at(22, 1, day=9)) == Decision(False, at(2, 0))
    assert run(late, at(2, 0), last=at(22, 1, day=9)) == Decision(True, at(2, 0))


def test_a_long_idle_subscription_is_due_since_its_latest_time():
    assert run(Schedule(360, "06:00"), at(12, 30), last=at(12, 30) - timedelta(days=9)) == Decision(
        True, at(12)
    )


def test_the_clock_of_a_daylight_saving_change_day_keeps_the_same_times():
    schedule = Schedule(360, "06:00")
    # 2026-03-29: Europe/Berlin jumps from 02:00 to 03:00 (the day has 23 hours)
    spring = local_slots(schedule, at(12, day=29, month=3))
    assert spring == [
        "28 06:00", "28 12:00", "28 18:00", "29 00:00",
        "29 06:00", "29 12:00", "29 18:00", "30 00:00",
        "30 06:00", "30 12:00", "30 18:00", "31 00:00",
    ]  # fmt: skip
    # 2026-10-25: it falls back from 03:00 to 02:00 (25 hours)
    autumn = local_slots(schedule, at(12, day=25, month=10))
    assert autumn == [
        "24 06:00", "24 12:00", "24 18:00", "25 00:00",
        "25 06:00", "25 12:00", "25 18:00", "26 00:00",
        "26 06:00", "26 12:00", "26 18:00", "27 00:00",
    ]  # fmt: skip
    # and "due" follows the clock too: 06:00 on the day of the change is 04:00 UTC
    last = at(0, 1, day=29, month=3)
    assert run(schedule, at(5, 59, day=29, month=3), last=last).due is False
    assert run(schedule, at(6, 0, day=29, month=3), last=last) == Decision(
        True, at(6, 0, day=29, month=3)
    )
    assert at(6, 0, day=29, month=3) == datetime(2026, 3, 29, 4, 0, tzinfo=UTC)


def test_a_time_that_does_not_exist_or_exists_twice_fires_once():
    nonexistent = slots_around(at(12, day=29, month=3), Schedule(1440, "02:30"), BERLIN)
    on_the_day = [s for s in nonexistent if s.date() == datetime(2026, 3, 29).date()]
    assert on_the_day == [datetime(2026, 3, 29, 1, 30, tzinfo=UTC)], "taken as 03:30 summer time"
    twice = slots_around(at(12, day=25, month=10), Schedule(1440, "02:30"), BERLIN)
    on_the_day = [s for s in twice if s.date() == datetime(2026, 10, 25).date()]
    assert on_the_day == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)], "the first 02:30"
    assert len(nonexistent) == len(twice) == 3


def test_utc_has_no_changes_and_a_fixed_zone_works_too():
    utc = Schedule(720, "06:00")
    assert local_slots(utc, at(12, zone=UTC), zone=UTC)[:4] == [
        "09 06:00",
        "09 18:00",
        "10 06:00",
        "10 18:00",
    ]
    assert run(utc, at(18, zone=UTC), last=at(6, 1, zone=UTC), zone=UTC).due
    fixed = datetime.now().astimezone().tzinfo
    assert run(utc, at(12, zone=UTC), last=at(6, 1, zone=UTC), zone=fixed) is not None


def test_a_start_time_with_a_15_minute_interval_has_96_times_a_day():
    schedule = Schedule(15, "00:00")
    day = [s for s in slots_around(at(12), schedule, BERLIN) if s.astimezone(BERLIN).day == 10]
    assert len(day) == 96


# --- the zone -------------------------------------------------------------------------------------------


def test_the_server_zone_comes_from_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    zone = poll_schedule.local_zone()
    assert poll_schedule.zone_name(zone) == "Europe/Berlin"
    monkeypatch.setenv("TZ", "Not/AZone")
    assert poll_schedule.local_zone() is not None, (
        "an unknown name falls back to the clock's offset"
    )
