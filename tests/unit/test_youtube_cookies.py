"""YouTube login cookies (#46): admin endpoints, validation, private storage, yt-dlp usage.

The cookie value below is a fake. It must never appear in a response, a log line or a message;
the tests check that, and a grep of the diff must find it in test files only.
"""

from __future__ import annotations

import logging
import stat

import pytest

from tests.helpers.accounts_app import ADMIN, add_account, client_for, running
from ytp import youtube_cookies
from ytp.youtube import ProbeError, VideoRef
from ytp.youtube_download import YoutubeDownloader, download_options

pytestmark = [pytest.mark.real_auth, pytest.mark.security]

ID = "dQw4w9WgXcQ"
COOKIE_NAME = "SAPISID"
COOKIE_VALUE = "fake-cookie-value-do-not-leak-1234567890"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    f".youtube.com\tTRUE\t/\tFALSE\t1900000000\t{COOKIE_NAME}\t{COOKIE_VALUE}\n"
    ".youtube.com\tTRUE\t/\tFALSE\t1900000000\tHSID\tsecond-fake-value\n"
    ".example.com\tTRUE\t/\tFALSE\t1900000000\tOTHER\tforeign-value\n"
    ".google.com\tTRUE\t/\tFALSE\t1900000000\tNID\tgoogle-fake-value\n"
)


@pytest.fixture
def app(tmp_path):
    with running(tmp_path) as client:
        add_account(client, "alice")
        yield client


def admin(app):
    return client_for(app, ADMIN)


def put(client, text: str):
    return client.put("/api/youtube/cookies", content=text.encode("utf-8"))


def calls(method: str, client, **extra):
    return client.request(method, "/api/youtube/cookies", **extra)


# --- the endpoints as admin ---------------------------------------------------------------------


def test_an_admin_stores_inspects_and_removes_the_cookies(app):
    response = put(admin(app), COOKIES)
    assert response.status_code == 200
    body = response.json()
    assert body["present"] is True
    assert body["cookie_count"] == 3, "two youtube cookies and one google cookie"
    assert body["updated_at"]

    status = admin(app).get("/api/youtube/cookies")
    assert status.status_code == 200
    assert status.json() == body, "the same file, the same answer"

    removed = admin(app).delete("/api/youtube/cookies")
    assert removed.status_code == 200
    assert removed.json() == {"present": False, "updated_at": None, "cookie_count": 0}
    assert admin(app).get("/api/youtube/cookies").json() == removed.json()


def test_removing_without_a_file_is_harmless(app):
    response = admin(app).delete("/api/youtube/cookies")
    assert response.status_code == 200
    assert response.json()["present"] is False


def test_the_get_answer_has_only_presence_time_and_count(app):
    put(admin(app), COOKIES)
    body = admin(app).get("/api/youtube/cookies").json()
    assert set(body) == {"present", "updated_at", "cookie_count"}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_logged_out_is_a_401_and_a_user_is_a_403(app, method):
    stranger = calls(method, client_for(app, None), content=b"x")
    assert stranger.status_code == 401
    assert stranger.json() == {"detail": "login required"}

    other = calls(method, client_for(app, "alice"), content=b"x")
    assert other.status_code == 403
    assert other.json() == {"detail": "this needs an admin account"}


# --- nothing leaks ------------------------------------------------------------------------------


def test_no_response_and_no_log_names_or_values_a_cookie(app, caplog):
    with caplog.at_level(logging.DEBUG):
        stored = put(admin(app), COOKIES)
        listed = admin(app).get("/api/youtube/cookies")
        removed = admin(app).delete("/api/youtube/cookies")
    for response in (stored, listed, removed):
        assert COOKIE_VALUE not in response.text
        assert COOKIE_NAME not in response.text
        assert "second-fake-value" not in response.text
    assert COOKIE_VALUE not in caplog.text
    assert COOKIE_NAME not in caplog.text
    assert any(
        record.getMessage() == "youtube cookies updated" and record.account == ADMIN
        for record in caplog.records
    )
    assert any(
        record.getMessage() == "youtube cookies removed" and record.account == ADMIN
        for record in caplog.records
    )


