"""app/ytp/reconciler.py: job states follow qBittorrent, safely, repeatedly and after downtime."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.helpers.app import make_client as app_client
from tests.helpers.clock import DatetimeClock
from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent, qbit_env
from ytp import jobs
from ytp.clients.qbittorrent import QbitClient, Torrent
from ytp.db import Database
from ytp.jobs import Job
from ytp.reconciler import (
    GAP_SECONDS,
    KEY_LAST_OK,
    RECONCILE_JOB_ID,
    RECONCILE_SECONDS,
    OrganizeError,
    Reconciler,
)
from ytp.scheduler import get_state

H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40


def torrent(hash_: str, state: str = "downloading", progress: float = 0.5, **extra) -> dict:
    return {
        "hash": hash_,
        "name": f"t-{hash_[0]}",
        "state": state,
        "progress": progress,
        "category": "movies",
        "content_path": f"/data/downloads/movies/t-{hash_[0]}",
        **extra,
    }


DONE = {"state": "uploading", "progress": 1.0}


@pytest.fixture
def fake() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    yield server
    server.stop()


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database.open(tmp_path / "app.db")
    yield db
    db.close()


class Organizer:
    """Records calls; can fail on demand; remembers the job status it saw while running."""

    def __init__(self, db: Database):
        self.db = db
        self.calls: list[tuple[int, str]] = []
        self.status_seen: list[str] = []
        self.raises: BaseException | None = None
        self.note: str | None = None

    def __call__(self, job: Job, t: Torrent) -> str | None:
        self.calls.append((job.id, t.content_path))
        self.status_seen.append(jobs.get_job(self.db, job.id).status)
        if self.raises is not None:
            raise self.raises
        return self.note


def add_job(db: Database, infohash: str, status: str = "queued", created: datetime | None = None):
    job, _ = jobs.submit_magnet_job(db, infohash, '{"type":"movies","title":"x","year":2000}')
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job.id))
        if created is not None:
            conn.execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?",
                (created.strftime("%Y-%m-%dT%H:%M:%S.000Z"), job.id),
            )
    return job


def status_of(db: Database, job_id: int) -> tuple[str, str | None]:
    job = jobs.get_job(db, job_id)
    return job.status, job.error


def make(fake, database, organize=True, **kw) -> tuple[Reconciler, Organizer]:
    organizer = Organizer(database)
    qbit = QbitClient(fake.url, USER, PASSWORD, timeout=3)
    return Reconciler(database, qbit, organizer if organize else None, **kw), organizer


# --- the happy path ----------------------


def test_a_queued_job_whose_torrent_is_downloading_becomes_downloading(fake, database):
    job = add_job(database, H1)
    fake.torrents = [torrent(H1)]
    reconciler, organizer = make(fake, database)
    result = reconciler.run_once()
    assert result.started == [job.id]
    assert status_of(database, job.id) == ("downloading", None)
    assert organizer.calls == []


def test_a_finished_torrent_is_organized_and_the_job_is_done(fake, database):
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, organizer = make(fake, database)
    result = reconciler.run_once()
    assert result.done == [job.id]
    assert organizer.calls == [(job.id, "/data/downloads/movies/t-1")]
    assert organizer.status_seen == ["organizing"], "claimed before the organizer ran"
    assert status_of(database, job.id) == ("done", None)


def test_a_note_from_the_organizer_is_stored_on_the_done_job(fake, database):
    """`done` with a note: some files were skipped, and the job list says so (#69)."""
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, organizer = make(fake, database)
    organizer.note = "3 of 10 files skipped: S03E08.mkv: no episode number in the name"
    assert reconciler.run_once().done == [job.id]
    stored = jobs.get_job(database, job.id)
    assert (stored.status, stored.error) == ("done", None)
    assert stored.note == "3 of 10 files skipped: S03E08.mkv: no episode number in the name"


def test_organizing_a_torrent_asks_for_new_storage_sizes(fake, database):
    """The dashboard's storage bar is recomputed after every organized torrent (#82)."""
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, **DONE)]
    calls = []
    reconciler, _ = make(fake, database, on_organized=lambda: calls.append(1))
    assert reconciler.run_once().done == [job.id]
    assert calls == [1]


def test_a_torrent_that_finished_while_the_app_was_down_is_organized_at_first_sight(fake, database):
    job = add_job(database, H1, "queued")  # never saw it downloading
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, organizer = make(fake, database)
    assert reconciler.run_once().done == [job.id]
    assert len(organizer.calls) == 1


