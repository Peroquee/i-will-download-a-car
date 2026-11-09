"""High-priority downloads (#51): queue order, held-back normals, pause/resume, instant refresh.

The queue lives in SQLite; the torrent controls go to the fake qBittorrent the other tests use.
No test sleeps on the clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from tests.unit.test_podcast_jobs import World as PodcastWorld
from tests.unit.test_youtube_download import ID
from tests.unit.test_youtube_download import World as YoutubeWorld
from ytp import jobs
from ytp.clients.qbittorrent import QbitClient
from ytp.db import Database
from ytp.podcast_jobs import JOB_TYPE as PODCAST_JOB_TYPE
from ytp.priority import PriorityManager
from ytp.youtube_jobs import JOB_TYPE as YOUTUBE_JOB_TYPE
from ytp.youtube_jobs import YoutubeJobMetadata

H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40
MOVIE = '{"type":"movies","title":"x","year":2000}'


def torrent(infohash: str, state: str = "downloading", progress: float = 0.5) -> dict:
    return {
        "hash": infohash,
        "name": infohash[:4],
        "state": state,
        "progress": progress,
        "category": "movies",
        "content_path": f"/data/downloads/movies/{infohash[:4]}",
    }


@pytest.fixture
def fake() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    yield server
    server.stop()


@pytest.fixture
def db(tmp_path) -> Iterator[Database]:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


def magnet_job(db: Database, infohash: str, *, priority: int = 0, status: str = "queued"):
    job, _ = jobs.submit_magnet_job(db, infohash, MOVIE, priority=priority)
    if status != "queued":
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job.id))
    return jobs.get_job(db, job.id)


def youtube_job(db: Database, video_id: str, *, priority: int = 0):
    metadata = YoutubeJobMetadata(video_id=video_id, channel="Blender", title="T")
    job, _ = jobs.submit_job(
        db, YOUTUBE_JOB_TYPE, video_id, metadata.model_dump_json(), priority=priority
    )
    return job


def podcast_job(db: Database, key: str, *, priority: int = 0):
    job, _ = jobs.submit_job(db, PODCAST_JOB_TYPE, key, '{"type":"podcast"}', priority=priority)
    return job


def manager(db: Database, fake: FakeQbittorrent, watchdog=None) -> PriorityManager:
    return PriorityManager(db, QbitClient(fake.url, USER, PASSWORD, timeout=3), watchdog)


# --- the queue (#51 requirement 3) ---------------------------------------------------------------


def test_a_high_priority_job_is_claimed_before_an_older_normal_one(db):
    normal = youtube_job(db, "normal-video")
    high = youtube_job(db, "high-video", priority=1)

    claimed = jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE)

    assert claimed is not None
    assert claimed.id == high.id, "priority wins over age"
    assert claimed.priority == 1

    jobs.transition(db, high.id, "done", expected=("downloading",))
    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE).id == normal.id


def test_the_old_order_still_applies_within_the_same_priority(db):
    first = youtube_job(db, "first")
    second = youtube_job(db, "second")
    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE).id == first.id
    jobs.transition(db, first.id, "done", expected=("downloading",))
    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE).id == second.id


# --- normal claims wait (#51 requirement 4a) -----------------------------------------------------


def test_normal_youtube_and_podcast_jobs_wait_while_a_high_job_downloads(db):
    high = youtube_job(db, "high-video", priority=1)
    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE).id == high.id  # -> downloading
    normal_youtube = youtube_job(db, "normal-video")
    podcast_job(db, "p" * 40)

    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE) is None
    assert jobs.claim_next_queued(db, PODCAST_JOB_TYPE) is None

    jobs.transition(db, high.id, "done", expected=("downloading",))
    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE).id == normal_youtube.id


def test_another_high_priority_job_is_claimed_while_one_downloads(db):
    first = youtube_job(db, "high-one", priority=1)
    second = youtube_job(db, "high-two", priority=1)
    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE).id == first.id
    assert jobs.claim_next_queued(db, YOUTUBE_JOB_TYPE).id == second.id


# --- normal torrents are paused / resumed (#51 requirement 4b) ------------------------------------


def test_normal_torrents_are_paused_while_a_high_job_downloads(db, fake):
    magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    magnet_job(db, H3, status="queued")  # qBittorrent already runs it; the job status lags
    fake.torrents = [torrent(H1), torrent(H2), torrent(H3)]

    manager(db, fake).tick()

    (pause,) = fake.calls("torrents/stop")
    assert pause.form["hashes"] == [f"{H2}|{H3}"]
    assert jobs.app_paused_hashes(db) == [H2, H3]


def test_a_second_tick_does_not_pause_again(db, fake):
    magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.torrents = [torrent(H1), torrent(H2)]
    priority = manager(db, fake)

    priority.tick()
    priority.tick()

    assert len(fake.calls("torrents/stop")) == 1


def test_a_torrent_the_user_paused_is_neither_paused_nor_resumed(db, fake):
    high = magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    magnet_job(db, H3, status="downloading")
    fake.torrents = [
        torrent(H1),
        torrent(H2, state="pausedDL"),  # the user paused this one
        torrent(H3),
    ]
    priority = manager(db, fake)

    priority.tick()
    (pause,) = fake.calls("torrents/stop")
    assert pause.form["hashes"] == [H3], "only the running normal torrent is paused"
    assert jobs.app_paused_hashes(db) == [H3]

    jobs.transition(db, high.id, "done", expected=("downloading",))
    priority.tick()
    (resume,) = fake.calls("torrents/start")
    assert resume.form["hashes"] == [H3]
    assert jobs.app_paused_hashes(db) == []


def test_a_finished_torrent_is_not_paused(db, fake):
    magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.torrents = [torrent(H1), torrent(H2, state="uploading", progress=1.0)]

    manager(db, fake).tick()

    assert fake.calls("torrents/stop") == [], "a seeding torrent keeps running"
    assert jobs.app_paused_hashes(db) == []


def test_the_paused_hashes_survive_a_restart(db, fake, tmp_path: Path):
    high = magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.torrents = [torrent(H1), torrent(H2)]
    manager(db, fake).tick()
    assert jobs.app_paused_hashes(db) == [H2]

    # "restart": new database and client objects on the same file.
    reopened = Database.open(tmp_path / "app.db")
    try:
        assert jobs.app_paused_hashes(reopened) == [H2], "the hashes live in the database"
        jobs.transition(reopened, high.id, "done", expected=("downloading",))
        PriorityManager(reopened, QbitClient(fake.url, USER, PASSWORD, timeout=3)).tick()
        (resume,) = fake.calls("torrents/start")
        assert resume.form["hashes"] == [H2]
        assert jobs.app_paused_hashes(reopened) == []
    finally:
        reopened.close()


def test_resume_does_nothing_while_a_high_job_still_downloads(db, fake):
    magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.torrents = [torrent(H1), torrent(H2)]
    priority = manager(db, fake)
    priority.tick()
    priority.tick()

    assert fake.calls("torrents/start") == []


def test_qbittorrent_trouble_does_not_record_a_pause_it_did_not_do(db, fake):
    magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.fail_status = 503

    manager(db, fake).tick()  # must not raise

    assert jobs.app_paused_hashes(db) == []


# --- the VPN watchdog's pause wins (#51 follow-up) -----------------------------------------------


class FakeWatchdog:
    def __init__(self, status: dict) -> None:
        self.status = status
        self.calls = 0

    def fetch_status(self) -> dict:
        self.calls += 1
        return self.status


def paused_by_the_app(db: Database, fake: FakeQbittorrent):
    """A high-priority download runs and the app has paused the normal torrent H2."""
    high = magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.torrents = [torrent(H1), torrent(H2)]
    manager(db, fake).tick()
    assert jobs.app_paused_hashes(db) == [H2]
    jobs.transition(db, high.id, "done", expected=("downloading",))
    return high


@pytest.mark.parametrize("state", ["down", "blind", "starting", "unknown"])
def test_no_resume_while_the_watchdog_is_not_healthy(db, fake, state):
    paused_by_the_app(db, fake)
    watchdog = FakeWatchdog({"state": state, "paused": False})

    manager(db, fake, watchdog).tick()

    assert fake.calls("torrents/start") == []
    assert jobs.app_paused_hashes(db) == [H2], "the rows are kept and retried"


def test_resume_happens_once_the_watchdog_is_healthy_again(db, fake):
    paused_by_the_app(db, fake)
    watchdog = FakeWatchdog({"state": "down", "paused": False})
    priority = manager(db, fake, watchdog)
    priority.tick()
    assert jobs.app_paused_hashes(db) == [H2]

    watchdog.status = {"state": "ok", "paused": False}
    priority.tick()

    (resume,) = fake.calls("torrents/start")
    assert resume.form["hashes"] == [H2]
    assert jobs.app_paused_hashes(db) == []


def test_the_watchdogs_own_pause_is_not_undone(db, fake):
    """The watchdog paused all torrents during an outage, after the app paused its ones."""
    paused_by_the_app(db, fake)
    with db.transaction() as conn:
        conn.execute("UPDATE priority_paused SET paused_at = '2026-11-09T01:00:00.500Z'")
    watchdog = FakeWatchdog({"state": "down", "paused": True, "since": "2026-11-09T01:00:05+00:00"})

    manager(db, fake, watchdog).tick()

    assert fake.calls("torrents/start") == []
    assert jobs.app_paused_hashes(db) == [], "the watchdog's policy owns the torrents now"


def test_a_watchdog_pause_in_the_same_second_still_wins(db, fake):
    """`since` has second precision: a watchdog pause in the app's second counts as newer."""
    paused_by_the_app(db, fake)
    with db.transaction() as conn:
        conn.execute("UPDATE priority_paused SET paused_at = '2026-11-09T01:00:01.900Z'")
    watchdog = FakeWatchdog({"state": "down", "paused": True, "since": "2026-11-09T01:00:01+00:00"})

    manager(db, fake, watchdog).tick()

    assert fake.calls("torrents/start") == []
    assert jobs.app_paused_hashes(db) == []


