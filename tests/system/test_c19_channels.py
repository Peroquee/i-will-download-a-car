"""The real yt-dlp resolving real YouTube channels (issue #19). Needs internet; opt-in.

    YTP_NETWORK_TESTS=1 make test-system
    YTP_NETWORK_TESTS=1 YTP_YTDLP_PROXY=http://127.0.0.1:8888 make test-system

Proves what the unit tests (which fake yt-dlp) cannot: that the pinned yt-dlp still understands
channel pages (the `youtube:tab` extractor must stay allowed) and what a missing channel is.
The Blender Foundation's channel is used (Creative Commons films).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from ytp.channels import ChannelResolver, parse_channel_ref
from ytp.youtube import ProbeError, YoutubeProbe

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

BLENDER = "UCSMOQeBJ2RAnuFungnQOxLg"
PROXY = os.environ.get("YTP_YTDLP_PROXY", "")


def resolve(text: str):
    resolver = ChannelResolver(YoutubeProbe(proxy=PROXY, timeout=60))
    return asyncio.run(resolver.resolve(parse_channel_ref(text)))


@pytest.mark.parametrize(
    "text",
    [BLENDER, f"https://www.youtube.com/channel/{BLENDER}", "@BlenderOfficial"],
)
def test_a_real_channel_resolves_to_its_id_and_name(text):
    channel = resolve(text)
    assert channel.channel_id == BLENDER
    assert channel.name


def test_a_video_resolves_to_its_channel():
    assert resolve("https://www.youtube.com/watch?v=aqz-KE-bpKQ").channel_id == BLENDER


def test_a_channel_that_does_not_exist_is_not_found_not_a_vpn_problem():
    with pytest.raises(ProbeError) as caught:
        resolve("@zzzqqqnothere9x")
    assert caught.value.kind == "not_found"
