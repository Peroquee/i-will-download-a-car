"""The podcast API (issue #34): probe, one episode, subscriptions, check now."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from tests.unit.test_podcast_jobs import ENV, FakeFetcher, episode_item, feed_of
from tests.unit.test_podcasts import item
from ytp import jobs, podcast_subscriptions
from ytp.config import Settings
from ytp.main import create_app
from ytp.podcast_jobs import PodcastWorker
from ytp.podcasts import PodcastFeedClient, episode_key
from ytp.safefetch import FetchError, SafeFetcher

FEED = "https://podcast.example/feed.xml"


@pytest.fixture(autouse=True)
def no_background_worker(monkeypatch):
    """The API tests look at the queue; the worker thread must not empty it meanwhile."""
    monkeypatch.setattr(PodcastWorker, "run", lambda self, stop: None)


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    config = tmp_path / "config"
    config.mkdir()
    (tmp_path / "Podcasts").mkdir()
    settings = Settings.from_env(
        {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), "PODCAST_POLL_MINUTES": "0"}
    )
    test_client = TestClient(create_app(settings), base_url="http://nas.lan")
    test_client.__enter__()
    fetcher = FakeFetcher()
    feeds = PodcastFeedClient(fetcher)
    test_client.fetcher = fetcher  # type: ignore[attr-defined]
    test_client.app.state.podcast_feeds = feeds
    test_client.app.state.podcast_worker.feeds = feeds
    test_client.app.state.podcast_poller.feeds = feeds
    try:
        yield test_client
    finally:
        test_client.__exit__(None, None, None)


# --- probe ----------------------------------------------------------------------------------------


def test_probe_lists_the_episodes_newest_first(client):
    client.fetcher.feeds[FEED] = feed_of(
        episode_item("old", "Old one", -10), episode_item("new", "New one", -1)
    )
    response = client.post("/api/podcasts/probe", json={"feed": FEED})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Test Cast"
    assert body["folder"] == "Test Cast"
    assert body["subscribed"] is False
    assert [e["title"] for e in body["episodes"]] == ["New one", "Old one"]
    assert body["episodes"][0]["key"] == episode_key(FEED, "new")
    assert set(body["episodes"][0]) == {"key", "title", "published", "size", "media_type"}
    assert (
        "enclosure_url" not in response.text
    )  # the browser never gets a file address to send back


def test_probe_shows_at_most_fifty_episodes(client):
    client.fetcher.feeds[FEED] = feed_of(
        *[episode_item(f"g{i}", f"E{i}", -i - 1) for i in range(80)]
    )
    assert len(client.post("/api/podcasts/probe", json={"feed": FEED}).json()["episodes"]) == 50


def test_probe_knows_a_subscribed_feed_and_its_folder(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "A", -1))
    sub, _ = podcast_subscriptions.add(client.app.state.db, FEED, "My Folder")
    body = client.post("/api/podcasts/probe", json={"feed": FEED}).json()
    assert body["subscribed"] is True
    assert body["subscription_id"] == sub.id
    assert body["folder"] == "My Folder"


def test_probe_stores_nothing(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "A", -1))
    client.post("/api/podcasts/probe", json={"feed": FEED})
    assert jobs.list_jobs(client.app.state.db) == []
    assert podcast_subscriptions.list_all(client.app.state.db) == []


@pytest.mark.parametrize(
    "feed",
    [
        "",
        "not a link",
        "ftp://x.example/f",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://u:p@x.example/f",
        "https://x.example:22/f",
        "x" * 3000,
    ],
)
def test_probe_rejects_unusable_addresses_as_a_field_error(client, feed):
    response = client.post("/api/podcasts/probe", json={"feed": feed})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "feed"]
    assert client.fetcher.calls == []


def test_probe_rejects_unknown_fields(client):
    assert (
        client.post("/api/podcasts/probe", json={"feed": FEED, "enclosure": "x"}).status_code == 422
    )


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (FetchError("not_found", "gone"), 404, "feed_not_found"),
        (FetchError("network", "down"), 502, "feed_network"),
        (FetchError("temporary", "later"), 503, "feed_temporary"),
        (FetchError("bad_response", "odd"), 502, "feed_bad_response"),
        (FetchError("too_large", "big"), 502, "feed_too_large"),
        (FetchError("blocked", "private"), 403, "feed_blocked"),
    ],
)
def test_feed_failures_carry_a_kind(client, error, status, kind):
    client.fetcher.feeds[FEED] = error
    response = client.post("/api/podcasts/probe", json={"feed": FEED})
    assert response.status_code == status
    assert response.json() == {"detail": error.message, "kind": kind}


def test_a_feed_that_is_not_rss_is_feed_bad(client):
    client.fetcher.feeds[FEED] = b"<html>login</html>"
    response = client.post("/api/podcasts/probe", json={"feed": FEED})
    assert (response.status_code, response.json()["kind"]) == (502, "feed_bad")


# --- one episode ----------------------------------------------------------------------------------


def test_one_episode_is_queued_by_its_key(client):
    client.fetcher.feeds[FEED] = feed_of(
        episode_item("a", "First", -2), episode_item("b", "Second", -1)
    )
    response = client.post(
        "/api/podcasts/episode", json={"feed": FEED, "key": episode_key(FEED, "a")}
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["type"], body["status"], body["duplicate"]) == ("podcast", "queued", False)
    (job,) = jobs.list_jobs(client.app.state.db)
    assert job.external_id == episode_key(FEED, "a")
    assert job.metadata["podcast"] == "Test Cast"
    assert job.metadata["title"] == "First"
    assert job.metadata["enclosure_url"] == "https://cdn.example/a.mp3"


def test_the_same_episode_twice_is_one_job(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    payload = {"feed": FEED, "key": episode_key(FEED, "a")}
    first = client.post("/api/podcasts/episode", json=payload)
    second = client.post("/api/podcasts/episode", json=payload)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]


def test_a_failed_episode_is_queued_again(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    payload = {"feed": FEED, "key": episode_key(FEED, "a")}
    job_id = client.post("/api/podcasts/episode", json=payload).json()["id"]
    jobs.mark_failed(client.app.state.db, job_id, "boom")
    again = client.post("/api/podcasts/episode", json=payload)
    assert again.status_code == 201 or again.json()["duplicate"] is False
    assert jobs.get_job(client.app.state.db, job_id).status == "queued"


def test_an_episode_uses_the_subscription_folder_so_everything_shares_it(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    podcast_subscriptions.add(client.app.state.db, FEED, "Shared Folder")
    client.post(
        "/api/podcasts/episode", json={"feed": FEED, "key": episode_key(FEED, "a"), "name": "Other"}
    )
    assert jobs.list_jobs(client.app.state.db)[0].metadata["podcast"] == "Shared Folder"


def test_without_a_subscription_the_given_name_is_the_folder(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    client.post(
        "/api/podcasts/episode",
        json={"feed": FEED, "key": episode_key(FEED, "a"), "name": "Mein Ordner"},
    )
    assert jobs.list_jobs(client.app.state.db)[0].metadata["podcast"] == "Mein Ordner"


@pytest.mark.security
def test_an_episode_that_is_not_in_the_feed_is_not_fetched(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    response = client.post("/api/podcasts/episode", json={"feed": FEED, "key": "0" * 40})
    assert response.status_code == 404
    assert jobs.list_jobs(client.app.state.db) == []


@pytest.mark.security
@pytest.mark.parametrize(
    "extra", [{"enclosure_url": "http://192.168.178.1/x.mp3"}, {"url": "http://localhost/x"}]
)
def test_the_browser_cannot_name_a_file_address(client, extra):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    response = client.post(
        "/api/podcasts/episode", json={"feed": FEED, "key": episode_key(FEED, "a"), **extra}
    )
    assert response.status_code == 422
    assert jobs.list_jobs(client.app.state.db) == []


@pytest.mark.parametrize("key", ["", "abc", "g" * 40, "A" * 40, "0" * 41, "../../etc/passwd"])
def test_a_malformed_key_is_a_field_error(client, key):
    response = client.post("/api/podcasts/episode", json={"feed": FEED, "key": key})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "key"]


@pytest.mark.parametrize("name", ["a/b", "..", "", "x" * 201, "bad\nname"])
def test_a_bad_folder_name_is_a_field_error(client, name):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    response = client.post(
        "/api/podcasts/episode", json={"feed": FEED, "key": episode_key(FEED, "a"), "name": name}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "name"]


# --- subscribing ----------------------------------------------------------------------------------


def test_subscribing_stores_the_feed_with_the_title_as_folder(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    response = client.post("/api/podcasts", json={"feed": FEED + "#x"})
    assert response.status_code == 201
    body = response.json()
    assert (body["feed_url"], body["title"], body["backfill"], body["duplicate"], body["kind"]) == (
        FEED,
        "Test Cast",
        0,
        False,
        "podcast",
    )
    assert jobs.list_jobs(client.app.state.db) == [], "subscribing downloads nothing by itself"


def test_a_given_name_and_backfill_are_kept(client):
    client.fetcher.feeds[FEED] = feed_of()
    body = client.post(
        "/api/podcasts", json={"feed": FEED, "name": "Mein Podcast", "backfill": 3}
    ).json()
    assert (body["title"], body["backfill"]) == ("Mein Podcast", 3)


def test_subscribing_twice_changes_nothing(client):
    client.fetcher.feeds[FEED] = feed_of()
    first = client.post("/api/podcasts", json={"feed": FEED, "name": "One"})
    second = client.post("/api/podcasts", json={"feed": FEED, "name": "Two", "backfill": 5})
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["duplicate"] is True
    assert second.json()["title"] == "One"
    assert len(podcast_subscriptions.list_all(client.app.state.db)) == 1


@pytest.mark.parametrize("backfill", [-1, 21, "3", 1.5, True])
def test_backfill_must_be_a_number_from_0_to_20(client, backfill):
    client.fetcher.feeds[FEED] = feed_of()
    response = client.post("/api/podcasts", json={"feed": FEED, "backfill": backfill})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "backfill"]


def test_the_limit_of_subscriptions_is_enforced(client, monkeypatch):
    monkeypatch.setattr(podcast_subscriptions, "MAX_PODCASTS", 1)
    podcast_subscriptions.add(client.app.state.db, "https://other.example/feed", "Other")
    client.fetcher.feeds[FEED] = feed_of()
    assert client.post("/api/podcasts", json={"feed": FEED}).status_code == 409


def test_subscribing_to_an_unreachable_feed_stores_nothing(client):
    client.fetcher.feeds[FEED] = FetchError("network", "down")
    assert client.post("/api/podcasts", json={"feed": FEED}).status_code == 502
    assert podcast_subscriptions.list_all(client.app.state.db) == []


def test_the_list_delete_and_poll(client):
    # published after the subscription is made, so it counts as new (the clock here is real)
    soon = (datetime.now(UTC) + timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    client.fetcher.feeds[FEED] = feed_of(item("a", "Fresh", soon))
    sub = client.post("/api/podcasts", json={"feed": FEED}).json()
    listed = client.get("/api/podcasts").json()["podcasts"]
    assert [p["id"] for p in listed] == [sub["id"]]
    polled = client.post(f"/api/podcasts/{sub['id']}/poll")
    assert polled.status_code == 200
    assert polled.json()["queued"] == 1
    assert polled.json()["last_checked"] is not None
    assert client.delete(f"/api/podcasts/{sub['id']}").status_code == 204
    assert client.get("/api/podcasts").json() == {"podcasts": []}
    assert client.delete(f"/api/podcasts/{sub['id']}").status_code == 404
    assert client.post(f"/api/podcasts/{sub['id']}/poll").status_code == 404
    assert len(jobs.list_jobs(client.app.state.db)) == 1, "removing a subscription keeps its jobs"


def test_a_failed_check_now_shows_the_reason_and_keeps_it(client):
    client.fetcher.feeds[FEED] = feed_of()
    sub = client.post("/api/podcasts", json={"feed": FEED}).json()
    client.fetcher.feeds[FEED] = FetchError("network", "podcast.example could not be reached")
    response = client.post(f"/api/podcasts/{sub['id']}/poll")
    assert (response.status_code, response.json()["kind"]) == (502, "feed_network")
    assert "could not be reached" in client.get("/api/podcasts").json()["podcasts"][0]["last_error"]


# --- the jobs view --------------------------------------------------------------------------------


def test_podcast_jobs_show_in_the_job_list_with_a_readable_label(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    client.post("/api/podcasts/episode", json={"feed": FEED, "key": episode_key(FEED, "a")})
    (listed,) = client.get("/api/jobs").json()["jobs"]
    assert listed["kind"] == "podcast"
    assert listed["label"] == "Test Cast - First"
    assert listed["status"] == "queued"
    assert listed["can_retry"] is False
    assert "enclosure" not in str(listed)


def test_a_failed_podcast_job_can_be_retried_from_the_list(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    job_id = client.post(
        "/api/podcasts/episode", json={"feed": FEED, "key": episode_key(FEED, "a")}
    ).json()["id"]
    jobs.mark_failed(client.app.state.db, job_id, "boom")
    response = client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409


def test_progress_of_a_running_podcast_download_is_shown(client):
    client.fetcher.feeds[FEED] = feed_of(episode_item("a", "First", -2))
    job_id = client.post(
        "/api/podcasts/episode", json={"feed": FEED, "key": episode_key(FEED, "a")}
    ).json()["id"]
    jobs.claim_next_queued(client.app.state.db, "podcast")
    client.app.state.podcast_worker.progress[job_id] = 42
    (listed,) = client.get("/api/jobs").json()["jobs"]
    assert (listed["status"], listed["progress"]) == ("downloading", 42)


# --- security -------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/podcasts/probe", {"feed": FEED}),
        ("post", "/api/podcasts/episode", {"feed": FEED, "key": "0" * 40}),
        ("post", "/api/podcasts", {"feed": FEED}),
        ("post", "/api/podcasts/1/poll", None),
        ("delete", "/api/podcasts/1", None),
    ],
)
def test_every_changing_podcast_route_refuses_other_sites(client, method, path, body):
    client.fetcher.feeds[FEED] = feed_of()
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"}):
        response = getattr(client, method)(
            path, headers=headers, **({"json": body} if body else {})
        )
        assert response.status_code == 403
    assert client.fetcher.calls == [], (
        "a cross-site request must not make the server fetch anything"
    )


@pytest.mark.security
def test_the_real_fetcher_refuses_a_feed_on_the_home_network(tmp_path):
    """End to end with the real SafeFetcher: a feed address that resolves to a LAN host is refused
    with 403, and nothing is connected to."""
    config = tmp_path / "config"
    config.mkdir()
    settings = Settings.from_env(
        {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), "PODCAST_POLL_MINUTES": "0"}
    )
    dialled = []

    def never(target, ip):
        dialled.append(ip)
        raise AssertionError("must not connect")

    def resolve(host, port):
        return {"nas.example": ["192.168.178.1"], "rebind.example": ["93.184.216.34", "10.0.0.7"]}[
            host
        ]

    with TestClient(create_app(settings), base_url="http://nas.lan") as test_client:
        test_client.app.state.podcast_feeds = PodcastFeedClient(
            SafeFetcher(resolver=resolve, dialer=never)
        )
        for feed in (
            "http://nas.example/feed.xml",
            "https://rebind.example/feed.xml",
            "http://192.168.178.1/feed.xml",
            "http://127.0.0.1/feed.xml",
            "http://[::1]/feed.xml",
        ):
            response = test_client.post("/api/podcasts/probe", json={"feed": feed})
            assert response.status_code == 403, feed
            assert response.json()["kind"] == "feed_blocked"
            assert "192.168" not in response.text
        assert dialled == []


def test_a_name_that_does_not_resolve_is_a_network_problem_not_a_crash(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    settings = Settings.from_env(
        {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), "PODCAST_POLL_MINUTES": "0"}
    )

    def resolve(host, port):
        raise socket.gaierror("no such host")

    with TestClient(create_app(settings), base_url="http://nas.lan") as test_client:
        test_client.app.state.podcast_feeds = PodcastFeedClient(SafeFetcher(resolver=resolve))
        response = test_client.post("/api/podcasts/probe", json={"feed": "https://nope.example/f"})
        assert (response.status_code, response.json()["kind"]) == (502, "feed_network")


def test_too_many_lookups_at_once_get_429(client, monkeypatch):
    from ytp import podcasts_api

    taken = [
        podcasts_api._gate.acquire(blocking=False) for _ in range(podcasts_api.PARALLEL_LOOKUPS)
    ]
    try:
        client.fetcher.feeds[FEED] = feed_of()
        assert client.post("/api/podcasts/probe", json={"feed": FEED}).status_code == 429
    finally:
        for got in taken:
            if got:
                podcasts_api._gate.release()
    assert client.post("/api/podcasts/probe", json={"feed": FEED}).status_code == 200


def test_the_settings_for_podcasts_are_validated():
    from ytp.config import ConfigError

    ok = Settings.from_env({**ENV})
    assert (ok.podcast_proxy, ok.podcast_poll_minutes) == ("http://gluetun:8888", 240)
    assert Settings.from_env({**ENV, "PODCAST_PROXY": ""}).podcast_proxy == ""
    assert Settings.from_env({**ENV, "PODCAST_POLL_MINUTES": "0"}).podcast_poll_minutes == 0
    for bad in (
        {"PODCAST_PROXY": "socks5://x:1080"},
        {"PODCAST_PROXY": "gluetun:8888"},
        {"PODCAST_POLL_MINUTES": "5"},
        {"PODCAST_POLL_MINUTES": "x"},
        {"PODCAST_POLL_MINUTES": "2000"},
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({**ENV, **bad})
