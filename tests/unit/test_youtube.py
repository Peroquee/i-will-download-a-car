"""app/ytp/youtube.py and POST /api/youtube/probe (issue #17). No network: yt-dlp is faked."""

# ruff: noqa: E501  (the real yt-dlp messages and the parametrised tables are long)

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ytp.config import ConfigError, Settings
from ytp.main import create_app
from ytp.youtube import (
    InvalidVideoUrl,
    ProbeError,
    VideoRef,
    YoutubeProbe,
    classify,
    parse_video_url,
    ydl_options,
)

ID = "dQw4w9WgXcQ"
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
INFO = {
    "id": ID,
    "title": "Größe Reise \u2013 Teil 1",
    "channel": "Kanal Ä",
    "channel_id": "UC1234567890123456789012",
    "duration": 3725,
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
}


# --- which addresses are accepted -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"https://www.youtube.com/watch?v={ID}",
        f"http://youtube.com/watch?v={ID}",
        f"https://m.youtube.com/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}",
        f"https://www.youtube.com/watch?v={ID}&t=42s&feature=share",
        f"https://www.youtube.com/watch?v={ID}&list=PLabc&index=3",
        f"https://youtu.be/{ID}",
        f"https://youtu.be/{ID}?si=tracking",
        f"https://www.youtube.com/shorts/{ID}",
        f"https://www.youtube.com/embed/{ID}",
        f"https://www.youtube.com/live/{ID}?feature=share",
        f"  https://youtu.be/{ID}\n",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={ID}",
        f"https://www.youtube.com:443/watch?v={ID}",
    ],
)
def test_single_video_addresses_give_the_video_id(text):
    assert parse_video_url(text) == VideoRef(ID)


def test_the_address_handed_to_ytdlp_is_rebuilt_from_the_id():
    ref = parse_video_url(f"https://www.youtube.com/watch?v={ID}&list=PLevil&t=9&x=<script>")
    assert ref.url == f"https://www.youtube.com/watch?v={ID}"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a url",
        "youtube.com/watch?v=" + ID,  # no scheme
        "ftp://www.youtube.com/watch?v=" + ID,
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://vimeo.com/123456",
        "https://example.com/watch?v=" + ID,
        "https://youtube.com.evil.example/watch?v=" + ID,
        "https://evil.example/youtube.com/watch?v=" + ID,
        "https://notyoutube.com/watch?v=" + ID,
        "https://www.youtube.com@evil.example/watch?v=" + ID,
        "https://user:pw@www.youtube.com/watch?v=" + ID,
        "https://www.youtube.com:8080/watch?v=" + ID,
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch?v=" + ID + "x",
        "https://www.youtube.com/watch?v=" + ID + "&v=" + "a" * 11,
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/channel/UC1234567890123456789012",
        "https://www.youtube.com/@handle",
        "https://www.youtube.com/c/name",
        "https://www.youtube.com/user/name",
        "https://www.youtube.com/",
        "https://youtu.be/",
        "https://youtu.be/short",
        "https://www.youtube.com/shorts/",
        "https://www.youtube.com/watch?v=" + ID + "\nhttps://evil.example",
        "https://www.youtube.com/watch?v=" + ID + " --exec rm",
        "https://www.youtube.com/watch?v=" + ID + ";rm -rf /",
        "https://www.youtube.com/" + "a" * 400,
        "https://www.youtube.com/watch?v=\x00" + ID[:10],
    ],
)
def test_everything_else_is_refused_before_ytdlp_sees_it(text):
    with pytest.raises(InvalidVideoUrl):
        parse_video_url(text)


@pytest.mark.parametrize("value", [None, 5, ["https://youtu.be/" + ID]])
def test_non_text_is_refused(value):
    with pytest.raises(InvalidVideoUrl):
        parse_video_url(value)  # type: ignore[arg-type]


def test_a_playlist_address_gets_a_helpful_message():
    with pytest.raises(InvalidVideoUrl, match="playlist or channel"):
        parse_video_url("https://www.youtube.com/playlist?list=PLabc")


