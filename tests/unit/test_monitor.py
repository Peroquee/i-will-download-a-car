"""The monitor that fills the alert log (issue #38): VPN changes, failed jobs, feed errors, disk."""

from __future__ import annotations

import json
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ytp import alertlog, jobs, subscriptions
from ytp.db import Database
from ytp.monitor import (
    DISK_ERROR_PERCENT,
    KEY_JOBS,
    SILENT_POLLS,
    AlertMonitor,
    disk_summary,
    vpn_condition,
)
from ytp.scheduler import get_state, set_state

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


class FakeWatchdog:
    def __init__(self, **status):
        self.status = {"state": "ok", **status}

    def set(self, **status):
        self.status = status

    def fetch_status(self):
        return dict(self.status)


Usage = namedtuple("Usage", "total used free")


def monitor(db, watchdog=None, free_percent=50.0, **kw):
    usage = {"free": free_percent}
    m = AlertMonitor(
        db,
        watchdog,
        Path("/data"),
        now=lambda: NOW,
        disk_usage=lambda _p: Usage(1000, 1000 - usage["free"] * 10, usage["free"] * 10),
        **kw,
    )
    m.usage = usage
    return m


def kinds(db):
    return [a.kind for a in reversed(alertlog.list_alerts(db, limit=100))]