def test_running_again_changes_nothing(fake, database):
    add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, organizer = make(fake, database)
    reconciler.run_once()
    again = reconciler.run_once()
    assert (again.checked, again.done, again.failed) == (0, [], [])
    assert len(organizer.calls) == 1, "a done job is never organized twice"


def test_hashes_match_whatever_the_case(fake, database):
    lower = "ab" * 20
    job = add_job(database, lower)
    fake.torrents = [torrent(lower.upper(), **DONE)]
    reconciler, _ = make(fake, database)
    assert reconciler.run_once().done == [job.id]


def test_torrents_without_a_job_and_jobs_of_other_types_are_left_alone(fake, database):
    fake.torrents = [torrent(H2, **DONE)]  # added by hand in qBittorrent
    with database.transaction() as conn:
        conn.execute("INSERT INTO jobs (type, external_id) VALUES ('youtube', ?)", (H3,))
    reconciler, organizer = make(fake, database)
    result = reconciler.run_once()
    assert result.checked == 0
    assert organizer.calls == []


def test_several_jobs_are_handled_independently(fake, database):
    a, b, c = (
        add_job(database, h, s) for h, s in [(H1, "queued"), (H2, "downloading"), (H3, "queued")]
    )
    fake.torrents = [torrent(H1), torrent(H2, **DONE)]  # H3 unknown but young
    reconciler, _ = make(fake, database)
    result = reconciler.run_once()
    assert (result.started, result.done) == ([a.id], [b.id])
    assert status_of(database, c.id)[0] == "queued"


# --- waiting for qBittorrent to settle ----------------------


@pytest.mark.parametrize("state", ["moving", "checkingUP", "checkingDL", "checkingResumeData"])
def test_a_torrent_that_is_moving_or_being_checked_is_not_organized_yet(fake, database, state):
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, state=state, progress=1.0)]
    reconciler, organizer = make(fake, database)
    reconciler.run_once()
    assert organizer.calls == []
    assert status_of(database, job.id)[0] == "downloading"
    fake.torrents = [torrent(H1, state="stalledUP", progress=1.0)]
    assert reconciler.run_once().done == [job.id]


@pytest.mark.parametrize("state", ["stoppedUP", "pausedUP", "forcedUP", "queuedUP", "stalledUP"])
def test_every_finished_state_counts_as_finished(fake, database, state):
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, state=state, progress=1.0)]
    reconciler, _ = make(fake, database)
    assert reconciler.run_once().done == [job.id]


def test_an_unfinished_paused_torrent_is_just_downloading(fake, database):
    job = add_job(database, H1, "queued")
    fake.torrents = [torrent(H1, state="stoppedDL", progress=0.4)]
    reconciler, organizer = make(fake, database)
    reconciler.run_once()
    assert status_of(database, job.id)[0] == "downloading"
    assert organizer.calls == []


# --- failures, with readable reasons ----------------------


@pytest.mark.parametrize(
    ("state", "words"), [("error", "'error'"), ("missingFiles", "its files are gone")]
)
def test_a_broken_torrent_fails_the_job_with_a_reason(fake, database, state, words):
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, state=state)]
    reconciler, _ = make(fake, database)
    assert reconciler.run_once().failed == [job.id]
    status, error = status_of(database, job.id)
    assert status == "failed"
    assert words in error


def test_a_torrent_removed_from_qbittorrent_fails_a_downloading_job(fake, database):
    job = add_job(database, H1, "downloading")
    reconciler, _ = make(fake, database)
    assert reconciler.run_once().failed == [job.id]
    assert "no longer in qBittorrent" in status_of(database, job.id)[1]
    assert "send the magnet link again" in status_of(database, job.id)[1]


def test_a_young_queued_job_unknown_to_qbittorrent_is_left_for_the_add_to_finish(fake, database):
    job = add_job(database, H1, "queued")
    reconciler, _ = make(fake, database, queued_grace_seconds=120)
    assert reconciler.run_once().failed == []
    assert status_of(database, job.id)[0] == "queued"


def test_an_old_queued_job_unknown_to_qbittorrent_never_got_added(fake, database):
    """The app stopped between writing the job and asking qBittorrent (#13)."""
    old = datetime.now(UTC) - timedelta(minutes=10)
    job = add_job(database, H1, "queued", created=old)
    reconciler, _ = make(fake, database)
    assert reconciler.run_once().failed == [job.id]
    status, error = status_of(database, job.id)
    assert status == "failed"
    assert "never reached qBittorrent" in error