@pytest.mark.security
def test_a_shell_looking_tail_is_never_in_what_ytdlp_gets():
    ref = parse_video_url(f"https://youtu.be/{ID}?a=$(reboot)")
    assert ref.url == f"https://www.youtube.com/watch?v={ID}"


# --- error classification -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
        ("ERROR: [youtube] x: Video unavailable", "unavailable"),
        ("ERROR: [youtube] zzzzzzzzzzz: This video is unavailable", "unavailable"),  # seen for real
        ("ERROR: [youtube] x: This video has been removed by the uploader", "unavailable"),
        (
            "ERROR: [youtube] x: This video is no longer available because the account was terminated",
            "unavailable",
        ),
        (
            "ERROR: [youtube] x: The uploader has not made this video available in your country",
            "region",
        ),
        ("ERROR: [youtube] x: This video is not available in your country", "region"),
        (
            "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.",
            "age",
        ),
        ("ERROR: [youtube] x: This video is age-restricted", "age"),
        ("ERROR: [youtube] x: Join this channel to get access to members-only content", "members"),
        ("ERROR: [youtube] x: Sign in to confirm you\u2019re not a bot", "bot_check"),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies", "bot_check"),
        ("ERROR: [youtube] x: This live event will begin in 2 hours", "live"),
        ("ERROR: [youtube] x: Premieres in 3 days", "live"),
        ("ERROR: [youtube] x: Unable to download webpage: <urlopen error timed out>", "network"),
        (
            "ERROR: [youtube] x: Unable to connect to proxy: Tunnel connection failed: 503",
            "network",
        ),
        (
            "ERROR: [youtube:tab] @nobody: Unable to download API page: HTTP Error 404: Not Found",  # seen for real
            "not_found",
        ),
        ("ERROR: [youtube:tab] x: Unable to download API page: HTTP Error 503", "network"),
        ("ERROR: Unsupported URL: https://example.com", "unsupported"),
        ("ERROR: something nobody has seen before", "unknown"),
        ("", "unknown"),
    ],
)
def test_ytdlp_messages_become_stable_kinds(message, kind):
    assert classify(message).kind == kind


def test_ansi_colours_in_a_message_are_ignored():
    assert classify("\x1b[0;31mERROR:\x1b[0m [youtube] x: Private video").kind == "private"


def test_an_unknown_message_is_shortened_and_one_line():
    error = classify("ERROR: weird\n" + "x" * 1000)
    assert error.kind == "unknown"
    assert "\n" not in error.message
    assert len(error.message) < 260


def test_age_wins_over_the_generic_sign_in_text():
    assert classify("Sign in to confirm your age").kind == "age"


# --- the probe --------------------------------------------------------------------------------------------


def fake_extract(info=None, error=None, seen=None):
    def extract(url, options):
        if seen is not None:
            seen.append((url, options))
        if error is not None:
            raise error
        return dict(INFO if info is None else info)

    return extract


def test_a_normal_video(tmp_path):
    seen = []
    probe = YoutubeProbe(extract=fake_extract(seen=seen))
    info = asyncio.run(probe.probe(VideoRef(ID)))
    assert info.as_dict() == {
        "video_id": ID,
        "url": f"https://www.youtube.com/watch?v={ID}",
        "title": "Größe Reise \u2013 Teil 1",
        "channel": "Kanal Ä",
        "channel_id": "UC1234567890123456789012",
        "duration": 3725,
        "duration_text": "1:02:05",
        "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    }
    assert seen[0][0] == f"https://www.youtube.com/watch?v={ID}"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0:00"), (59, "0:59"), (600, "10:00"), (3600, "1:00:00"), (36000, "10:00:00")],
)
def test_duration_text(seconds, text):
    from ytp.youtube import VideoInfo

    assert VideoInfo(ID, "u", "t", "c", "i", seconds, "").duration_text == text


