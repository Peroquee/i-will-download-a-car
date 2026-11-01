"""app/ytp/clients/jellyfin.py: the client against a fake Jellyfin, and the debounced refresher."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.helpers.fake_jellyfin import (
    API_KEY,
    BOOKS_ID,
    MOVIES_ID,
    SERIES_ID,
    FakeJellyfin,
)
from ytp.clients.jellyfin import (
    JellyfinAuthError,
    JellyfinClient,
    JellyfinRequestError,
    JellyfinUnavailable,
    Library,
    LibraryRefresher,
)
from ytp.config import Settings
from ytp.main import create_app

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


@pytest.fixture
def fake() -> Iterator[FakeJellyfin]:
    server = FakeJellyfin().start()
    yield server
    server.stop()


def client_for(fake: FakeJellyfin, key: str = API_KEY, **kw) -> JellyfinClient:
    return JellyfinClient(fake.url, key, timeout=kw.pop("timeout", 3.0))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- the client ----------------------


def test_ping_needs_no_key_and_returns_the_version(fake):
    assert JellyfinClient(fake.url, "").ping() == "10.10.7"
    assert "authorization" not in fake.seen[0].headers


def test_the_key_travels_in_the_authorization_header_never_in_the_url(fake):
    client = client_for(fake)
    client.refresh_library()
    client.refresh_item(MOVIES_ID)
    client.list_libraries()
    for seen in fake.seen:
        assert seen.headers["authorization"] == f'MediaBrowser Token="{API_KEY}"'
        assert API_KEY not in seen.path + seen.query
        assert "api_key" not in seen.query.lower()


def test_refresh_library_posts_to_library_refresh(fake):
    client_for(fake).refresh_library()
    (call,) = fake.calls("/Library/Refresh")
    assert call.method == "POST"


def test_refresh_item_scans_one_library_recursively(fake):
    client_for(fake).refresh_item(MOVIES_ID)
    (call,) = fake.calls(f"/Items/{MOVIES_ID}/Refresh")
    assert call.method == "POST"
    assert "Recursive=true" in call.query
    assert "ReplaceAllMetadata=false" in call.query


@pytest.mark.parametrize(
    "item", ["", "../Library/Refresh", "abc", "g" * 32, f"{MOVIES_ID}/x", "a" * 31]
)
def test_a_bad_item_id_never_reaches_the_url(fake, item):
    with pytest.raises(ValueError, match="item id"):
        client_for(fake).refresh_item(item)
    assert fake.seen == []


def test_dashed_item_ids_are_accepted(fake):
    guid = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
    fake.libraries[0]["ItemId"] = guid
    client_for(fake).refresh_item(guid)
    assert fake.calls(f"/Items/{guid}/Refresh")


def test_an_unknown_item_is_a_request_error(fake):
    with pytest.raises(JellyfinRequestError) as error:
        client_for(fake).refresh_item("d" * 32)
    assert error.value.status == 404


def test_list_libraries_parses_names_ids_and_locations(fake):
    libraries = client_for(fake).list_libraries()
    assert [lib.name for lib in libraries] == ["Filme", "Serien", "Hörbücher"]
    assert libraries[2].item_id == BOOKS_ID
    assert libraries[2].locations == ("/media/jellyfin/Hörbücher/", "/other/place")


@pytest.mark.parametrize("status", [401, 403])
def test_a_wrong_or_revoked_key_is_an_auth_error_with_the_fix(fake, status):
    fake.status_override = status
    with pytest.raises(JellyfinAuthError, match="API Keys"):
        client_for(fake).refresh_library()


def test_a_key_that_does_not_match_gets_401(fake):
    with pytest.raises(JellyfinAuthError):
        client_for(fake, key="some other key").refresh_library()


def test_no_key_fails_without_any_network_traffic(fake):
    with pytest.raises(JellyfinAuthError, match="JELLYFIN_API_KEY is not set"):
        client_for(fake, key="").refresh_library()
    assert fake.seen == []


def test_a_server_error_is_unavailable(fake):
    fake.status_override = 503
    with pytest.raises(JellyfinUnavailable, match="503"):
        client_for(fake).refresh_library()


def test_unreachable_is_unavailable_for_every_call():
    client = JellyfinClient(f"http://127.0.0.1:{free_port()}", API_KEY, timeout=2)
    for call in (client.ping, client.list_libraries, client.refresh_library):
        with pytest.raises(JellyfinUnavailable, match="cannot reach Jellyfin"):
            call()


def test_a_server_that_never_answers_times_out():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        client = JellyfinClient(f"http://127.0.0.1:{sock.getsockname()[1]}", API_KEY, timeout=0.4)
        started = time.monotonic()
        with pytest.raises(JellyfinUnavailable):
            client.refresh_library()
    assert time.monotonic() - started < 3


@pytest.mark.security
def test_the_key_is_in_no_repr_error_or_log(fake, caplog):
    caplog.set_level(logging.DEBUG)
    assert API_KEY not in repr(client_for(fake))
    fake.status_override = 401
    with pytest.raises(JellyfinAuthError) as error:
        client_for(fake).refresh_library()
    dead = JellyfinClient(f"http://127.0.0.1:{free_port()}", API_KEY, timeout=1)
    with pytest.raises(JellyfinUnavailable) as unavailable:
        dead.refresh_library()
    assert API_KEY not in str(error.value) + str(unavailable.value) + caplog.text


@pytest.mark.parametrize("url", ["", "ftp://x", "jellyfin:8096", "http://x:8096/jellyfin"])
def test_a_bad_base_url_is_refused(url):
    with pytest.raises(ValueError, match="base_url"):
        JellyfinClient(url, API_KEY)


def test_library_folder_matching_ignores_prefix_case_slashes_and_unicode_form():
    import unicodedata

    library = Library("Books", BOOKS_ID, ("/media/jellyfin/Hörbücher/",))
    assert library.has_folder("Hörbücher")
    assert library.has_folder(unicodedata.normalize("NFD", "Hörbücher"))
    assert library.has_folder("hörbücher")
    assert not library.has_folder("Musik")
    assert not library.has_folder("jellyfin")  # a parent folder is not the library folder
    assert Library("W", "x", ("C:\\media\\Filme",)).has_folder("Filme")


# --- the refresher: debounce ----------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def refresher(fake: FakeJellyfin, clock: Clock, **kw) -> LibraryRefresher:
    return LibraryRefresher(client_for(fake), clock=clock, **kw)


def test_a_burst_of_requests_becomes_one_refresh_after_the_quiet_period(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock, quiet_seconds=30)
    for _ in range(50):
        refresher_.request_refresh("Filme")
        clock.now += 0.1
    assert refresher_.flush_due() is False, "not quiet yet"
    clock.now += 29
    assert refresher_.flush_due() is False
    clock.now += 2
    assert refresher_.flush_due() is True
    assert len(fake.calls(f"/Items/{MOVIES_ID}/Refresh")) == 1
    assert fake.calls("/Library/Refresh") == []
    assert refresher_.flush_due() is False, "nothing pending any more"
    assert not refresher_.pending


def test_every_new_request_restarts_the_quiet_period(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock, quiet_seconds=30, max_wait_seconds=600)
    refresher_.request_refresh("Filme")
    clock.now += 25
    refresher_.request_refresh("Filme")
    clock.now += 25
    assert refresher_.flush_due() is False
    clock.now += 6
    assert refresher_.flush_due() is True


def test_a_steady_trickle_is_still_refreshed_after_the_maximum_wait(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock, quiet_seconds=30, max_wait_seconds=120)
    for _ in range(30):
        refresher_.request_refresh("Serien")
        clock.now += 10  # never quiet for 30 s
        if refresher_.flush_due():
            break
    assert len(fake.calls(f"/Items/{SERIES_ID}/Refresh")) == 1
    assert clock.now - 1000 <= 130


def test_several_folders_get_one_refresh_each_and_no_full_scan(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock)
    for folder in ("Filme", "Serien", "Filme", "Hörbücher", "Serien"):
        refresher_.request_refresh(folder)
    clock.now += 60
    assert refresher_.flush_due()
    sent = sorted(c.path for c in fake.seen if c.path.startswith("/Items/"))
    assert sent == sorted(f"/Items/{i}/Refresh" for i in (MOVIES_ID, SERIES_ID, BOOKS_ID))
    assert fake.calls("/Library/Refresh") == []


def test_the_folder_name_in_nfd_still_finds_the_library(fake):
    import unicodedata

    clock = Clock()
    refresher_ = refresher(fake, clock)
    refresher_.request_refresh(unicodedata.normalize("NFD", "Hörbücher"))
    clock.now += 60
    refresher_.flush_due()
    assert fake.calls(f"/Items/{BOOKS_ID}/Refresh")


def test_a_folder_no_library_has_gets_a_full_refresh(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock)
    refresher_.request_refresh("Filme")
    refresher_.request_refresh("Musik")  # no such library in the fake
    clock.now += 60
    refresher_.flush_due()
    assert len(fake.calls("/Library/Refresh")) == 1
    assert len(fake.calls(f"/Items/{MOVIES_ID}/Refresh")) == 1


def test_a_request_without_a_folder_means_everything(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock)
    refresher_.request_refresh()
    refresher_.request_refresh("Filme")
    clock.now += 60
    refresher_.flush_due()
    assert len(fake.calls("/Library/Refresh")) == 1
    assert fake.calls("/Library/VirtualFolders") == [], "no need to look up libraries"


def test_requests_after_a_flush_start_a_new_batch(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock)
    refresher_.request_refresh("Filme")
    clock.now += 60
    refresher_.flush_due()
    refresher_.request_refresh("Filme")
    clock.now += 60
    refresher_.flush_due()
    assert len(fake.calls(f"/Items/{MOVIES_ID}/Refresh")) == 2
    assert refresher_.refreshes_sent == 2


# --- the refresher: fail soft ----------------------


def test_request_refresh_never_raises_and_never_calls_jellyfin(fake):
    refresher_ = refresher(fake, Clock())
    refresher_.request_refresh("Filme")
    assert fake.seen == [], "only flush_due talks to Jellyfin"


def test_without_a_client_everything_is_a_quiet_no_op():
    refresher_ = LibraryRefresher(None)
    assert not refresher_.enabled
    refresher_.request_refresh("Filme")
    assert refresher_.flush_due() is False
    assert not refresher_.pending


def test_a_failing_jellyfin_is_retried_later_not_immediately_and_not_for_ever(fake, caplog):
    clock = Clock()
    refresher_ = refresher(fake, clock, quiet_seconds=1, retry_seconds=60, max_failures=3)
    fake.status_override = 503
    refresher_.request_refresh("Filme")
    clock.now += 5
    with caplog.at_level(logging.WARNING, logger="ytp.clients.jellyfin"):
        assert refresher_.flush_due() is False
        assert refresher_.pending, "kept for a retry"
        calls = len(fake.seen)
        clock.now += 30
        assert refresher_.flush_due() is False
        assert len(fake.seen) == calls, "waits out the retry delay"
        clock.now += 31
        refresher_.flush_due()
        clock.now += 61
        refresher_.flush_due()
    assert not refresher_.pending, "gave up after max_failures"
    assert "will retry" in caplog.text
    assert "giving up" in caplog.text


def test_a_retry_that_succeeds_clears_the_pending_refresh(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock, quiet_seconds=1, retry_seconds=10)
    fake.status_override = 503
    refresher_.request_refresh("Filme")
    clock.now += 5
    refresher_.flush_due()
    fake.status_override = None
    clock.now += 11
    assert refresher_.flush_due() is True
    assert not refresher_.pending


def test_a_revoked_key_is_logged_once_and_the_batch_dropped_without_retries(fake, caplog):
    clock = Clock()
    refresher_ = refresher(fake, clock, quiet_seconds=1, retry_seconds=10)
    fake.status_override = 401
    refresher_.request_refresh("Filme")
    clock.now += 5
    with caplog.at_level(logging.WARNING, logger="ytp.clients.jellyfin"):
        assert refresher_.flush_due() is False
    (record,) = [r for r in caplog.records if r.getMessage() == "jellyfin: library refresh dropped"]
    assert "API Keys" in record.error
    assert not refresher_.pending, "a revoked key is not retried every minute"
    calls = len(fake.seen)
    clock.now += 100
    assert refresher_.flush_due() is False
    assert len(fake.seen) == calls


def test_requests_during_a_failed_flush_are_not_lost(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock, quiet_seconds=1, retry_seconds=10)
    fake.status_override = 503
    refresher_.request_refresh("Filme")
    clock.now += 5
    refresher_.flush_due()
    refresher_.request_refresh("Serien")
    fake.status_override = None
    clock.now += 11
    assert refresher_.flush_due() is True
    assert fake.calls(f"/Items/{MOVIES_ID}/Refresh")
    assert fake.calls(f"/Items/{SERIES_ID}/Refresh")


def test_many_threads_requesting_at_once_give_one_refresh(fake):
    clock = Clock()
    refresher_ = refresher(fake, clock)

    def work():
        for _ in range(20):
            refresher_.request_refresh("Filme")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    clock.now += 60
    refresher_.flush_due()
    assert len(fake.calls(f"/Items/{MOVIES_ID}/Refresh")) == 1


# --- inside the app ----------------------


def make_client(tmp_path: Path, **env: str) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), **env}
    return TestClient(create_app(Settings.from_env(values)))


def test_with_url_and_key_the_app_has_a_refresher_and_a_scheduled_flush(tmp_path, fake):
    with make_client(tmp_path, JELLYFIN_URL=fake.url, JELLYFIN_API_KEY=API_KEY) as client:
        assert client.app.state.jellyfin.enabled
        assert "jellyfin-refresh" in client.app.state.scheduler.job_ids()
        assert client.get("/health").status_code == 200, "health does not depend on Jellyfin"


def test_without_a_key_the_app_degrades_gracefully(tmp_path, fake):
    with make_client(tmp_path, JELLYFIN_URL=fake.url) as client:
        refresher_ = client.app.state.jellyfin
        assert not refresher_.enabled
        refresher_.request_refresh("Filme")
        assert "jellyfin-refresh" not in client.app.state.scheduler.job_ids()
        assert fake.seen == []
        assert client.get("/health").status_code == 200