def test_a_reported_watchdog_pause_wins_when_the_times_cannot_be_compared(db, fake):
    paused_by_the_app(db, fake)
    watchdog = FakeWatchdog({"state": "down", "paused": True})  # no usable `since`

    manager(db, fake, watchdog).tick()

    assert fake.calls("torrents/start") == []
    assert jobs.app_paused_hashes(db) == []


def test_an_egress_mismatch_keeps_the_watchdogs_pause(db, fake):
    """The watchdog never auto-resumes after an egress mismatch; neither may the app."""
    paused_by_the_app(db, fake)
    watchdog = FakeWatchdog({"state": "ok", "paused": False, "egress": "mismatch"})

    manager(db, fake, watchdog).tick()

    assert fake.calls("torrents/start") == []
    assert jobs.app_paused_hashes(db) == []


def test_without_a_watchdog_client_the_manager_resumes_as_before(db, fake):
    paused_by_the_app(db, fake)

    manager(db, fake).tick()

    (resume,) = fake.calls("torrents/start")
    assert resume.form["hashes"] == [H2]
    assert jobs.app_paused_hashes(db) == []


# --- a watchdog pause during a high-priority download (#66) ---------------------------------------


def test_an_outage_that_starts_and_ends_during_a_high_download_keeps_the_pause(db, fake):
    """#66: the pause is seen though the high job outlives the outage.

    The watchdog pauses everything when the VPN drops; the app's own rows must go without a
    resume, or the manager would undo the watchdog's "stay paused" policy when the job ends.
    """
    high = magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.torrents = [torrent(H1), torrent(H2)]
    watchdog = FakeWatchdog({"state": "ok", "paused": False})
    priority = manager(db, fake, watchdog)
    priority.tick()  # the app pauses H2 for the high-priority download
    assert jobs.app_paused_hashes(db) == [H2]
    with db.transaction() as conn:
        conn.execute("UPDATE priority_paused SET paused_at = '2026-11-09T01:00:00.500Z'")

    # The outage starts: the watchdog pauses everything (qBittorrent reports H2 paused).
    fake.torrents = [torrent(H1), torrent(H2, state="pausedDL")]
    watchdog.status = {"state": "down", "paused": True, "since": "2026-11-09T01:00:05+00:00"}
    priority.tick()
    assert jobs.app_paused_hashes(db) == [], "the watchdog's policy owns the torrents now"

    # The outage ends before the high download does; the watchdog clears `paused` again.
    watchdog.status = {"state": "ok", "paused": False}
    priority.tick()
    assert jobs.app_paused_hashes(db) == []

    jobs.transition(db, high.id, "done", expected=("downloading",))
    priority.tick()

    assert fake.calls("torrents/start") == [], "the app never resumes the watchdog's pause"
    assert jobs.app_paused_hashes(db) == []