def test_missing_or_odd_fields_are_tolerated():
    probe = YoutubeProbe(
        extract=fake_extract(info={"title": "T", "uploader": "U", "duration": None})
    )
    info = asyncio.run(probe.probe(VideoRef(ID)))
    assert (info.channel, info.channel_id, info.duration, info.thumbnail) == ("U", "", 0, "")


def test_the_thumbnail_falls_back_to_the_list_and_only_https_is_kept():
    info = {
        "title": "T",
        "thumbnails": [
            {"url": "https://i.ytimg.com/small.jpg"},
            {"url": "http://evil.example/x.jpg"},
        ],
    }
    got = asyncio.run(YoutubeProbe(extract=fake_extract(info=info)).probe(VideoRef(ID)))
    assert got.thumbnail == "https://i.ytimg.com/small.jpg"
    only_http = {
        "title": "T",
        "thumbnail": "http://x.example/a.jpg",
        "thumbnails": [{"url": "javascript:1"}],
    }
    assert (
        asyncio.run(
            YoutubeProbe(extract=fake_extract(info=only_http)).probe(VideoRef(ID))
        ).thumbnail
        == ""
    )


def test_control_characters_in_texts_are_removed():
    info = {"title": "Evil\x1b[31m title\n\t", "channel": "Chan\x00nel"}
    got = asyncio.run(YoutubeProbe(extract=fake_extract(info=info)).probe(VideoRef(ID)))
    assert "\x1b" not in got.title
    assert "\n" not in got.title
    assert got.channel == "Channel"


@pytest.mark.parametrize(
    ("info", "kind"),
    [
        ({"title": "T", "is_live": True}, "live"),
        ({"title": "T", "live_status": "is_upcoming"}, "live"),
        ({"_type": "playlist", "title": "P", "entries": []}, "unsupported"),
        ({"title": "T", "entries": [{"id": "x"}]}, "unsupported"),
        ({"title": ""}, "unavailable"),
        ({}, "unavailable"),
    ],
)
def test_info_that_is_not_one_ready_video_is_an_error(info, kind):
    with pytest.raises(ProbeError) as caught:
        asyncio.run(YoutubeProbe(extract=fake_extract(info=info)).probe(VideoRef(ID)))
    assert caught.value.kind == kind


def test_a_finished_live_stream_is_fine():
    info = dict(INFO, was_live=True, live_status="was_live")
    assert asyncio.run(YoutubeProbe(extract=fake_extract(info=info)).probe(VideoRef(ID))).title


def test_probe_errors_pass_through_and_other_exceptions_become_unknown():
    with pytest.raises(ProbeError) as caught:
        asyncio.run(
            YoutubeProbe(extract=fake_extract(error=ProbeError("private", "x"))).probe(VideoRef(ID))
        )
    assert caught.value.kind == "private"
    with pytest.raises(ProbeError) as caught:
        asyncio.run(YoutubeProbe(extract=fake_extract(error=KeyError("boom"))).probe(VideoRef(ID)))
    assert caught.value.kind == "unknown"
    assert "KeyError" in caught.value.message


def test_a_slow_probe_times_out_instead_of_hanging():
    release = threading.Event()

    def slow(url, options):
        release.wait(5)
        return dict(INFO)

    probe = YoutubeProbe(timeout=0.3, extract=slow)

    async def go() -> float:
        started = time.monotonic()
        with pytest.raises(ProbeError) as caught:
            await probe.probe(VideoRef(ID))
        assert caught.value.kind == "timeout"
        return time.monotonic() - started

    # (asyncio.run itself waits for the abandoned thread at the end; the caller does not.)
    try:
        assert asyncio.run(_with_release(go(), release, after=1.0)) < 2
    finally:
        release.set()


async def _with_release(coro, release: threading.Event, after: float):
    """Let the abandoned worker finish soon after the check, so the loop can shut down."""
    threading.Timer(after, release.set).start()
    return await coro