# --- the VPN --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"state": "ok"}, "ok"),
        ({"state": "ok", "egress": "mismatch"}, "mismatch"),
        ({"state": "unknown", "egress": "mismatch"}, "mismatch"),
        ({"state": "down", "egress": "mismatch"}, "down"),
        ({"state": "blind"}, "blind"),
        ({"state": "starting"}, "starting"),
        ({"state": "weird"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_the_condition_follows_the_vpn_bar(status, expected):
    assert vpn_condition(status) == expected


def history(db):
    return [(e["condition"], e["paused"]) for e in reversed(alertlog.vpn_history(db))]


def test_a_working_vpn_writes_a_history_row_but_no_alert(db):
    m = monitor(db, FakeWatchdog())
    m.run_once()
    m.run_once()
    assert history(db) == [("ok", False)]
    assert kinds(db) == []


def test_down_alerts_once_and_recovery_alerts_once(db):
    dog = FakeWatchdog()
    m = monitor(db, dog)
    m.run_once()
    dog.set(state="down", paused=True, reason="gluetun stopped")
    m.run_once()
    m.run_once()
    dog.set(state="ok")
    m.run_once()
    m.run_once()
    assert kinds(db) == ["vpn_down", "vpn_recovered"]
    assert history(db) == [("ok", False), ("down", True), ("ok", False)]
    down = alertlog.list_alerts(db)[1]
    assert down.severity == "error"
    assert down.params == {"paused": True}


def test_the_other_bad_conditions_have_their_own_alerts(db):
    dog = FakeWatchdog()
    m = monitor(db, dog)
    m.run_once()
    for status, kind in (
        ({"state": "blind"}, "vpn_blind"),
        ({"state": "ok", "egress": "mismatch"}, "egress_mismatch"),
    ):
        dog.set(**status)
        m.run_once()
        assert kinds(db)[-1] == kind


def test_starting_never_alerts_and_does_not_count_as_recovery(db):
    dog = FakeWatchdog()
    m = monitor(db, dog)
    m.run_once()
    dog.set(state="starting")
    m.run_once()
    dog.set(state="ok")
    m.run_once()
    assert kinds(db) == []
    assert history(db) == [("ok", False), ("starting", False), ("ok", False)]


def test_a_silent_watchdog_alerts_only_after_several_polls_and_recovery_follows(db):
    dog = FakeWatchdog()
    m = monitor(db, dog)
    m.run_once()
    dog.set(state="unknown")
    for _ in range(SILENT_POLLS - 1):
        m.run_once()
    assert kinds(db) == []
    m.run_once()
    assert kinds(db) == ["watchdog_silent"]
    dog.set(state="ok")
    m.run_once()
    assert kinds(db) == ["watchdog_silent", "vpn_recovered"]


def test_one_missed_poll_in_between_is_forgotten(db):
    dog = FakeWatchdog()
    m = monitor(db, dog)
    m.run_once()
    dog.set(state="unknown")
    m.run_once()
    dog.set(state="ok")
    m.run_once()
    dog.set(state="unknown")
    for _ in range(SILENT_POLLS - 1):
        m.run_once()
    assert kinds(db) == [], "the streak starts again after an answer"


def test_a_restart_of_the_app_does_not_repeat_the_alert(db):
    dog = FakeWatchdog(state="down", paused=True)
    monitor(db, dog).run_once()
    monitor(db, dog).run_once()  # a new process, the same state
    assert kinds(db) == ["vpn_down"]


def test_a_down_vpn_at_the_first_look_is_announced(db):
    monitor(db, FakeWatchdog(state="down")).run_once()
    assert kinds(db) == ["vpn_down"]


def test_pausing_while_down_is_history_not_a_second_alert(db):
    dog = FakeWatchdog(state="down", paused=False)
    m = monitor(db, dog)
    m.run_once()
    dog.set(state="down", paused=True)
    m.run_once()
    assert kinds(db) == ["vpn_down"]
    assert history(db) == [("down", False), ("down", True)]


def test_without_a_watchdog_nothing_happens(db):
    monitor(db, None).run_once()
    assert kinds(db) == []


def test_vpn_texts_hold_no_address_even_if_the_watchdog_sends_one(db):
    monitor(db, FakeWatchdog(state="down", reason="lost 84.150.20.30 and 2003:abcd::1")).run_once()
    event = alertlog.vpn_history(db)[0]
    assert "84.150" not in event["reason"]
    assert "2003" not in event["reason"]


# --- failed jobs ----------------------------------------------------------------------------


def fail_job(
    db,
    external_id,
    reason,
    when,
    job_type="magnet",
    metadata='{"type": "movies", "title": "X", "year": 2000}',
):
    job, _ = jobs.submit_job(db, job_type, external_id, metadata, "magnet:?xt=urn:btih:secret")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (reason, when, job.id),
        )
    return job


def stamp(delta_minutes: float) -> str:
    return (NOW + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_the_first_look_does_not_announce_old_failures(db):
    fail_job(db, "a" * 40, "old", stamp(-600))
    m = monitor(db)
    m.run_once()
    assert kinds(db) == []
    assert get_state(db, KEY_JOBS) is not None


def test_a_job_that_fails_later_is_announced_once_without_its_magnet_link(db):
    m = monitor(db)
    m.run_once()
    job = fail_job(db, "a" * 40, "no sources for 7 days: x", stamp(5))
    m.run_once()
    m.run_once()
    assert kinds(db) == ["job_failed"]
    alert = alertlog.list_alerts(db)[0]
    assert alert.params["job_id"] == job.id
    assert alert.params["reason"] == "no sources for 7 days: x"
    assert "X (2000)" in alert.params["label"]
    assert "secret" not in json.dumps(alert.as_dict())


def test_a_retried_job_that_fails_again_is_announced_again(db):
    m = monitor(db)
    m.run_once()
    job = fail_job(db, "a" * 40, "first", stamp(5))
    m.run_once()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = 'second', updated_at = ? WHERE id = ?",
            (stamp(30), job.id),
        )
    m.run_once()
    assert kinds(db) == ["job_failed", "job_failed"]


def test_youtube_and_podcast_failures_are_announced_too(db):
    m = monitor(db)
    m.run_once()
    fail_job(
        db,
        "vid",
        "video is private",
        stamp(1),
        "youtube",
        '{"type": "youtube", "video_id": "vid", "channel": "C", "title": "T"}',
    )
    m.run_once()
    assert alertlog.list_alerts(db)[0].params["label"] == "C - T"


# --- feeds ----------------------------------------------------------------------------------


def sub_with_error(db, error, name="Blender", channel="UC" + "a" * 22):
    sub, _ = subscriptions.add(db, channel, name)
    with db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET last_error = ? WHERE id = ?", (error, sub.id))
    return sub


def test_a_feed_error_is_announced_once_while_it_stays_the_same(db):
    sub_with_error(db, "the feed could not be fetched (is the VPN up?)")
    m = monitor(db)
    m.run_once()
    m.run_once()
    monitor(db).run_once()  # even after a restart
    assert kinds(db) == ["feed_error"]
    alert = alertlog.list_alerts(db)[0]
    assert alert.params["name"] == "Blender"
    assert alert.severity == "warning"


def test_a_different_error_or_a_new_error_after_recovery_is_announced_again(db):
    sub = sub_with_error(db, "first problem")
    m = monitor(db)
    m.run_once()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_error = 'second problem' WHERE id = ?", (sub.id,)
        )
    m.run_once()
    with db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET last_error = NULL WHERE id = ?", (sub.id,))
    m.run_once()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_error = 'second problem' WHERE id = ?", (sub.id,)
        )
    m.run_once()
    assert kinds(db) == ["feed_error"] * 3


