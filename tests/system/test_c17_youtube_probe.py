"""The real yt-dlp against real YouTube (issue #17). Needs internet; skipped unless asked.

    YTP_NETWORK_TESTS=1 make test-system            # directly from this host
    YTP_NETWORK_TESTS=1 YTP_YTDLP_PROXY=http://127.0.0.1:8888 make test-system   # via a proxy

It proves what the unit tests (which fake yt-dlp) cannot: that the pinned yt-dlp still understands
YouTube and that the option names and error texts we rely on are still the real ones. It uses the
Blender Foundation's Creative Commons film "Big Buck Bunny" and a video id that does not exist.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from ytp.youtube import ProbeError, YoutubeProbe, parse_video_url

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

BIG_BUCK_BUNNY = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
PROXY = os.environ.get("YTP_YTDLP_PROXY", "")


def probe(url: str):
    return asyncio.run(YoutubeProbe(proxy=PROXY, timeout=60).probe(parse_video_url(url)))


def test_a_real_video_gives_title_channel_duration_and_thumbnail():
    info = probe(BIG_BUCK_BUNNY)
    assert "Big Buck Bunny" in info.title
    assert info.channel
    assert info.duration > 300
    assert info.thumbnail.startswith("https://")


def test_a_video_that_does_not_exist_is_a_clean_error_not_a_crash():
    with pytest.raises(ProbeError) as caught:
        probe("https://www.youtube.com/watch?v=zzzzzzzzzzz")
    assert caught.value.kind == "unavailable"
    assert caught.value.message