def test_abandoned_slow_probes_cannot_pile_up():
    release = threading.Event()

    def slow(url, options):
        release.wait(5)
        return dict(INFO)

    probe = YoutubeProbe(timeout=0.2, max_parallel=1, extract=slow)

    async def go():
        with pytest.raises(ProbeError) as first:
            await probe.probe(VideoRef(ID))
        assert first.value.kind == "timeout"
        # The first thread is still running and holds the only slot.
        with pytest.raises(ProbeError) as second:
            await probe.probe(VideoRef(ID))
        assert second.value.kind == "busy"

    asyncio.run(go())
    release.set()
    time.sleep(0.2)  # the worker ends and gives its slot back
    assert asyncio.run(YoutubeProbe(extract=fake_extract()).probe(VideoRef(ID)))


def test_a_slot_is_given_back_after_every_outcome():
    probe = YoutubeProbe(max_parallel=1, extract=fake_extract(error=ProbeError("private", "x")))
    for _ in range(3):
        with pytest.raises(ProbeError):
            asyncio.run(probe.probe(VideoRef(ID)))
    probe._extract = fake_extract()
    assert asyncio.run(probe.probe(VideoRef(ID)))


# --- how yt-dlp is configured ---------------------------------------------------------------------------------


def test_the_ytdlp_options_are_metadata_only_and_locked_down():
    options = ydl_options("http://gluetun:8888", 10)
    assert options["skip_download"] is True
    assert options["noplaylist"] is True
    assert options["allowed_extractors"] == ["youtube"]
    assert options["cachedir"] is False
    assert options["retries"] == options["extractor_retries"] == 0
    assert options["enable_file_urls"] is False
    assert options["proxy"] == "http://gluetun:8888"
    assert options["socket_timeout"] == 10
    assert "outtmpl" not in options
    assert "exec" not in options


def test_no_proxy_option_when_the_proxy_is_empty():
    assert "proxy" not in ydl_options("", 10)


def test_the_real_ytdlp_accepts_these_options():
    """Catches an option that a yt-dlp update renamed, without any network."""
    import yt_dlp

    with yt_dlp.YoutubeDL(ydl_options("http://gluetun:8888", 10)) as ydl:
        assert ydl.params["noplaylist"] is True
        assert ydl.params["proxy"] == "http://gluetun:8888"


def test_ytdlp_is_pinned_in_the_image_requirements():
    root = Path(__file__).resolve().parents[2]
    lines = (root / "app" / "requirements.txt").read_text().splitlines()
    pinned = [ln for ln in lines if ln.startswith("yt-dlp==")]
    assert len(pinned) == 1
    import re

    assert re.fullmatch(r"yt-dlp==\d{4}\.\d{1,2}\.\d{1,2}", pinned[0])


# --- the endpoint --------------------------------------------------------------------------------------------------


def make_client(tmp_path: Path, extract, **env: str) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), **env}
    client = TestClient(create_app(Settings.from_env(values)))
    client.__enter__()
    client.app.state.youtube = YoutubeProbe(timeout=1.0, extract=extract)
    return client


@pytest.fixture
def client(tmp_path):
    test_client = make_client(tmp_path, fake_extract())
    yield test_client
    test_client.__exit__(None, None, None)


def post(client, url=f"https://youtu.be/{ID}", **extra):
    return client.post("/api/youtube/probe", json={"url": url, **extra})


def test_probe_endpoint_returns_the_metadata(client):
    response = post(client)
    assert response.status_code == 200
    body = response.json()
    assert (body["title"], body["channel"], body["duration"]) == (
        "Größe Reise \u2013 Teil 1",
        "Kanal Ä",
        3725,
    )
    assert body["video_id"] == ID


@pytest.mark.parametrize(
    "url", ["https://vimeo.com/1", "not a url", "", "https://www.youtube.com/playlist?list=PL1"]
)
def test_a_bad_address_is_a_field_level_422_and_ytdlp_is_not_called(tmp_path, url):
    seen = []
    c = make_client(tmp_path, fake_extract(seen=seen))
    try:
        response = post(c, url)
    finally:
        c.__exit__(None, None, None)
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "url"]
    assert error["type"] == "video_url_invalid"
    assert seen == []


