"""The podcast worker and the feed poller (issue #34), with a fake network and a real database."""

# ruff: noqa: E501

from __future__ import annotations

import logging
import random
import threading
from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.test_podcasts import JPEG, rss
from ytp import jobs, podcast_subscriptions
from ytp.config import Settings
from ytp.db import Database
from ytp.podcast_jobs import (
    JOB_TYPE,
    PodcastJobMetadata,
    PodcastPoller,
    PodcastWorker,
    episode_metadata,
)
from ytp.podcasts import PodcastError, PodcastFeedClient, episode_key, parse_podcast
from ytp.safefetch import Fetched, FetchError

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
FEED = "https://podcast.example/feed.xml"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SUBSCRIBED = "2026-10-20T00:00:00.000Z"


class FakeFetcher:
    """Stands in for SafeFetcher: feeds and files come from dicts, every call is recorded."""

    def __init__(self):
        self.feeds: dict[str, bytes | FetchError] = {}
        self.files: dict[str, bytes | FetchError] = {}
        self.images: dict[str, bytes | FetchError] = {}
        self.calls: list[tuple[str, str]] = []
        self.content_type = "audio/mpeg"
        self.final_url: str | None = None
        self.gate: threading.Event | None = None

    def fetch_bytes(self, url, *, kind, max_bytes):
        self.calls.append((kind, url))
        table = self.feeds if kind == "feed" else self.images
        answer = table.get(url)
        if answer is None:
            raise FetchError("not_found", "not found (HTTP 404)")
        if isinstance(answer, FetchError):
            raise answer
        return Fetched(
            url, "application/rss+xml" if kind == "feed" else "image/jpeg", answer, len(answer)
        )

    def download(self, url, destination, *, kind, max_bytes, deadline_seconds=0, progress=None):
        self.calls.append((kind, url))
        if self.gate is not None:
            self.gate.wait(5)
        answer = self.files.get(url)
        if answer is None:
            raise FetchError("not_found", "not found (HTTP 404)")
        if isinstance(answer, FetchError):
            raise answer
        with destination.open("xb") as out:
            out.write(answer)
        if progress:
            progress(len(answer) // 2, len(answer))
            progress(len(answer), len(answer))
        return Fetched(self.final_url or url, self.content_type, b"", len(answer))


class Refresher:
    def __init__(self):
        self.asked: list[str | None] = []

    def request_refresh(self, folder=None):
        self.asked.append(folder)


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        (tmp_path / "Podcasts").mkdir()
        (tmp_path / "config").mkdir()
        self.settings = Settings.from_env(
            {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(tmp_path / "config")}
        )
        self.db = Database.open(self.settings.database_path)
        self.fetcher = FakeFetcher()
        self.feeds = PodcastFeedClient(self.fetcher)
        self.refresher = Refresher()

    def worker(self, **kw) -> PodcastWorker:
        return PodcastWorker(self.db, self.settings, self.feeds, self.refresher, **kw)

    def queue(
        self,
        url="https://cdn.example/new.mp3",
        title="Pilot",
        podcast="Test Cast",
        cover=None,
        key="k" * 40,
        **meta,
    ) -> jobs.Job:
        metadata = PodcastJobMetadata(
            podcast=podcast,
            title=title,
            published="2026-10-31",
            feed_url=FEED,
            enclosure_url=url,
            cover_url=cover,
            **meta,
        )
        job, _ = jobs.submit_job(self.db, JOB_TYPE, key, metadata.model_dump_json())
        return job

    def status(self, job_id):
        job = jobs.get_job(self.db, job_id)
        return job.status, job.error

    def tree(self):
        library = self.tmp / "Podcasts"
        return sorted(str(p.relative_to(library)) for p in library.rglob("*") if p.is_file())


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.db.close()


# --- the worker ---------------------------------------------------------------------------------


def test_an_episode_is_downloaded_filed_and_marked_done(world):
    world.fetcher.files["https://cdn.example/new.mp3"] = b"ID3audio"
    job = world.queue()
    assert world.worker().run_one() is True
    assert world.status(job.id) == ("done", None)
    assert world.tree() == ["Test Cast/2026-10-31 - Pilot.mp3"]
    assert (world.tmp / "Podcasts/Test Cast/2026-10-31 - Pilot.mp3").read_bytes() == b"ID3audio"
    assert world.refresher.asked == ["Podcasts"]
    assert not (world.tmp / "downloads/incomplete/podcasts" / job.external_id).exists()


def test_nothing_queued_means_nothing_to_do(world):
    assert world.worker().run_one() is False


def test_the_cover_is_saved_once_per_podcast(world):
    world.fetcher.files["https://cdn.example/a.mp3"] = b"ID3a"
    world.fetcher.files["https://cdn.example/b.mp3"] = b"ID3b"
    world.fetcher.images["https://cdn.example/c.jpg"] = JPEG
    world.queue("https://cdn.example/a.mp3", "A", cover="https://cdn.example/c.jpg", key="a" * 40)
    world.queue("https://cdn.example/b.mp3", "B", cover="https://cdn.example/c.jpg", key="b" * 40)
    worker = world.worker()
    worker.run_one()
    worker.run_one()
    assert world.tree() == [
        "Test Cast/2026-10-31 - A.mp3",
        "Test Cast/2026-10-31 - B.mp3",
        "Test Cast/folder.jpg",
    ]
    assert [c for c in world.fetcher.calls if c[0] == "image"] == [
        ("image", "https://cdn.example/c.jpg")
    ]


def test_a_missing_cover_does_not_fail_the_episode(world):
    world.fetcher.files["https://cdn.example/a.mp3"] = b"ID3a"
    job = world.queue("https://cdn.example/a.mp3", cover="https://cdn.example/gone.jpg")
    world.worker().run_one()
    assert world.status(job.id) == ("done", None)
    assert world.tree() == ["Test Cast/2026-10-31 - Pilot.mp3"]


@pytest.mark.parametrize(
    ("error", "expect"),
    [
        (
            FetchError(
                "blocked", "this address points into a private or local network and is refused"
            ),
            "private or local network",
        ),
        (FetchError("too_large", "the file is larger than 2048 MiB"), "larger than"),
        (FetchError("bad_response", "the server sent a web page instead of the file"), "web page"),
        (FetchError("network", "cdn.example could not be reached"), "could not be reached"),
        (FetchError("not_found", "cdn.example answered: not found (HTTP 404)"), "not found"),
    ],
)
def test_fetch_failures_become_readable_job_errors(world, error, expect):
    world.fetcher.files["https://cdn.example/new.mp3"] = error
    job = world.queue()
    world.worker().run_one()
    status, reason = world.status(job.id)
    assert status == "failed"
    assert expect in reason
    assert world.tree() == []
    assert not (world.tmp / "downloads/incomplete/podcasts" / job.external_id).exists()


def test_a_failed_job_is_retried_by_queueing_it_again(world):
    world.fetcher.files["https://cdn.example/new.mp3"] = FetchError("network", "down")
    job = world.queue()
    world.worker().run_one()
    assert world.status(job.id)[0] == "failed"
    world.fetcher.files["https://cdn.example/new.mp3"] = b"ID3ok"
    _, outcome = jobs.submit_job(
        world.db,
        JOB_TYPE,
        job.external_id,
        job.metadata and PodcastJobMetadata.model_validate(job.metadata).model_dump_json(),
    )
    assert outcome == "requeued"
    world.worker().run_one()
    assert world.status(job.id) == ("done", None)


def test_an_unknown_file_type_fails_instead_of_guessing(world):
    world.fetcher.files["https://cdn.example/get?id=1"] = b"ID3x"
    world.fetcher.content_type = "application/octet-stream"
    job = world.queue("https://cdn.example/get?id=1")
    world.worker().run_one()
    status, reason = world.status(job.id)
    assert status == "failed"
    assert "not recognised" in reason
    assert world.tree() == []


def test_the_type_can_come_from_the_server_or_the_feed(world):
    world.fetcher.files["https://cdn.example/get?id=1"] = b"ID3x"
    world.fetcher.content_type = "audio/mp4"
    job = world.queue("https://cdn.example/get?id=1")
    world.worker().run_one()
    assert world.status(job.id)[0] == "done"
    assert world.tree() == ["Test Cast/2026-10-31 - Pilot.m4a"]


def test_the_extension_of_the_final_address_counts_after_a_redirect(world):
    world.fetcher.files["https://track.example/r/1"] = b"ID3x"
    world.fetcher.final_url = "https://cdn.example/real.ogg"
    world.fetcher.content_type = "application/octet-stream"
    job = world.queue("https://track.example/r/1")
    world.worker().run_one()
    assert world.status(job.id)[0] == "done"
    assert world.tree() == ["Test Cast/2026-10-31 - Pilot.ogg"]


def test_a_missing_library_folder_fails_with_the_fix(world):
    (world.tmp / "Podcasts").rmdir()
    job = world.queue()
    world.worker().run_one()
    status, reason = world.status(job.id)
    assert status == "failed"
    assert "make layout" in reason


def test_too_little_free_space_fails_before_downloading(world):
    job = world.queue()
    world.worker(min_free_bytes=10**18).run_one()
    status, reason = world.status(job.id)
    assert status == "failed"
    assert "free space" in reason
    assert world.fetcher.calls == []


def test_stored_data_that_is_no_longer_valid_fails_clearly(world):
    with world.db.transaction() as conn:
        conn.execute(
            'INSERT INTO jobs (type, external_id, metadata) VALUES (\'podcast\', \'x\', \'{"type":"podcast","podcast":"../x"}\')'
        )
    world.worker().run_one()
    (job,) = jobs.list_jobs(world.db)
    assert job.status == "failed"
    assert "pick the episode again" in job.error
    assert world.fetcher.calls == []


@pytest.mark.security
def test_a_hostile_stored_address_is_not_trusted(world):
    """The address is fetched by SafeFetcher, which refuses it; the worker adds no shortcut."""
    world.fetcher.files["http://192.168.178.1/admin.mp3"] = FetchError("blocked", "refused")
    job = world.queue("http://192.168.178.1/admin.mp3")
    world.worker().run_one()
    assert world.status(job.id)[0] == "failed"
    assert world.tree() == []


def test_a_crash_in_one_job_fails_only_that_job(world, monkeypatch):
    world.fetcher.files["https://cdn.example/a.mp3"] = b"ID3a"
    world.fetcher.files["https://cdn.example/b.mp3"] = b"ID3b"
    first = world.queue("https://cdn.example/a.mp3", "A", key="a" * 40)
    second = world.queue("https://cdn.example/b.mp3", "B", key="b" * 40)
    real = world.fetcher.download
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(world.fetcher, "download", flaky)
    worker = world.worker()
    worker.run_one()
    worker.run_one()
    assert world.status(first.id) == ("failed", "the download failed: RuntimeError")
    assert world.status(second.id) == ("done", None)


def test_after_a_restart_interrupted_jobs_run_again(world):
    world.fetcher.files["https://cdn.example/new.mp3"] = b"ID3x"
    job = world.queue()
    jobs.claim_next_queued(world.db, JOB_TYPE)  # was "downloading" when the app stopped
    stop = threading.Event()
    worker = world.worker(idle_seconds=0.05)
    thread = threading.Thread(target=worker.run, args=(stop,))
    thread.start()
    for _ in range(100):
        if world.status(job.id)[0] == "done":
            break
        threading.Event().wait(0.05)
    stop.set()
    thread.join(5)
    assert world.status(job.id) == ("done", None)


def test_progress_is_reported_while_downloading_and_forgotten_after(world):
    world.fetcher.files["https://cdn.example/new.mp3"] = b"ID3" * 100
    job = world.queue()
    worker = world.worker()
    seen = {}
    real = world.fetcher.download

    def watching(*args, **kwargs):
        result = real(*args, **kwargs)
        seen.update(worker.progress)
        return result

    world.fetcher.download = watching  # type: ignore[method-assign]
    worker.run_one()
    assert seen == {job.id: 100}
    assert worker.progress == {}


def test_an_interrupted_filing_leaves_no_second_copy(world):
    """The file is already in the library (the app stopped right after filing): same size, no (2)."""
    world.fetcher.files["https://cdn.example/new.mp3"] = b"ID3same"
    folder = world.tmp / "Podcasts/Test Cast"
    folder.mkdir()
    (folder / "2026-10-31 - Pilot.mp3").write_bytes(b"ID3same")
    job = world.queue()
    world.worker().run_one()
    assert world.status(job.id)[0] == "done"
    assert world.tree() == ["Test Cast/2026-10-31 - Pilot.mp3"]


# --- the poller ---------------------------------------------------------------------------------


def feed_of(*items):
    return rss(*items)


def pub(days):
    return (NOW + timedelta(days=days)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def episode_item(guid, title, days, url=None):
    from tests.unit.test_podcasts import item

    return item(guid, title, pub(days), url or f"https://cdn.example/{guid}.mp3")


class FakeWorker:
    def __init__(self):
        self.woken = 0

    def wake(self):
        self.woken += 1


def make_poller(world, worker=None):
    poller = PodcastPoller(
        world.db,
        world.feeds,
        worker or FakeWorker(),
        poll_minutes=240,
        now=lambda: NOW,
        sleep=lambda s: None,
        rng=random.Random(1),
    )
    return poller


def subscribe(world, **values):
    sub, _ = podcast_subscriptions.add(
        world.db, FEED, values.pop("title", "Test Cast"), values.pop("backfill", 0)
    )
    with world.db.transaction() as conn:
        conn.execute(
            "UPDATE podcast_subscriptions SET created_at = ? WHERE id = ?", (SUBSCRIBED, sub.id)
        )
    return podcast_subscriptions.get(world.db, sub.id)


def test_new_episodes_are_queued_with_the_subscription_s_folder(world):
    sub = subscribe(world, title="My Cast")
    world.fetcher.feeds[FEED] = feed_of(
        episode_item("new1", "Fresh", -1), episode_item("old1", "Old", -30)
    )
    worker = FakeWorker()
    assert make_poller(world, worker).poll(sub) == 1
    (job,) = jobs.list_jobs(world.db)
    assert job.type == JOB_TYPE
    assert job.external_id == episode_key(FEED, "new1")
    assert job.metadata["podcast"] == "My Cast"
    assert job.metadata["title"] == "Fresh"
    assert job.metadata["published"] == (NOW - timedelta(days=1)).date().isoformat()
    assert job.metadata["enclosure_url"] == "https://cdn.example/new1.mp3"
    after = podcast_subscriptions.get(world.db, sub.id)
    assert after.last_error is None
    assert after.last_checked == "2026-11-01T12:00:00.000Z"


def test_nothing_older_than_the_subscription_is_taken_unless_backfill_says_so(world):
    sub = subscribe(world, backfill=2)
    world.fetcher.feeds[FEED] = feed_of(
        episode_item("n", "N", -1),
        episode_item("o1", "O1", -15),
        episode_item("o2", "O2", -20),
        episode_item("o3", "O3", -25),
    )
    assert make_poller(world).poll(sub) == 3
    assert {j.external_id for j in jobs.list_jobs(world.db)} == {
        episode_key(FEED, g) for g in ("n", "o1", "o2")
    }
    # a later poll does not backfill again
    sub = podcast_subscriptions.get(world.db, sub.id)
    assert make_poller(world).poll(sub) == 0


def test_an_episode_picked_by_hand_is_not_queued_again_by_the_poller(world):
    sub = subscribe(world)
    world.fetcher.feeds[FEED] = feed_of(episode_item("n", "N", -1))
    podcast = parse_podcast(world.fetcher.feeds[FEED], FEED)
    metadata = episode_metadata("Test Cast", podcast.episodes[0], FEED, None)
    jobs.submit_job(world.db, JOB_TYPE, podcast.episodes[0].key, metadata.model_dump_json())
    assert make_poller(world).poll(sub) == 0
    assert len(jobs.list_jobs(world.db)) == 1


def test_a_failed_episode_is_not_requeued_by_every_poll(world):
    sub = subscribe(world)
    world.fetcher.feeds[FEED] = feed_of(episode_item("n", "N", -1))
    poller = make_poller(world)
    poller.poll(sub)
    (job,) = jobs.list_jobs(world.db)
    jobs.mark_failed(world.db, job.id, "boom")
    assert poller.poll(podcast_subscriptions.get(world.db, sub.id)) == 0
    assert jobs.get_job(world.db, job.id).status == "failed"


def test_a_feed_error_is_recorded_and_raised(world):
    sub = subscribe(world)
    world.fetcher.feeds[FEED] = FetchError("network", "podcast.example could not be reached")
    with pytest.raises(PodcastError):
        make_poller(world).poll(sub)
    after = podcast_subscriptions.get(world.db, sub.id)
    assert "could not be reached" in after.last_error
    assert after.last_checked is not None


def test_a_bad_feed_is_recorded(world):
    sub = subscribe(world)
    world.fetcher.feeds[FEED] = b"<html>sign in</html>"
    with pytest.raises(PodcastError) as error:
        make_poller(world).poll(sub)
    assert error.value.kind == "bad_feed"
    assert podcast_subscriptions.get(world.db, sub.id).last_error


def test_due_feeds_are_polled_and_the_worker_is_woken_once(world):
    subscribe(world)
    world.fetcher.feeds[FEED] = feed_of(episode_item("n", "N", -1), episode_item("m", "M", -2))
    worker = FakeWorker()
    result = make_poller(world, worker).run_once()
    assert (result.polled, result.queued, result.failed) == (1, 2, 0)
    assert worker.woken == 1


def test_a_feed_that_was_just_checked_is_not_due_before_its_own_interval(world):
    sub = subscribe(world)
    podcast_subscriptions.record_poll(world.db, sub.id, None, NOW - timedelta(hours=1))
    assert make_poller(world).due() == []
    podcast_subscriptions.record_poll(world.db, sub.id, None, NOW - timedelta(hours=6))
    assert len(make_poller(world).due()) == 1


def test_after_an_error_the_feed_is_tried_again_soon(world):
    sub = subscribe(world)
    podcast_subscriptions.record_poll(world.db, sub.id, "down", NOW - timedelta(minutes=11))
    assert len(make_poller(world).due()) == 1


def test_a_down_connection_stops_the_run_after_the_first_failure(world):
    for i in range(3):
        podcast_subscriptions.add(world.db, f"https://p{i}.example/feed", f"P{i}")
    for i in range(3):
        world.fetcher.feeds[f"https://p{i}.example/feed"] = FetchError("network", "unreachable")
    result = make_poller(world).run_once()
    assert result.failed == 1
    assert len([c for c in world.fetcher.calls if c[0] == "feed"]) == 1


def test_one_crashing_feed_does_not_stop_the_others(world, monkeypatch, caplog):
    podcast_subscriptions.add(world.db, "https://a.example/feed", "A")
    podcast_subscriptions.add(world.db, "https://b.example/feed", "B")
    world.fetcher.feeds["https://b.example/feed"] = feed_of(episode_item("n", "N", 0.1))
    real = world.feeds.load

    def load(url):
        if "a.example" in url:
            raise RuntimeError("bug")
        return real(url)

    monkeypatch.setattr(world.feeds, "load", load)
    with caplog.at_level(logging.ERROR):
        result = make_poller(world).run_once()
    assert (result.polled, result.failed, result.queued) == (2, 1, 1)
    errors = {s.title: s.last_error for s in podcast_subscriptions.list_all(world.db)}
    assert errors["A"] == "internal error while polling"
    assert errors["B"] is None


def test_the_manual_poll_works_whatever_the_interval_and_only_one_at_a_time(world):
    sub = subscribe(world)
    podcast_subscriptions.record_poll(world.db, sub.id, None, NOW)
    world.fetcher.feeds[FEED] = feed_of(episode_item("n", "N", -1))
    worker = FakeWorker()
    poller = make_poller(world, worker)
    assert poller.poll_now(sub) == 1
    assert worker.woken == 1