# --- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no header at all",
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t1\tNAME\n",
        "# Some other header\n.youtube.com\tTRUE\t/\tFALSE\t1\tN\tv\n",
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t1\tN\tv\n",
        "# Netscape HTTP Cookie File\n.youtube.com TRUE / FALSE 1 N v\n",
    ],
    ids=["empty", "no-header", "six-fields", "wrong-header", "no-youtube", "spaces"],
)
def test_a_file_that_is_not_a_usable_netscape_cookie_file_is_a_400(app, text):
    response = put(admin(app), text)
    assert response.status_code == 400
    assert not youtube_cookies.cookie_path(app.app.state.settings).exists()


def test_a_file_larger_than_one_mib_is_a_400(app):
    text = "# Netscape HTTP Cookie File\n" + "x" * (1024 * 1024)
    response = put(admin(app), text)
    assert response.status_code == 400
    assert not youtube_cookies.cookie_path(app.app.state.settings).exists()


def test_foreign_domains_are_stripped_before_saving(app):
    assert put(admin(app), COOKIES).status_code == 200
    text = youtube_cookies.cookie_path(app.app.state.settings).read_text(encoding="utf-8")
    assert "example.com" not in text
    assert "foreign-value" not in text
    assert ".youtube.com" in text
    assert COOKIE_VALUE in text
    assert ".google.com" in text, "YouTube's login also needs google.com cookies"
    assert text.startswith("# Netscape HTTP Cookie File\n")


def test_a_chrome_httponly_cookie_line_is_kept(app):
    text = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1900000000\tSID\thttponly-value\n"
    )
    kept = youtube_cookies.parse(text)
    assert kept == ["#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1900000000\tSID\thttponly-value"]


def test_the_header_may_be_the_http_cookie_file_one(app):
    text = "# HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t1\tN\tv\n"
    assert youtube_cookies.parse(text)


# --- the private file ---------------------------------------------------------------------------


def test_the_folder_is_0700_and_the_file_0600(app):
    put(admin(app), COOKIES)
    path = youtube_cookies.cookie_path(app.app.state.settings)
    assert path.is_file()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- yt-dlp -------------------------------------------------------------------------------------


def test_the_cookiefile_option_is_passed_only_when_the_file_exists(tmp_path):
    missing = tmp_path / "youtube-cookies.txt"
    options = download_options(
        tmp_path, proxy="", max_height=1080, socket_timeout=1, cookie_file=missing
    )
    assert "cookiefile" not in options

    missing.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    options = download_options(
        tmp_path, proxy="", max_height=1080, socket_timeout=1, cookie_file=missing
    )
    assert options["cookiefile"] == str(missing)

    assert "cookiefile" not in download_options(
        tmp_path, proxy="", max_height=1080, socket_timeout=1
    )


def test_the_downloader_hands_the_file_to_ytdlp(tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    seen: dict = {}

    def capture(url, options):
        seen.update(options)
        raise ProbeError("private", "This video is private.")

    downloader = YoutubeDownloader(download=capture, min_free_bytes=0, cookie_file=cookie)
    with pytest.raises(ProbeError):
        downloader.download(VideoRef(ID), tmp_path / "work")
    assert seen["cookiefile"] == str(cookie)


def test_an_age_error_without_cookies_tells_how_to_fix_it(tmp_path):
    def fail(url, options):
        raise ProbeError(
            "age", "This video is age-restricted and cannot be read without a YouTube login."
        )

    downloader = YoutubeDownloader(download=fail, min_free_bytes=0)
    with pytest.raises(ProbeError) as caught:
        downloader.download(VideoRef(ID), tmp_path / "work")
    assert caught.value.kind == "age"
    assert "An admin can add YouTube cookies in the settings" in caught.value.message


def test_an_age_error_with_cookies_stored_keeps_the_plain_message(tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    def fail(url, options):
        raise ProbeError(
            "age", "This video is age-restricted and cannot be read without a YouTube login."
        )

    downloader = YoutubeDownloader(download=fail, min_free_bytes=0, cookie_file=cookie)
    with pytest.raises(ProbeError) as caught:
        downloader.download(VideoRef(ID), tmp_path / "work")
    assert "An admin can add YouTube cookies" not in caught.value.message


def test_another_error_gets_no_cookie_hint(tmp_path):
    def fail(url, options):
        raise ProbeError("private", "This video is private.")

    downloader = YoutubeDownloader(download=fail, min_free_bytes=0)
    with pytest.raises(ProbeError) as caught:
        downloader.download(VideoRef(ID), tmp_path / "work")
    assert "cookies" not in caught.value.message.lower()