def test_an_egress_mismatch_during_a_high_download_is_never_resumed(db, fake):
    high = magnet_job(db, H1, priority=1, status="downloading")
    magnet_job(db, H2, status="downloading")
    fake.torrents = [torrent(H1), torrent(H2)]
    watchdog = FakeWatchdog({"state": "ok", "paused": False})
    priority = manager(db, fake, watchdog)
    priority.tick()
    assert jobs.app_paused_hashes(db) == [H2]

    fake.torrents = [torrent(H1), torrent(H2, state="pausedDL")]
    watchdog.status = {"state": "ok", "paused": False, "egress": "mismatch"}
    priority.tick()
    assert jobs.app_paused_hashes(db) == []

    watchdog.status = {"state": "ok", "paused": False}
    jobs.transition(db, high.id, "done", expected=("downloading",))
    priority.tick()

    assert fake.calls("torrents/start") == []
    assert jobs.app_paused_hashes(db) == []


def test_the_watchdog_status_is_read_once_per_tick(db, fake):
    paused_by_the_app(db, fake)
    watchdog = FakeWatchdog({"state": "ok", "paused": False})

    manager(db, fake, watchdog).tick()

    assert watchdog.calls == 1, "the pre-check's answer is passed to the resume path"
    (resume,) = fake.calls("torrents/start")
    assert resume.form["hashes"] == [H2]