def test_a_failed_job_can_be_retried_by_sending_the_magnet_again(fake, database):
    old = datetime.now(UTC) - timedelta(minutes=10)
    job = add_job(database, H1, "queued", created=old)
    make(fake, database)[0].run_once()
    again, outcome = jobs.submit_magnet_job(database, H1, "{}")
    assert (again.id, outcome) == (job.id, "requeued")


# --- never stuck in organizing ----------------------


def test_an_organizer_error_fails_the_job_with_its_message(fake, database):
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, organizer = make(fake, database)
    organizer.raises = OrganizeError("no video file found in the torrent")
    assert reconciler.run_once().failed == [job.id]
    assert status_of(database, job.id) == (
        "failed",
        "organizing failed: no video file found in the torrent",
    )


@pytest.mark.parametrize(
    "error", [OSError("No space left on device"), KeyError("title"), RuntimeError("x")]
)
def test_any_other_exception_also_ends_in_failed_not_organizing(fake, database, error, caplog):
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, organizer = make(fake, database)
    organizer.raises = error
    with caplog.at_level(logging.ERROR, logger="ytp.reconciler"):
        reconciler.run_once()
    status, message = status_of(database, job.id)
    assert status == "failed"
    assert message.startswith(f"organizing failed: {type(error).__name__}")
    assert "organizer crashed" in caplog.text


def test_one_failing_job_does_not_stop_the_others(fake, database):
    a = add_job(database, H1, "downloading")
    b = add_job(database, H2, "downloading")
    fake.torrents = [torrent(H1, **DONE), torrent(H2, **DONE)]
    reconciler, organizer = make(fake, database)

    def flaky(job, t):
        organizer(job, t)
        if job.id == a.id:
            raise OrganizeError("first one is broken")

    reconciler.organize = flaky
    result = reconciler.run_once()
    assert (result.failed, result.done) == ([a.id], [b.id])


def test_a_job_left_organizing_by_a_crash_is_organized_again(fake, database, caplog):
    """The app died in the middle of organizing: the next run picks it up."""
    job = add_job(database, H1, "organizing")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, organizer = make(fake, database)
    with caplog.at_level(logging.WARNING, logger="ytp.reconciler"):
        result = reconciler.run_once()
    assert result.requeued == [job.id]
    assert result.done == [job.id]
    assert len(organizer.calls) == 1
    assert "interrupted" in caplog.text


def test_a_job_left_organizing_whose_torrent_vanished_fails_instead_of_sticking(fake, database):
    job = add_job(database, H1, "organizing")
    reconciler, _ = make(fake, database)
    reconciler.run_once()
    assert status_of(database, job.id)[0] == "failed"


def test_no_job_is_left_organizing_after_any_run(fake, database):
    ids = [add_job(database, h, "organizing").id for h in (H1, H2, H3)]
    fake.torrents = [torrent(H1, **DONE), torrent(H2, state="error")]
    reconciler, _ = make(fake, database)
    reconciler.run_once()
    assert all(status_of(database, i)[0] != "organizing" for i in ids)


def test_without_an_organizer_finished_torrents_wait_and_nothing_is_lost(fake, database, caplog):
    job = add_job(database, H1, "queued")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, _ = make(fake, database, organize=False)
    with caplog.at_level(logging.WARNING, logger="ytp.reconciler"):
        reconciler.run_once()
        reconciler.run_once()
    assert status_of(database, job.id) == ("downloading", None)
    assert caplog.text.count("no organizer is installed") == 1, "warned once, not every 30 s"
    reconciler.organize = Organizer(database)  # #16 arrives
    assert reconciler.run_once().done == [job.id]


# --- qBittorrent away ----------------------


def test_qbittorrent_being_away_changes_no_job_and_is_logged_once(fake, database, caplog):
    job = add_job(database, H1, "downloading")
    fake.torrents = [torrent(H1, **DONE)]
    reconciler, _ = make(fake, database)
    port = fake.port
    fake.stop()
    with caplog.at_level(logging.INFO, logger="ytp.reconciler"):
        for _ in range(3):
            assert reconciler.run_once().skipped == "qBittorrent unreachable"
        assert status_of(database, job.id)[0] == "downloading", "an outage never fails a job"
        assert caplog.text.count("qBittorrent is away") == 1
        restarted = FakeQbittorrent().start(port)
        try:
            restarted.torrents = [torrent(H1, **DONE)]
            assert reconciler.run_once().done == [job.id]
        finally:
            restarted.stop()
        assert "reachable again" in caplog.text


