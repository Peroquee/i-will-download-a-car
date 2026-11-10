"""POST /api/subscriptions/{id}/poll, the "check now" button (issue #27)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.helpers.fake_probe import FakeTraitsProbe
from tests.unit.test_feeds import entry, feed
from ytp import jobs, subscriptions
from ytp.config import Settings
from ytp.feeds import FeedClient, FeedError
from ytp.main import create_app

CID = "UCSMOQeBJ2RAnuFungnQOxLg"
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hb",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


class FakeFeeds(FeedClient):
    def __init__(self):
        super().__init__()
        self.answer: bytes | FeedError | Exception = feed()
        self.gate: threading.Event | None = None

    def fetch(self, channel_id):
        if self.gate is not None:
            self.gate.wait(5)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    yield from running(tmp_path, "0")


def running(tmp_path: Path, minutes: str) -> Iterator[TestClient]:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {
        **ENV,
        "DATA_ROOT": str(tmp_path),
        "CONFIG_DIR": str(config),
        "SUBSCRIPTION_POLL_MINUTES": minutes,
    }
    test_client = TestClient(create_app(Settings.from_env(values)))
    test_client.__enter__()
    try:
        test_client.app.state.poller.feeds = FakeFeeds()
        test_client.app.state.poller.probe = FakeTraitsProbe()  # never the real YouTube
        test_client.app.state.poller.lister = None  # the RSS feed alone (the fake above)
        yield test_client
    finally:
        test_client.__exit__(None, None, None)


def subscribe(client) -> dict:
    db = client.app.state.db
    sub, _ = subscriptions.add(db, CID, "Blender")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET created_at = '2026-10-01T00:00:00.000Z' WHERE id = ?",
            (sub.id,),
        )
    return sub


def fresh_entry(video_id="aaaaaaaaaaa", title="New video"):
    return entry(video_id, title, (datetime.now(UTC) - timedelta(hours=1)).isoformat())


def test_check_now_queues_new_videos_at_once_and_updates_last_checked(client):
    sub = subscribe(client)
    client.app.state.poller.feeds.answer = feed(fresh_entry(), fresh_entry("bbbbbbbbbbb", "Two"))
    response = client.post(f"/api/subscriptions/{sub.id}/poll")
    assert response.status_code == 200
    body = response.json()
    assert body["queued"] == 2
    assert body["id"] == sub.id
    assert body["last_checked"] is not None
    assert body["last_error"] is None
    queued = jobs.list_jobs(client.app.state.db)
    assert {j.external_id for j in queued} == {"aaaaaaaaaaa", "bbbbbbbbbbb"}
    assert {j.metadata["channel"] for j in queued} == {"Blender"}


def test_check_now_works_when_the_schedule_is_off(client):
    assert client.app.state.settings.subscription_poll_minutes == 0
    # the job always runs (#79): an admin can switch polling on later; it finds nothing due
    assert "subscription-poll" in client.app.state.scheduler.job_ids()
    assert client.app.state.poller.due() == []
    sub = subscribe(client)
    assert client.post(f"/api/subscriptions/{sub.id}/poll").status_code == 200


def test_check_now_with_nothing_new_queues_nothing(client):
    sub = subscribe(client)
    assert client.post(f"/api/subscriptions/{sub.id}/poll").json()["queued"] == 0


def test_check_now_ignores_the_interval_but_not_the_dedupe(client):
    sub = subscribe(client)
    client.app.state.poller.feeds.answer = feed(fresh_entry())
    assert client.post(f"/api/subscriptions/{sub.id}/poll").json()["queued"] == 1
    assert client.post(f"/api/subscriptions/{sub.id}/poll").json()["queued"] == 0


def test_a_failed_check_reports_the_reason_and_records_it(client):
    sub = subscribe(client)
    client.app.state.poller.feeds.answer = FeedError("network", "the feed could not be fetched")
    response = client.post(f"/api/subscriptions/{sub.id}/poll")
    assert response.status_code == 502
    assert response.json() == {"detail": "the feed could not be fetched", "kind": "network"}
    listed = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert listed["last_error"] == "the feed could not be fetched"
    assert listed["last_checked"] is not None


@pytest.mark.parametrize(
    ("kind", "status"),
    [("network", 502), ("not_found", 502), ("bad_feed", 502), ("temporary", 503)],
)
def test_feed_error_kinds_map_to_statuses(client, kind, status):
    sub = subscribe(client)
    client.app.state.poller.feeds.answer = FeedError(kind, "nope")
    assert client.post(f"/api/subscriptions/{sub.id}/poll").status_code == status


def test_an_unexpected_failure_is_a_500_with_a_recorded_reason(client):
    sub = subscribe(client)
    client.app.state.poller.feeds.answer = RuntimeError("bug with /secret/path")
    response = client.post(f"/api/subscriptions/{sub.id}/poll")
    assert response.status_code == 500
    assert "secret" not in response.text
    listed = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert listed["last_error"] == "internal error while polling"


def test_unknown_subscription_is_a_404(client):
    assert client.post("/api/subscriptions/999/poll").status_code == 404
    assert client.post("/api/subscriptions/abc/poll").status_code == 422


def test_only_one_manual_check_runs_at_a_time(client):
    sub = subscribe(client)
    feeds = client.app.state.poller.feeds
    feeds.gate = threading.Event()
    result: dict = {}
    first = threading.Thread(
        target=lambda: result.update(r=client.post(f"/api/subscriptions/{sub.id}/poll"))
    )
    first.start()
    try:
        deadline = 50
        while deadline and client.app.state.poller._manual.acquire(blocking=False):
            client.app.state.poller._manual.release()
            threading.Event().wait(0.02)
            deadline -= 1
        assert client.post(f"/api/subscriptions/{sub.id}/poll").status_code == 429
    finally:
        feeds.gate.set()
        first.join(5)
    assert result["r"].status_code == 200


def test_a_get_is_not_allowed(client):
    assert client.get("/api/subscriptions/1/poll").status_code == 405


@pytest.mark.security
def test_cross_site_check_now_is_refused_and_does_nothing(client):
    sub = subscribe(client)
    client.app.state.poller.feeds.answer = feed(fresh_entry())
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"}):
        assert client.post(f"/api/subscriptions/{sub.id}/poll", headers=headers).status_code == 403
    assert jobs.list_jobs(client.app.state.db) == []
    assert subscriptions.get(client.app.state.db, sub.id).last_checked is None


def test_without_a_database_it_is_a_503(tmp_path):
    app = create_app(Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path)}))
    assert TestClient(app).post("/api/subscriptions/1/poll").status_code == 503


def test_the_subscription_list_shows_what_was_skipped(client):
    from ytp import skipped_videos

    sub = subscribe(client)
    db = client.app.state.db
    skipped_videos.record(
        db, sub.id, "aaaaaaaaaaa", "short", "A Short", "2026-10-31T00:00:00+00:00"
    )
    skipped_videos.record(db, sub.id, "bbbbbbbbbbb", "live", "Live now", None)
    row = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert row["skipped"]["count"] == 2
    assert row["skipped"]["by_reason"] == {"live": 1, "premiere": 0, "short": 1}
    assert [r["video_id"] for r in row["skipped"]["recent"]] == ["bbbbbbbbbbb", "aaaaaaaaaaa"]


def test_check_now_skips_a_live_stream_and_says_nothing_failed(client):
    from ytp.video_filter import VideoTraits

    sub = subscribe(client)
    client.app.state.poller.feeds.answer = feed(
        fresh_entry("aaaaaaaaaaa"), fresh_entry("bbbbbbbbbbb")
    )
    client.app.state.poller.probe.answers["aaaaaaaaaaa"] = VideoTraits(live_status="is_live")
    reply = client.post(f"/api/subscriptions/{sub.id}/poll")
    assert reply.status_code == 200
    assert reply.json()["queued"] == 1
    assert reply.json()["last_error"] is None
    row = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert row["skipped"]["by_reason"]["live"] == 1
