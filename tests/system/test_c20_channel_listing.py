"""The real YouTube through yt-dlp: a channel's newest videos, and every spelling of a channel.

    YTP_NETWORK_TESTS=1 make test-system
    YTP_NETWORK_TESTS=1 YTP_YTDLP_PROXY=http://127.0.0.1:8888 make test-system   # through the VPN

Needs internet; opt-in. Proves what the unit tests (which fake yt-dlp) cannot, now that the RSS
feed answers 404 for whole networks (2026-11) and the poller asks yt-dlp first:

* the flat listing of `/channel/<id>/videos` returns the newest videos, at most 30, without
  walking through the whole channel, and without dates;
* a real video gives the date the poller needs (`VideoTraits.published`);
* a whole poll of a real channel needs one look at the first video, and none at the next poll;
* a channel given as `@handle`, `/c/name`, `/user/name`, `/channel/UC...` or by one of its videos
  resolves to the same `UC...` id; a channel that does not exist is `not_found`.

MKBHD's channel is used (it uploads often and has all the old address forms).
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC, datetime

import pytest

from ytp import jobs, seen_videos, subscriptions
from ytp.channel_videos import ChannelLister
from ytp.channels import ChannelResolver, parse_channel_ref
from ytp.db import Database
from ytp.subscription_poller import SubscriptionPoller
from ytp.video_filter import VideoTraits
from ytp.youtube import ProbeError, VideoRef, YoutubeProbe

pytestmark = [
    pytest.mark.network,
    pytest.mark.real_ytdlp,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

MKBHD = "UCBJycsmduvYEL83R_U4JriQ"
BIG_BUCK_BUNNY = "aqz-KE-bpKQ"
PROXY = os.environ.get("YTP_YTDLP_PROXY", "")


def probe() -> YoutubeProbe:
    return YoutubeProbe(proxy=PROXY, timeout=90, socket_timeout=20)


def test_the_real_list_has_the_newest_videos_and_is_cut_off_at_the_limit():
    entries = ChannelLister(probe(), limit=30).list(MKBHD)
    assert 10 <= len(entries) <= 30, "a big channel: the limit is reached, not the whole channel"
    assert len({e.video_id for e in entries}) == len(entries)
    assert all(len(e.video_id) == 11 and e.title for e in entries)
    assert all(e.published is None for e in entries), "the list has no dates (the poller copes)"


def test_a_channel_that_does_not_exist_is_not_found():
    with pytest.raises(ProbeError) as caught:
        ChannelLister(probe()).list("UC" + "a" * 22)
    assert caught.value.kind == "not_found"


def test_a_real_video_tells_when_it_went_online():
    traits = probe().traits_blocking(VideoRef(BIG_BUCK_BUNNY))
    assert isinstance(traits, VideoTraits)
    assert traits.published is not None
    assert traits.published.year <= 2015  # Big Buck Bunny, uploaded 2014


@pytest.mark.parametrize(
    "text",
    [
        MKBHD,
        f"https://www.youtube.com/channel/{MKBHD}/videos",
        "@mkbhd",
        "https://www.youtube.com/@mkbhd",
        "https://www.youtube.com/c/mkbhd",
        "https://www.youtube.com/user/marquesbrownlee",
    ],
)
def test_every_spelling_of_the_channel_resolves_to_the_same_id(text):
    resolver = ChannelResolver(probe())
    channel = asyncio.run(resolver.resolve(parse_channel_ref(text)))
    assert channel.channel_id == MKBHD
    assert channel.name


def test_a_whole_poll_of_a_real_channel_needs_one_look_and_the_next_none(tmp_path):
    """Subscribed just now: every listed video is older, so nothing is queued. The first poll
    dates the newest video (one look), the second stops at it without any look."""
    db = Database.open(tmp_path / "app.db")
    try:
        sub, _ = subscriptions.add(db, MKBHD, "Marques Brownlee")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE subscriptions SET created_at = ? WHERE id = ?",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"), sub.id),
            )
        looks: list[str] = []
        real = probe()
        original = real.traits_blocking

        def counting(ref):
            looks.append(ref.video_id)
            return original(ref)

        real.traits_blocking = counting  # type: ignore[method-assign]
        poller = SubscriptionPoller(
            db,
            feeds=None,  # type: ignore[arg-type]
            worker=None,
            poll_minutes=60,
            sleep=lambda s: None,
            rng=random.Random(1),
            probe=real,
            lister=ChannelLister(real),
        )
        assert poller.poll(subscriptions.get(db, sub.id)) == 0
        assert len(looks) == 1
        assert seen_videos.ids(db, sub.id)
        assert poller.poll(subscriptions.get(db, sub.id)) == 0
        assert len(looks) == 1, "the second poll stops at the first video it knows"
        assert jobs.list_jobs(db) == []
    finally:
        db.close()