def test_a_refused_qbittorrent_login_is_reported_once_and_does_not_raise(fake, database, caplog):
    add_job(database, H1, "downloading")
    qbit = QbitClient(fake.url, USER, "wrong password", timeout=3)
    reconciler = Reconciler(database, qbit, Organizer(database))
    with caplog.at_level(logging.ERROR, logger="ytp.reconciler"):
        for _ in range(4):
            assert reconciler.run_once().skipped == "qBittorrent login refused"
    assert caplog.text.count("cannot log in") == 1
    assert fake.logins == 1, "no hammering: a refused login is not retried"


def test_a_success_records_when_the_last_good_run_was(fake, database):
    make(fake, database)[0].run_once()
    assert get_state(database, KEY_LAST_OK).endswith("Z")


# --- the torrent states ----------------------


@pytest.mark.parametrize(
    ("state", "progress", "ready"),
    [
        ("uploading", 1.0, True),
        ("stalledUP", 1.0, True),
        ("moving", 1.0, False),
        ("checkingUP", 1.0, False),
        ("downloading", 0.99, False),
        ("stoppedDL", 0.5, False),
        ("error", 1.0, False),
        ("missingFiles", 1.0, False),
    ],
)
def test_torrent_ready_to_organize(state, progress, ready):
    t = Torrent.from_api({"hash": H1, "name": "x", "state": state, "progress": progress})
    assert t.ready_to_organize is ready
    assert t.failed is (state in {"error", "missingFiles"})


# --- inside the app ----------------------


def make_client(tmp_path: Path, fake: FakeQbittorrent | None, **env: str) -> TestClient:
    qbit = qbit_env(fake) if fake is not None else {}
    return app_client(tmp_path, **{**qbit, **env})


def test_the_app_schedules_the_reconciler_every_30_seconds_when_qbittorrent_is_configured(
    tmp_path, fake
):
    with make_client(tmp_path, fake) as client:
        scheduler = client.app.state.scheduler
        assert RECONCILE_JOB_ID in scheduler.job_ids()
        job = scheduler._scheduler.get_job(RECONCILE_JOB_ID)
        assert job.trigger.interval.total_seconds() == RECONCILE_SECONDS == 30
        assert job.max_instances == 1
        assert client.get("/health").status_code == 200


def test_without_qbittorrent_there_is_no_reconciler(tmp_path):
    with make_client(tmp_path, None) as client:
        assert RECONCILE_JOB_ID not in client.app.state.scheduler.job_ids()
        assert client.app.state.reconciler is None


def test_a_job_started_through_the_endpoint_is_picked_up_by_the_scheduled_run(tmp_path, fake):
    """End to end inside the app: POST, qBittorrent 'downloads', the reconciler notices."""
    magnet = f"magnet:?xt=urn:btih:{H1}"
    with make_client(tmp_path, fake) as client:
        body = {"magnet": magnet, "metadata": {"type": "movies", "title": "X", "year": 2000}}
        assert client.post("/api/magnet", json=body).status_code == 201
        fake.torrents = [torrent(H1)]
        client.app.state.reconciler.run_once()
        job = jobs.get_job(client.app.state.db, 1)
        assert job.status == "downloading"


# --- giving up on a stalled magnet link (#40) ----------------------

DAY = timedelta(days=1)
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
DOWNLOADS = Path("/data/downloads")
NO_METADATA = {"state": "metaDL", "progress": 0.0, "size": 0, "amount_left": 0, "content_path": ""}


def partial(left: int, state: str = "stalledDL", **extra) -> dict:
    return {
        "state": state,
        "progress": (1000 - left) / 1000,
        "size": 1000,
        "amount_left": left,
        **extra,
    }


def stall_reconciler(fake, database, clock, days: float | None = 7, **kw) -> Reconciler:
    """The tests jump whole days between runs (the real schedule is every 30 s), so the gap that
    restarts the clocks is made huge unless a test is about the gap."""
    qbit = QbitClient(fake.url, USER, PASSWORD, timeout=3)
    kw.setdefault("gap_seconds", 100 * 86400)
    return Reconciler(
        database,
        qbit,
        Organizer(database),
        now=clock,
        stall_seconds=None if days is None else days * 86400,
        downloads_dir=DOWNLOADS,
        **kw,
    )


