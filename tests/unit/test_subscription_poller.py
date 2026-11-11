# ruff: noqa: E501
"""Polling subscribed channels (issue #20): which videos are new, who is due, and the run."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers.app import ENV
from tests.helpers.fakes import FakeFeeds
from tests.helpers.feedxml import CID, entry, feed
from ytp import jobs, subscriptions
from ytp.db import Database
from ytp.feeds import FeedError
from ytp.subscription_poller import (
    MAX_CHANNELS_PER_RUN,
    MAX_CHECKS_PER_MINUTE,
    RETRY_MINUTES,
    SubscriptionPoller,
    interval_for,
    new_entries,
)
from ytp.subscriptions import Subscription
from ytp.youtube_jobs import YoutubeJobMetadata

CID2 = "UCX6OQ3DkcsbYNE6H8uQQuVA"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SUBSCRIBED = "2026-10-20T00:00:00.000Z"


def iso(delta_days: float) -> str:
    return (NOW + timedelta(days=delta_days)).isoformat()


def sub(**values) -> Subscription:
    fields = {
        "id": 1, "channel_id": CID, "channel_name": "Blender", "enabled": True, "backfill": 0,
        "created_at": SUBSCRIBED, "last_checked": None, "last_error": None,
    }  # fmt: skip
    return Subscription(**{**fields, **values})


def make_entries(*specs):
    """specs: (video_id, days relative to NOW); returned newest first like parse_feed does."""
    from ytp.feeds import FeedEntry

    items = [FeedEntry(v, f"title {v}", NOW + timedelta(days=d)) for v, d in specs]
    return sorted(items, key=lambda e: e.published, reverse=True)


# --- which videos are new ---------------------------------------------------------------------------------


def test_only_videos_after_subscribing_are_taken_oldest_first():
    entries = make_entries(("a" * 11, -1), ("b" * 11, -5), ("c" * 11, -30), ("d" * 11, -2))
    got = new_entries(sub(), entries)
    assert [e.video_id for e in got] == ["b" * 11, "d" * 11, "a" * 11]


def test_the_back_catalogue_is_never_taken_by_default():
    entries = make_entries(("a" * 11, -60), ("b" * 11, -90))
    assert new_entries(sub(), entries) == []


def test_backfill_adds_the_newest_older_videos_on_the_first_poll_only():
    entries = make_entries(
        ("n" * 11, -1), ("o1" + "x" * 9, -25), ("o2" + "x" * 9, -40), ("o3" + "x" * 9, -50)
    )
    first = new_entries(sub(backfill=2), entries)
    assert [e.video_id for e in first] == ["o2" + "x" * 9, "o1" + "x" * 9, "n" * 11]
    later = new_entries(sub(backfill=2, last_checked=iso(-0.1)), entries)
    assert [e.video_id for e in later] == ["n" * 11]


def test_a_video_exactly_at_the_subscription_time_is_not_new():
    entries = make_entries(("a" * 11, -12))  # NOW - 12 days = 2026-10-20T12:00, after SUBSCRIBED
    assert len(new_entries(sub(created_at="2026-10-20T12:00:00.000Z"), entries)) == 0


# --- who is due ---------------------------------------------------------------------------------------------


def test_intervals_differ_per_channel_within_20_percent_and_are_stable():
    ids = [f"UC{i:022d}" for i in range(200)]
    minutes = {c: interval_for(sub(channel_id=c), 60).total_seconds() / 60 for c in ids}
    assert all(48 <= m <= 72 for m in minutes.values())
    assert len({round(m) for m in minutes.values()}) > 10, "they are spread, not all the same"
    assert interval_for(sub(channel_id=ids[3]), 60) == interval_for(sub(channel_id=ids[3]), 60)


def test_after_an_error_the_retry_is_sooner():
    assert interval_for(sub(last_error="down"), 60) == timedelta(minutes=RETRY_MINUTES)
    assert interval_for(sub(last_error="down"), 15) == timedelta(minutes=RETRY_MINUTES)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


class FakeWorker:
    def __init__(self):
        self.woken = 0

    def wake(self):
        self.woken += 1


def make_poller(db, feeds=None, **kwargs):
    sleeps: list[float] = []
    poller = SubscriptionPoller(
        db,
        feeds or FakeFeeds(),
        kwargs.pop("worker", FakeWorker()),
        poll_minutes=60,
        now=lambda: NOW,
        sleep=sleeps.append,
        rng=random.Random(1),
        **kwargs,
    )
    poller.sleeps = sleeps
    return poller


def add(db, channel_id=CID, name="Blender", checked=None, error=None, created=SUBSCRIBED, **kw):
    s, _ = subscriptions.add(db, channel_id, name, **kw)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET created_at = ?, last_checked = ?, last_error = ? WHERE id = ?",
            (created, checked, error, s.id),
        )
    return subscriptions.get(db, s.id)


def test_never_checked_channels_are_due_first_then_the_longest_ago(db):
    a = add(db, "UC" + "a" * 22, "A", checked=iso(-1))
    b = add(db, "UC" + "b" * 22, "B", checked=None)
    c = add(db, "UC" + "c" * 22, "C", checked=iso(-3))
    assert [s.id for s in make_poller(db).due()] == [b.id, c.id, a.id]


def test_recently_checked_and_disabled_channels_are_not_due(db):
    add(db, "UC" + "a" * 22, checked=(NOW - timedelta(minutes=10)).isoformat())
    off = add(db, "UC" + "b" * 22)
    with db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET enabled = 0 WHERE id = ?", (off.id,))
    assert make_poller(db).due() == []


def test_a_channel_with_an_error_is_due_again_after_ten_minutes(db):
    add(db, checked=(NOW - timedelta(minutes=11)).isoformat(), error="down")
    assert len(make_poller(db).due()) == 1
    add(db, CID2, checked=(NOW - timedelta(minutes=9)).isoformat(), error="down")
    assert len(make_poller(db).due()) == 1


# --- a run --------------------------------------------------------------------------------------------------


NEW = entry("aaaaaaaaaaa", "A/B: the “new” one", iso(-1))
NEW2 = entry("bbbbbbbbbbb", "Second", iso(-0.5))
OLD = entry("ccccccccccc", "Old", iso(-40))


def queued(db):
    return [(j.external_id, j.status, j.metadata) for j in jobs.list_jobs(db, limit=100)]


def test_new_videos_are_queued_as_youtube_jobs_the_worker_can_run(db):
    sub_ = add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW, NEW2, OLD)
    worker = FakeWorker()
    result = make_poller(db, feeds, worker=worker).run_once()
    assert (result.polled, result.queued, result.failed) == (1, 2, 0)
    rows = {ext: (status, meta) for ext, status, meta in queued(db)}
    assert set(rows) == {"aaaaaaaaaaa", "bbbbbbbbbbb"}
    status, meta = rows["aaaaaaaaaaa"]
    assert status == "queued"
    assert meta == {
        "type": "youtube", "video_id": "aaaaaaaaaaa", "channel": "Blender",
        "title": "A-B: the “new” one",
    }  # fmt: skip
    YoutubeJobMetadata.model_validate(meta)
    assert worker.woken == 1, "the worker is woken once for the whole batch"
    stored = subscriptions.get(db, sub_.id)
    assert stored.last_checked is not None
    assert stored.last_error is None


def test_polling_again_queues_nothing_twice(db):
    add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW)
    poller = make_poller(db, feeds)
    assert poller.run_once().queued == 1
    with db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET last_checked = NULL")
    assert poller.run_once().queued == 0
    assert len(queued(db)) == 1


def test_a_video_already_fetched_by_hand_is_not_queued_again(db):
    add(db)
    jobs.submit_job(db, "youtube", "aaaaaaaaaaa", '{"type": "youtube"}')
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW)
    worker = FakeWorker()
    assert make_poller(db, feeds, worker=worker).run_once().queued == 0
    assert worker.woken == 0


def test_a_failed_video_is_not_queued_again_by_the_poller(db):
    add(db)
    job, _ = jobs.submit_job(db, "youtube", "aaaaaaaaaaa", '{"type": "youtube"}')
    jobs.mark_failed(db, job.id, "private video")
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW)
    assert make_poller(db, feeds).run_once().queued == 0
    assert jobs.get_job(db, job.id).status == "failed", "retrying is the user's decision (#26)"


def test_the_first_poll_with_backfill_takes_the_newest_old_videos(db):
    add(db, backfill=1)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW, OLD, entry("ddddddddddd", "Older", iso(-50)))
    assert make_poller(db, feeds).run_once().queued == 2
    assert {ext for ext, _, _ in queued(db)} == {"aaaaaaaaaaa", "ccccccccccc"}


def test_one_failing_channel_does_not_stop_the_others_and_records_why(db):
    bad = add(db, "UC" + "a" * 22, "Bad")
    good = add(db, CID, "Good")
    feeds = FakeFeeds()
    feeds.answers["UC" + "a" * 22] = FeedError("not_found", "YouTube has no feed (HTTP 404)")
    feeds.answers[CID] = feed(NEW)
    result = make_poller(db, feeds).run_once()
    assert (result.polled, result.queued, result.failed) == (2, 1, 1)
    assert subscriptions.get(db, bad.id).last_error == "YouTube has no feed (HTTP 404)"
    assert subscriptions.get(db, bad.id).last_checked is not None
    assert subscriptions.get(db, good.id).last_error is None


def test_an_error_is_cleared_by_the_next_good_poll(db):
    s = add(db, error="old problem", checked=(NOW - timedelta(minutes=30)).isoformat())
    make_poller(db).run_once()
    assert subscriptions.get(db, s.id).last_error is None


def test_when_the_connection_is_down_the_run_stops_after_the_first_failure(db):
    for letter in "abc":
        add(db, "UC" + letter * 22, letter)
    feeds = FakeFeeds()
    for letter in "abc":
        feeds.answers["UC" + letter * 22] = FeedError("network", "the feed could not be fetched")
    result = make_poller(db, feeds).run_once()
    assert len(feeds.fetched) == 1
    assert result.failed == 1
    untouched = [s for s in subscriptions.list_all(db) if s.last_checked is None]
    assert len(untouched) == 2, "the others stay due and are tried at the next run"


def test_an_unexpected_error_in_one_channel_is_contained(db):
    boom = add(db, "UC" + "a" * 22, "Boom")
    add(db, CID, "Fine")

    class Exploding(FakeFeeds):
        def fetch(self, channel_id):
            if channel_id == "UC" + "a" * 22:
                raise RuntimeError("bug")
            return super().fetch(channel_id)

    feeds = Exploding()
    feeds.answers[CID] = feed(NEW)
    result = make_poller(db, feeds).run_once()
    assert result.queued == 1
    assert subscriptions.get(db, boom.id).last_error == "internal error while polling"


def test_channels_are_spread_out_within_a_run(db):
    for letter in "abc":
        add(db, "UC" + letter * 22, letter)
    poller = make_poller(db)
    poller.run_once()
    assert len(poller.sleeps) == 2, "a pause between channels, none before the first"
    assert all(3.0 <= s <= 8.0 for s in poller.sleeps)


def test_a_run_polls_at_most_a_few_channels_and_never_more_than_the_cap_per_minute(db):
    for i in range(MAX_CHANNELS_PER_RUN + 5):
        add(db, f"UC{i:022d}", f"c{i}")
    feeds = FakeFeeds()
    poller = make_poller(db, feeds)
    clock = [NOW]
    poller._now = lambda: clock[0]
    assert MAX_CHECKS_PER_MINUTE <= MAX_CHANNELS_PER_RUN
    assert poller.run_once().polled == MAX_CHECKS_PER_MINUTE
    assert poller.run_once().polled == 0, "the same minute: the cap is used up"
    clock[0] = NOW + timedelta(seconds=61)
    second = poller.run_once().polled
    assert second == min(MAX_CHECKS_PER_MINUTE, MAX_CHANNELS_PER_RUN + 5 - MAX_CHECKS_PER_MINUTE)
    clock[0] = NOW + timedelta(seconds=122)
    assert poller.run_once().polled == MAX_CHANNELS_PER_RUN + 5 - MAX_CHECKS_PER_MINUTE - second


def test_a_run_ends_early_when_the_app_shuts_down(db):
    for letter in "abc":
        add(db, "UC" + letter * 22, letter)
    poller = make_poller(db)
    poller.stop.set()
    assert poller.run_once().polled == 0


def test_a_title_that_is_only_dots_falls_back_to_the_video_id(db):
    add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(entry("aaaaaaaaaaa", "...", iso(-1)))
    make_poller(db, feeds).run_once()
    assert queued(db)[0][2]["title"] == "aaaaaaaaaaa"


def test_the_feed_of_the_wrong_channel_is_an_error_not_a_download(db):
    s = add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW, channel=CID2)
    result = make_poller(db, feeds).run_once()
    assert result.failed == 1
    assert queued(db) == []
    assert "different channel" in subscriptions.get(db, s.id).last_error


def test_the_poller_works_without_a_download_worker(db):
    add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW)
    assert make_poller(db, feeds, worker=None).run_once().queued == 1


def test_queued_metadata_is_plain_json(db):
    add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(NEW)
    make_poller(db, feeds).run_once()
    raw = db.query("SELECT metadata FROM jobs")[0]["metadata"]
    assert json.loads(raw)["video_id"] == "aaaaaaaaaaa"


# --- settings and wiring ---


def test_record_poll_can_take_the_time_from_the_caller(db):
    s = add(db)
    subscriptions.record_poll(db, s.id, None, datetime(2026, 11, 1, 12, 30, 5, 123456, tzinfo=UTC))
    assert subscriptions.get(db, s.id).last_checked == "2026-11-01T12:30:05.123Z"


@pytest.mark.parametrize(("value", "minutes"), [("60", 60), ("15", 15), ("1440", 1440), ("0", 0)])
def test_the_poll_interval_setting(value, minutes):
    from ytp.config import Settings

    env = {**ENV, "SUBSCRIPTION_POLL_MINUTES": value}
    assert Settings.from_env(env).subscription_poll_minutes == minutes


def test_the_poll_interval_defaults_to_an_hour():
    from ytp.config import Settings

    assert Settings.from_env(ENV).subscription_poll_minutes == 60


@pytest.mark.parametrize("value", ["5", "14", "1441", "abc", "-1", "1.5"])
def test_a_bad_poll_interval_is_refused_with_the_name_of_the_setting(value):
    from ytp.config import ConfigError, Settings

    with pytest.raises(ConfigError, match="SUBSCRIPTION_POLL_MINUTES"):
        Settings.from_env({**ENV, "SUBSCRIPTION_POLL_MINUTES": value})


@pytest.mark.parametrize("minutes", ["60", "0"])
def test_the_app_always_schedules_the_poller_and_a_zero_default_pauses_it(tmp_path, minutes):
    from starlette.testclient import TestClient

    from ytp.config import Settings
    from ytp.main import create_app

    config = tmp_path / "config"
    config.mkdir()
    env = {
        **ENV,
        "DATA_ROOT": str(tmp_path),
        "CONFIG_DIR": str(config),
        "SUBSCRIPTION_POLL_MINUTES": minutes,
    }
    app = create_app(Settings.from_env(env))
    with TestClient(app):
        # which channel is due is decided per subscription (#79): 0 pauses, an admin can change it
        assert "subscription-poll" in app.state.scheduler.job_ids()
        assert app.state.poller is not None, "it also serves the check-now button"
        assert app.state.defaults.schedule("channel").minutes == int(minutes)