@pytest.mark.parametrize("url", ["https://vimeo.com/1", "not a url", ""])
def test_probe_and_download_share_the_same_video_url_check(url):
    """#75: one validator for both request bodies; the two cannot drift apart."""
    from pydantic import ValidationError

    from ytp.api import DownloadRequest, ProbeRequest

    for model, extra in (
        (ProbeRequest, {}),
        (DownloadRequest, {"channel": "Kanal", "title": "Titel"}),
    ):
        with pytest.raises(ValidationError) as raised:
            model(url=url, **extra)
        (error,) = raised.value.errors()
        assert error["loc"] == ("url",)
        assert error["type"] == "video_url_invalid"


@pytest.mark.parametrize(
    "body", [{}, {"url": 5}, {"url": f"https://youtu.be/{ID}", "extra": 1}, []]
)
def test_malformed_bodies_are_422(client, body):
    assert client.post("/api/youtube/probe", json=body).status_code == 422


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("private", 404), ("unavailable", 404), ("region", 403), ("age", 403), ("members", 403),
        ("live", 409), ("bot_check", 502), ("network", 502), ("unknown", 502),
        ("timeout", 504), ("busy", 429), ("unsupported", 422),
    ],
)  # fmt: skip
def test_each_failure_kind_has_its_status_and_a_readable_message(tmp_path, kind, status):
    c = make_client(tmp_path, fake_extract(error=ProbeError(kind, f"message for {kind}")))
    try:
        response = post(c)
    finally:
        c.__exit__(None, None, None)
    assert response.status_code == status
    assert response.json() == {"detail": f"message for {kind}", "kind": kind}


def test_a_real_classification_flows_through(tmp_path):
    c = make_client(tmp_path, fake_extract(error=classify("ERROR: [youtube] x: Private video")))
    try:
        response = post(c)
    finally:
        c.__exit__(None, None, None)
    assert (response.status_code, response.json()["kind"]) == (404, "private")


def test_a_slow_probe_answers_504_within_the_deadline(tmp_path):
    release = threading.Event()

    def slow(url, options):
        release.wait(5)
        return dict(INFO)

    c = make_client(tmp_path, slow)
    try:
        started = time.monotonic()
        response = post(c)
        assert time.monotonic() - started < 3
    finally:
        release.set()
        c.__exit__(None, None, None)
    assert response.status_code == 504
    assert response.json()["kind"] == "timeout"


def test_only_post_is_allowed(client):
    assert client.get("/api/youtube/probe").status_code == 405


def test_nothing_is_stored_by_a_probe(client):
    post(client)
    assert client.app.state.db.query("SELECT COUNT(*) FROM jobs")[0][0] == 0


@pytest.mark.security
def test_the_pasted_tail_never_reaches_the_extractor(tmp_path):
    seen = []
    c = make_client(tmp_path, fake_extract(seen=seen))
    try:
        post(c, f"https://www.youtube.com/watch?v={ID}&list=PLevil&x=$(reboot)")
    finally:
        c.__exit__(None, None, None)
    assert seen[0][0] == f"https://www.youtube.com/watch?v={ID}"


# --- configuration ------------------------------------------------------------------------------------------------------


def test_the_proxy_defaults_to_gluetun_and_empty_means_direct():
    assert Settings.from_env(ENV).ytdlp_proxy == "http://gluetun:8888"
    assert Settings.from_env({**ENV, "YTDLP_PROXY": ""}).ytdlp_proxy == ""
    assert (
        Settings.from_env({**ENV, "YTDLP_PROXY": "socks5://gluetun:1080"}).ytdlp_proxy
        == "socks5://gluetun:1080"
    )


def test_a_bad_proxy_or_timeout_is_reported():
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({**ENV, "YTDLP_PROXY": "gluetun:8888", "YOUTUBE_PROBE_TIMEOUT": "0"})
    text = str(caught.value)
    assert "YTDLP_PROXY" in text
    assert "YOUTUBE_PROBE_TIMEOUT" in text
    assert Settings.from_env({**ENV, "YOUTUBE_PROBE_TIMEOUT": "40"}).youtube_probe_timeout == 40
