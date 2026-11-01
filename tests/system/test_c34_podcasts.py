"""A real podcast feed and episode through the real fetcher (issue #34). Needs internet; opt-in.

    YTP_NETWORK_TESTS=1 make test-system
    YTP_NETWORK_TESTS=1 YTP_PODCAST_PROXY=http://127.0.0.1:8888 make test-system   # through the VPN

Proves what the unit tests (a local fake server and made-up XML) cannot: that a real feed has the
shape `parse_podcast` expects, that the episode behind a real chain of tracking redirects
(Podtrac and others) is reached with every hop checked, that TLS works against the pinned IP
address, and that the file ends up where the worker puts it. NPR's "News Now" is used: a public
feed whose episodes are about five minutes (a few MB) long.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from ytp.podcasts import (
    MAX_EPISODE_BYTES,
    PodcastError,
    PodcastFeedClient,
    choose_extension,
    place_episode,
)
from ytp.safefetch import SafeFetcher

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

FEED = "https://feeds.npr.org/500005/podcast.xml"
PROXY = os.environ.get("YTP_PODCAST_PROXY", "")


@pytest.fixture(scope="module")
def client():
    return PodcastFeedClient(SafeFetcher(PROXY))


def test_the_real_feed_has_episodes_in_the_expected_shape(client):
    podcast = client.load(FEED)
    assert podcast.title
    assert podcast.episodes
    assert podcast.episodes == sorted(podcast.episodes, key=lambda e: e.published, reverse=True)
    assert all(e.enclosure_url.startswith(("http://", "https://")) for e in podcast.episodes)
    assert podcast.image_url is None or podcast.image_url.startswith("https://")


def test_the_cover_is_a_real_picture(client):
    podcast = client.load(FEED)
    assert podcast.image_url
    cover = client.cover(podcast.image_url)
    assert cover is not None
    assert cover[1] in {".jpg", ".png"}


def test_a_real_episode_is_downloaded_through_the_redirects_and_filed(client, tmp_path):
    podcast = client.load(FEED)
    episode = podcast.episodes[0]
    part = tmp_path / "episode.part"
    fetched = client.fetcher.download(
        episode.enclosure_url, part, kind="media", max_bytes=MAX_EPISODE_BYTES, deadline_seconds=300
    )
    assert fetched.size > 100_000
    assert part.stat().st_size == fetched.size
    extension = choose_extension(
        ("url", episode.enclosure_url), ("url", fetched.url), ("type", fetched.content_type)
    )
    assert extension in {".mp3", ".m4a"}
    library = tmp_path / "Podcasts"
    library.mkdir()
    target = place_episode(
        part, library, podcast.title, date.today(), episode.title, extension or ".mp3"
    )
    assert target.is_file()
    assert target.stat().st_size == fetched.size
    assert target.parent == library / podcast.title


def test_the_real_resolver_refuses_the_home_network_and_localhost(client):
    for feed in (
        "http://192.168.178.1/feed.xml",
        "http://127.0.0.1/feed.xml",
        "http://localhost/f",
    ):
        with pytest.raises(PodcastError) as error:
            client.load(feed)
        assert error.value.kind == "blocked"