def test_the_watchdog_is_not_asked_without_paused_rows(db, fake):
    watchdog = FakeWatchdog({"state": "ok", "paused": False})

    manager(db, fake, watchdog).tick()

    assert watchdog.calls == 0


# --- the immediate Jellyfin refresh (#51 requirement 5) ------------------------------------------


def test_a_high_priority_youtube_job_asks_for_an_immediate_refresh(tmp_path):
    world = YoutubeWorld(tmp_path)
    calls: list[tuple[str | None, bool]] = []
    world.refresher.request_refresh = lambda folder=None, *, immediate=False: calls.append(
        (folder, immediate)
    )
    metadata = YoutubeJobMetadata(video_id=ID, channel="Blender", title="Big Buck Bunny")
    jobs.submit_job(world.db, YOUTUBE_JOB_TYPE, ID, metadata.model_dump_json(), priority=1)

    assert world.worker().run_one() is True

    assert calls == [("Youtube", True)]


def test_a_normal_youtube_job_keeps_the_debounce(tmp_path):
    world = YoutubeWorld(tmp_path)
    calls: list[tuple[str | None, bool]] = []
    world.refresher.request_refresh = lambda folder=None, *, immediate=False: calls.append(
        (folder, immediate)
    )
    metadata = YoutubeJobMetadata(video_id=ID, channel="Blender", title="Big Buck Bunny")
    jobs.submit_job(world.db, YOUTUBE_JOB_TYPE, ID, metadata.model_dump_json())

    assert world.worker().run_one() is True

    assert calls == [("Youtube", False)]


def test_a_high_priority_podcast_job_asks_for_an_immediate_refresh(tmp_path):
    world = PodcastWorld(tmp_path)
    try:
        world.fetcher.files["https://cdn.example/new.mp3"] = b"ID3audio"
        job = world.queue()
        with world.db.transaction() as conn:
            conn.execute("UPDATE jobs SET priority = 1 WHERE id = ?", (job.id,))

        assert world.worker().run_one() is True

        assert world.refresher.asked == ["Podcasts"]
        assert world.refresher.immediate == [True]
    finally:
        world.db.close()