def deletions(fake) -> list:
    return fake.calls("torrents/delete")


def test_c40_a_torrent_without_metadata_is_given_up_after_seven_days(fake, database):
    job = add_job(database, H1)
    fake.torrents = [torrent(H1, **NO_METADATA)]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()  # first sight: the clock starts
    clock.advance(DAY * 6 + timedelta(hours=23))
    assert reconciler.run_once().gave_up == []
    assert status_of(database, job.id)[0] == "downloading"
    assert deletions(fake) == []
    clock.advance(timedelta(hours=1))
    result = reconciler.run_once()
    assert result.gave_up == [job.id]
    assert result.failed == [job.id]
    status, error = status_of(database, job.id)
    assert status == "failed"
    assert error == "no sources for 7 days: the torrent never got its file list"
    assert fake.torrents == []  # removed from qBittorrent
    assert len(deletions(fake)) == 1


def test_c40_a_partial_download_that_stops_is_given_up_with_its_percentage(fake, database):
    job = add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()
    clock.advance(DAY * 7)
    result = reconciler.run_once()
    assert result.gave_up == [job.id]
    assert status_of(database, job.id) == (
        "failed",
        "no sources for 7 days: the download stopped at 40 %",
    )


def test_c40_a_slow_but_moving_download_is_never_given_up(fake, database):
    job = add_job(database, H1, status="downloading")
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    left = 900
    for _ in range(30):  # a month, one byte-ish step a day
        fake.torrents = [torrent(H1, **partial(left))]
        assert reconciler.run_once().gave_up == []
        left -= 1
        clock.advance(DAY)
    assert status_of(database, job.id)[0] == "downloading"
    assert deletions(fake) == []


def test_c40_progress_restarts_the_clock(fake, database):
    job = add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()
    clock.advance(DAY * 6)
    fake.torrents = [torrent(H1, **partial(599))]  # one more byte on day 6
    reconciler.run_once()
    clock.advance(DAY * 6)  # 12 days since the start, 6 since the last progress
    assert reconciler.run_once().gave_up == []
    clock.advance(DAY)
    assert reconciler.run_once().gave_up == [job.id]


def test_c40_a_failed_job_can_be_retried_and_gets_a_fresh_clock(fake, database):
    job = add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()
    clock.advance(DAY * 7)
    assert reconciler.run_once().gave_up == [job.id]
    # The retry of #26: the same job is queued again and the magnet link is added anew.
    queued, outcome = jobs.submit_magnet_job(database, H1, '{"type":"movies","title":"x"}')
    assert outcome == "requeued"
    assert queued.id == job.id
    assert (queued.progress_bytes, queued.progress_at) == (None, None)
    fake.torrents = [torrent(H1, **partial(600))]
    reconciler.run_once()
    clock.advance(DAY * 6)
    assert reconciler.run_once().gave_up == []
    clock.advance(DAY)
    assert reconciler.run_once().gave_up == [job.id]


def test_c40_time_while_paused_does_not_count(fake, database):
    job = add_job(database, H1, status="downloading")
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    fake.torrents = [torrent(H1, **partial(600))]
    reconciler.run_once()
    fake.torrents = [torrent(H1, **partial(600, state="stoppedDL"))]  # the watchdog paused it
    for _ in range(10):
        clock.advance(DAY)
        assert reconciler.run_once().gave_up == []
    fake.torrents = [torrent(H1, **partial(600))]  # resumed, still no source
    clock.advance(timedelta(minutes=1))
    assert reconciler.run_once().gave_up == []
    clock.advance(DAY * 6)
    assert reconciler.run_once().gave_up == []
    clock.advance(DAY * 2)
    assert reconciler.run_once().gave_up == [job.id]


def test_c40_qbittorrent_queueing_and_checking_do_not_count(fake, database):
    add_job(database, H1, status="downloading")
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    for state in ("queuedDL", "checkingResumeData", "queuedDL"):
        fake.torrents = [torrent(H1, **partial(600, state=state))]
        reconciler.run_once()
        clock.advance(DAY * 5)
        assert reconciler.run_once().gave_up == []


