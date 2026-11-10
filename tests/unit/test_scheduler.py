"""app/ytp/scheduler.py: lifecycle, no overlap, failures contained, restart, health."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ytp.config import Settings
from ytp.db import Database
from ytp.main import create_app
from ytp.scheduler import (
    HEARTBEAT_JOB_ID,
    HEARTBEAT_SECONDS,
    KEY_LAST_HEARTBEAT,
    KEY_STARTED_AT,
    STALE_AFTER_HEARTBEATS,
    AppScheduler,
    get_state,
    set_state,
)

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database.open(tmp_path / "app.db")
    yield db
    db.close()


def run_for(seconds: float, scheduler: AppScheduler, then=None) -> None:
    """Start the scheduler in an event loop, let it run, stop it (like the app lifespan)."""

    async def main():
        scheduler.start()
        await asyncio.sleep(seconds)
        if then:
            then()
        scheduler.shutdown()

    asyncio.run(main())


def next_run_in(scheduler: AppScheduler, job_id: str) -> timedelta | None:
    """How long until a job runs next (test-only; the app has no use for it)."""
    job = scheduler._scheduler.get_job(job_id)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time - datetime.now(UTC)


# --- app_state ----------------------


def test_app_state_is_a_key_value_store_with_upsert(database):
    assert get_state(database, "k") is None
    set_state(database, "k", "one")
    set_state(database, "k", "two")
    assert get_state(database, "k") == "two"
    assert database.query("SELECT COUNT(*) FROM app_state")[0][0] == 1


def test_the_migration_creates_app_state_without_touching_the_first_tables(database):
    names = {r[0] for r in database.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "subscriptions", "app_state", "schema_migrations"} <= names
    assert [
        r["name"] for r in database.query("SELECT name FROM schema_migrations ORDER BY version")
    ] == [
        "0001_initial.sql",
        "0002_app_state.sql",
        "0003_job_source.sql",
        "0004_subscription_backfill.sql",
        "0005_podcasts.sql",
        "0006_subscription_target.sql",
        "0007_magnet_progress.sql",
        "0008_skipped_videos.sql",
        "0009_alerts.sql",
        "0010_accounts.sql",
        "0011_podcast_backlog.sql",
        "0012_seen_videos.sql",
        "0013_job_dismissals.sql",
        "0014_job_priority.sql",
        "0015_resolution_rights.sql",
        "0016_job_note.sql",
        "0017_poll_settings.sql",
        "0018_indexes_and_constraints.sql",
    ]


# --- the placeholder job ----------------------


def test_the_heartbeat_job_is_registered_and_runs_at_once_then_every_30_seconds(database):
    scheduler = AppScheduler(database)
    seen: dict = {}

    def look():
        seen["jobs"] = scheduler.job_ids()
        seen["heartbeat"] = get_state(database, KEY_LAST_HEARTBEAT)
        seen["next"] = next_run_in(scheduler, HEARTBEAT_JOB_ID)

    run_for(0.3, scheduler, look)
    assert seen["jobs"] == [HEARTBEAT_JOB_ID]
    assert seen["heartbeat"] is not None, "the first run does not wait for the interval"
    assert HEARTBEAT_SECONDS == 30
    assert 25 < seen["next"].total_seconds() <= 30


def test_the_heartbeat_keeps_going(database):
    scheduler = AppScheduler(database, heartbeat_seconds=0.05)
    stamps: list[str | None] = []

    def sample():
        stamps.append(get_state(database, KEY_LAST_HEARTBEAT))

    async def main():
        scheduler.start()
        for _ in range(3):
            await asyncio.sleep(0.12)
            sample()
        scheduler.shutdown()

    asyncio.run(main())
    assert len(set(stamps)) == 3, stamps


def test_start_and_shutdown_are_idempotent(database):
    scheduler = AppScheduler(database)

    async def main():
        scheduler.start()
        scheduler.start()
        assert scheduler.running
        scheduler.shutdown()
        scheduler.shutdown()
        assert not scheduler.running

    asyncio.run(main())


# --- jobs never overlap themselves ----------------------


def test_a_slow_job_is_never_running_twice_and_the_skipped_runs_are_logged(database, caplog):
    scheduler = AppScheduler(database)
    lock = threading.Lock()
    state = {"now": 0, "max": 0, "runs": 0}

    def slow():
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            state["runs"] += 1
        time.sleep(0.35)
        with lock:
            state["now"] -= 1

    async def main():
        scheduler.start()
        scheduler.add_interval_job(slow, job_id="slow", seconds=0.05, run_immediately=True)
        await asyncio.sleep(1.2)
        scheduler.shutdown()

    with caplog.at_level(logging.WARNING, logger="ytp.scheduler"):
        asyncio.run(main())
    assert state["max"] == 1, "overlapping runs of one job"
    assert state["runs"] >= 2
    assert "job skipped: the previous run is still going" in caplog.text


def test_a_backlog_of_due_runs_collapses_into_one(database):
    scheduler = AppScheduler(database)
    runs: list[float] = []

    def job():
        runs.append(time.monotonic())
        time.sleep(0.4)

    async def main():
        scheduler.start()
        scheduler.add_interval_job(job, job_id="j", seconds=0.05, run_immediately=True)
        await asyncio.sleep(0.9)
        scheduler.shutdown()

    asyncio.run(main())
    # ~0.9 s at one run per 0.4 s: a handful, not the 18 that were due.
    assert 2 <= len(runs) <= 4


def test_add_interval_job_sets_the_guarantees_on_every_job(database):
    scheduler = AppScheduler(database)

    async def main():
        scheduler.start()
        scheduler.add_interval_job(lambda: None, job_id="x", seconds=60)
        job = scheduler._scheduler.get_job("x")
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 60
        assert next_run_in(scheduler, "x").total_seconds() > 55, "not run_immediately: waits"
        # Registering again replaces; there is never a second copy.
        scheduler.add_interval_job(lambda: None, job_id="x", seconds=120)
        assert scheduler.job_ids().count("x") == 1
        scheduler.shutdown()

    asyncio.run(main())


# --- failures are contained ----------------------


def test_a_failing_job_is_logged_and_neither_it_nor_the_others_stop(database, caplog):
    scheduler = AppScheduler(database, heartbeat_seconds=0.05)
    calls = {"bad": 0}

    def bad():
        calls["bad"] += 1
        raise RuntimeError("boom from a test job")

    async def main():
        scheduler.start()
        scheduler.add_interval_job(bad, job_id="bad", seconds=0.05, run_immediately=True)
        await asyncio.sleep(0.4)
        scheduler.shutdown()

    with caplog.at_level(logging.ERROR, logger="ytp.scheduler"):
        asyncio.run(main())
    assert calls["bad"] >= 3, "the failing job is scheduled again"
    assert "boom from a test job" in caplog.text
    assert scheduler.check() == "not running"  # (after shutdown) but the heartbeat kept going:
    assert get_state(database, KEY_LAST_HEARTBEAT) is not None


# --- surviving a restart ----------------------


def test_a_new_scheduler_after_a_restart_has_its_jobs_and_remembers_the_last_heartbeat(database):
    first = AppScheduler(database)
    run_for(0.3, first)
    left_behind = get_state(database, KEY_LAST_HEARTBEAT)
    assert left_behind is not None
    started_before = get_state(database, KEY_STARTED_AT)

    second = AppScheduler(database)
    jobs_after_start: list[str] = []
    run_for(0.3, second, lambda: jobs_after_start.extend(second.job_ids()))
    assert second.previous_heartbeat == left_behind
    assert jobs_after_start == [HEARTBEAT_JOB_ID], "the schedule comes back by itself"
    assert get_state(database, KEY_LAST_HEARTBEAT) >= left_behind
    assert get_state(database, KEY_STARTED_AT) >= started_before


def test_a_first_ever_start_has_no_previous_heartbeat(database):
    scheduler = AppScheduler(database)
    run_for(0.1, scheduler)
    assert scheduler.previous_heartbeat is None


def test_the_previous_heartbeat_is_logged_at_start(database, caplog):
    set_state(database, KEY_LAST_HEARTBEAT, "2026-10-31T23:59:59.000Z")
    with caplog.at_level(logging.INFO, logger="ytp.scheduler"):
        run_for(0.1, AppScheduler(database))
    record = next(r for r in caplog.records if r.getMessage() == "scheduler started")
    assert record.previous_heartbeat == "2026-10-31T23:59:59.000Z"


# --- health ----------------------


def test_check_is_green_while_beating_and_red_when_not_running(database):
    scheduler = AppScheduler(database)
    assert scheduler.check() == "not running"

    async def main():
        scheduler.start()
        await asyncio.sleep(0.1)
        assert scheduler.check() is None
        scheduler.shutdown()

    asyncio.run(main())
    assert scheduler.check() == "not running"


def test_check_notices_a_heartbeat_that_stopped(database):
    now = [1000.0]
    scheduler = AppScheduler(database, clock=lambda: now[0])

    async def main():
        scheduler.start()
        await asyncio.sleep(0.1)
        assert scheduler.check() is None
        now[0] += STALE_AFTER_HEARTBEATS * HEARTBEAT_SECONDS - 1
        assert scheduler.check() is None
        now[0] += 2
        assert "no heartbeat for" in (scheduler.check() or "")
        scheduler.shutdown()

    asyncio.run(main())


# --- inside the app ----------------------


def make_client(tmp_path: Path, **env: str) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), **env}
    return TestClient(create_app(Settings.from_env(values)))


def test_the_scheduler_runs_with_the_app_and_stops_with_it(tmp_path):
    with make_client(tmp_path) as client:
        scheduler = client.app.state.scheduler
        assert scheduler.running
        assert sorted(scheduler.job_ids()) == sorted(
            [
                HEARTBEAT_JOB_ID,
                "subscription-poll",
                "podcast-poll",
                "podcast-backlog",
                "alert-monitor",
            ]
        )
        assert client.get("/health").json()["checks"]["scheduler"] == "ok"
        assert get_state(client.app.state.db, KEY_LAST_HEARTBEAT) is not None
    assert not scheduler.running


def test_without_a_database_there_is_no_scheduler_and_health_says_so(tmp_path):
    with make_client(tmp_path, CONFIG_DIR=str(tmp_path / "missing")) as client:
        assert client.app.state.scheduler is None
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["checks"]["scheduler"] == "disabled: no database"


def test_a_stalled_scheduler_makes_the_app_unhealthy(tmp_path):
    with make_client(tmp_path) as client:
        scheduler = client.app.state.scheduler
        deadline = time.monotonic() + 5
        while scheduler._last_heartbeat is None and time.monotonic() < deadline:
            time.sleep(0.01)  # the first heartbeat runs in a worker thread right after start
        assert scheduler._last_heartbeat is not None
        scheduler._clock = lambda: time.monotonic() + 10 * HEARTBEAT_SECONDS
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["checks"]["scheduler"].startswith("stalled: no heartbeat")


def test_apscheduler_does_not_log_every_run(tmp_path, capfd):
    with make_client(tmp_path) as client:
        client.get("/health")
    log = capfd.readouterr().out
    assert "Running job" not in log
    assert "executed successfully" not in log
    assert "scheduler started" in log