def test_podcast_feed_errors_are_announced_with_the_podcast_name(db):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO podcast_subscriptions (feed_url, title, last_error) VALUES (?, ?, ?)",
            ("https://example.org/feed.xml", "My Show", "the feed is not valid XML"),
        )
    monitor(db).run_once()
    alert = alertlog.list_alerts(db)[0]
    assert (alert.kind, alert.params["name"], alert.params["source"]) == (
        "feed_error",
        "My Show",
        "podcast",
    )


# --- disk space -----------------------------------------------------------------------------


def test_disk_space_alerts_on_crossing_not_on_every_run(db):
    m = monitor(db, free_percent=50)
    m.run_once()
    assert kinds(db) == []
    m.usage["free"] = 9
    m.run_once()
    m.run_once()
    assert kinds(db) == ["disk_low"]
    assert alertlog.list_alerts(db)[0].severity == "warning"
    m.usage["free"] = DISK_ERROR_PERCENT - 1
    m.run_once()
    assert alertlog.list_alerts(db)[0].severity == "error"
    m.usage["free"] = 8  # better, but still low: no new alert
    m.run_once()
    assert kinds(db) == ["disk_low", "disk_low"]
    m.usage["free"] = 60
    m.run_once()
    m.usage["free"] = 4
    m.run_once()
    assert kinds(db) == ["disk_low"] * 3


def test_an_unreadable_drive_is_not_an_alert_and_does_not_stop_the_other_checks(db):
    def broken(_path):
        raise OSError("not mounted")

    m = AlertMonitor(
        db, FakeWatchdog(state="down"), Path("/data"), now=lambda: NOW, disk_usage=broken
    )
    m.run_once()
    assert kinds(db) == ["vpn_down"]
    assert disk_summary(Path("/data"), broken) is None


def test_the_disk_summary_has_percent_and_gigabytes():
    summary = disk_summary(Path("/x"), lambda _p: Usage(2_000_000_000_000, 0, 500_000_000_000))
    assert summary == {"free_percent": 25, "free_gb": 500, "total_gb": 2000}


def test_a_failing_check_does_not_hide_the_others(db, caplog):
    m = monitor(db, FakeWatchdog(state="down"))
    m.check_jobs = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    m.run_once()
    assert kinds(db) == ["vpn_down"]
    assert any("alert check failed" in r.getMessage() for r in caplog.records)


def test_state_is_kept_in_app_state_so_it_survives_a_restart(db):
    set_state(db, "alerts.disk", "error")
    m = monitor(db, free_percent=2)
    m.run_once()
    assert kinds(db) == [], "the low disk was announced before the restart"