def test_c40_downtime_of_the_app_does_not_count(fake, database):
    job = add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    stall_reconciler(fake, database, clock, gap_seconds=GAP_SECONDS).run_once()
    clock.advance(DAY * 6)
    restarted = stall_reconciler(
        fake, database, clock, gap_seconds=GAP_SECONDS
    )  # a new process, state from the database
    assert restarted.run_once().gave_up == []  # the time away restarts the clock
    later = stall_reconciler(fake, database, clock)  # (jumping days now, no gap detection)
    clock.advance(DAY * 2)
    assert later.run_once().gave_up == []  # 8 days since the start, 2 since coming back
    clock.advance(DAY * 5)
    assert later.run_once().gave_up == [job.id]


def test_c40_an_outage_of_qbittorrent_does_not_count(fake, database):
    add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock, gap_seconds=GAP_SECONDS)
    reconciler.run_once()
    fake.list_status = 503
    clock.advance(DAY * 6)
    assert reconciler.run_once().skipped
    fake.list_status = 200
    clock.advance(timedelta(minutes=1))
    assert reconciler.run_once().gave_up == []
    clock.advance(DAY * 2)
    assert reconciler.run_once().gave_up == []


@pytest.mark.parametrize("days", [None, 0])
def test_c40_zero_or_unset_never_gives_up(fake, database, days):
    job = add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock, days=days)
    for _ in range(5):
        reconciler.run_once()
        clock.advance(DAY * 20)
    assert status_of(database, job.id)[0] == "downloading"
    assert deletions(fake) == []


def test_c40_files_are_only_deleted_inside_downloads(fake, database):
    add_job(database, H1, status="downloading")
    add_job(database, H2, status="downloading")
    add_job(database, H3, status="downloading")
    fake.torrents = [
        torrent(H1, **partial(600, content_path="/data/downloads/movies/a")),
        torrent(H2, **partial(600, content_path="/data/Filme/a")),
        torrent(H3, **partial(600, content_path="/data/downloads/../Filme/a")),
    ]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()
    clock.advance(DAY * 7)
    assert len(reconciler.run_once().gave_up) == 3
    flags = {c.form["hashes"][0][0]: c.form["deleteFiles"][0] for c in deletions(fake)}
    assert flags == {"1": "true", "2": "false", "3": "false"}


def test_c40_without_a_downloads_folder_no_files_are_deleted(fake, database):
    add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    qbit = QbitClient(fake.url, USER, PASSWORD, timeout=3)
    reconciler = Reconciler(
        database, qbit, now=clock, stall_seconds=7 * 86400, gap_seconds=100 * 86400
    )
    reconciler.run_once()
    clock.advance(DAY * 7)
    reconciler.run_once()
    assert deletions(fake)[0].form["deleteFiles"] == ["false"]


def test_c40_if_qbittorrent_refuses_the_removal_the_job_is_not_failed_and_it_tries_again(
    fake, database
):
    job = add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()
    clock.advance(DAY * 7)
    fake.delete_status = 500
    assert reconciler.run_once().gave_up == []
    assert status_of(database, job.id)[0] == "downloading"
    fake.delete_status = 200
    clock.advance(timedelta(seconds=30))
    assert reconciler.run_once().gave_up == [job.id]


def test_c40_a_finished_torrent_is_never_given_up(fake, database):
    job = add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()
    clock.advance(DAY * 30)
    fake.torrents = [torrent(H1, **DONE, size=1000, amount_left=0)]
    result = reconciler.run_once()
    assert result.done == [job.id]
    assert result.gave_up == []
    assert deletions(fake) == []


def test_c40_giving_up_is_logged_without_the_magnet_link(fake, database, caplog):
    add_job(database, H1, status="downloading")
    fake.torrents = [torrent(H1, **partial(600))]
    clock = DatetimeClock(T0)
    reconciler = stall_reconciler(fake, database, clock)
    reconciler.run_once()
    clock.advance(DAY * 7)
    with caplog.at_level(logging.WARNING, logger="ytp.reconciler"):
        reconciler.run_once()
    records = [r for r in caplog.records if r.getMessage() == "job gave up: no sources"]
    assert len(records) == 1
    assert records[0].days == 7


def test_c40_the_app_hands_the_stall_limit_and_downloads_folder_to_the_reconciler(tmp_path, fake):
    with make_client(tmp_path, fake, MAGNET_STALL_DAYS="3") as client:
        reconciler = client.app.state.reconciler
        assert reconciler._stall_seconds == 3 * 86400
        assert reconciler._downloads_dir == client.app.state.settings.downloads_dir
    with make_client(tmp_path, fake, MAGNET_STALL_DAYS="0") as client:
        assert client.app.state.reconciler._stall_seconds is None
