"""The real channel RSS feed (issue #20). Needs internet; opt-in.

    YTP_NETWORK_TESTS=1 make test-system
    YTP_NETWORK_TESTS=1 YTP_YTDLP_PROXY=http://127.0.0.1:8888 make test-system   # through the VPN

Proves what the unit tests (a local fake server and made-up XML) cannot: that YouTube still serves
the feed at this address in the shape `parse_feed` expects, and that the CONNECT tunnel through
the proxy works. The Blender Foundation's channel is used.
"""

from __future__ import annotations

import os

import pytest

from ytp.feeds import FeedClient, parse_feed

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

BLENDER = "UCSMOQeBJ2RAnuFungnQOxLg"
PROXY = os.environ.get("YTP_YTDLP_PROXY", "")


def test_the_real_feed_has_recent_videos_in_the_expected_shape():
    entries = parse_feed(FeedClient(PROXY).fetch(BLENDER), BLENDER)
    assert entries, "the channel has published videos"
    assert len(entries) <= 15
    assert all(len(e.video_id) == 11 and e.title for e in entries)
    assert entries == sorted(entries, key=lambda e: e.published, reverse=True)
