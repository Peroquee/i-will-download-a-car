"""The real yt-dlp answers that decide "live / premiere / Short" (issue #39). Needs internet.

    YTP_NETWORK_TESTS=1 make test-system

Proves that the fields `ytp.video_filter` reads (`live_status`, `is_live`, `media_type`, the
sizes) still look the way the unit tests assume. An ordinary video (Blender's Creative Commons
"Big Buck Bunny") must be left alone; a 24/7 live stream must be called live.
"""

from __future__ import annotations

import os

import pytest

from ytp.video_filter import skip_reason
from ytp.youtube import ProbeError, YoutubeProbe, parse_video_url

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

PROXY = os.environ.get("YTP_YTDLP_PROXY", "")
BIG_BUCK_BUNNY = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
LIVE_24_7 = "https://www.youtube.com/watch?v=jfKfPfyJRdk"  # a long-running public live stream


def traits(url: str):
    return YoutubeProbe(proxy=PROXY, timeout=60).traits_blocking(parse_video_url(url))


def test_an_ordinary_video_is_not_skipped():
    found = traits(BIG_BUCK_BUNNY)
    assert found.duration > 300
    assert skip_reason(found) is None


def test_a_live_stream_is_called_live_either_way():
    """yt-dlp either answers (live_status is_live) or refuses a stream it cannot read; both
    are "live" for the poller, never an ordinary video."""
    refused = None
    try:
        found = traits(LIVE_24_7)
    except ProbeError as error:
        refused = error.kind
    if refused is None:
        assert skip_reason(found) == "live"
    else:
        assert refused == "live"
