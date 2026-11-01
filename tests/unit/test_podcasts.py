"""Podcast feeds: reading them safely, keys, file names and filing (issue #34)."""

# ruff: noqa: E501

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ytp.podcasts import (
    MAX_EPISODES,
    MAX_FEED_BYTES,
    PodcastError,
    PodcastFeedClient,
    choose_extension,
    episode_filename,
    episode_key,
    has_cover,
    normalise_feed_url,
    parse_date,
    parse_podcast,
    place_episode,
    save_cover,
)
from ytp.safefetch import Fetched, FetchError

FEED = "https://podcast.example/feed.xml"


def item(
    guid, title, pub, url="https://cdn.example/e.mp3", mtype="audio/mpeg", length="1234", extra=""
):
    guid_xml = f"<guid>{guid}</guid>" if guid is not None else ""
    enclosure = f'<enclosure url="{url}" type="{mtype}" length="{length}"/>' if url else ""
    return (
        f"<item><title>{title}</title>{guid_xml}<pubDate>{pub}</pubDate>{enclosure}{extra}</item>"
    )


def rss(*items, title="Test Cast", image="", extra=""):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        f"<channel><title>{title}</title>{image}{extra}" + "".join(items) + "</channel></rss>"
    ).encode()


NEW = item(
    "g-new", "Newer &amp; better", "Fri, 31 Oct 2026 12:00:00 +0000", "https://cdn.example/new.mp3"
)
OLD = item(
    "g-old", "Older", "Wed, 01 Oct 2026 08:30:00 GMT", "https://cdn.example/old.m4a", "audio/x-m4a"
)


def test_a_feed_gives_its_episodes_newest_first():
    podcast = parse_podcast(rss(OLD, NEW), FEED)
    assert podcast.title == "Test Cast"
    assert [e.title for e in podcast.episodes] == ["Newer & better", "Older"]
    first = podcast.episodes[0]
    assert first.published == datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert first.enclosure_url == "https://cdn.example/new.mp3"
    assert first.media_type == "audio/mpeg"
    assert first.size == 1234
    assert first.key == episode_key(FEED, "g-new")


def test_the_cover_comes_from_itunes_image_or_the_plain_image_tag():
    itunes = '<itunes:image href="https://cdn.example/cover.jpg"/>'
    plain = "<image><url>https://cdn.example/plain.png</url><title>x</title></image>"
    assert parse_podcast(rss(NEW, image=itunes), FEED).image_url == "https://cdn.example/cover.jpg"
    assert parse_podcast(rss(NEW, image=plain), FEED).image_url == "https://cdn.example/plain.png"
    both = parse_podcast(rss(NEW, image=plain + itunes), FEED)
    assert both.image_url == "https://cdn.example/cover.jpg"
    assert parse_podcast(rss(NEW), FEED).image_url is None


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://x.example/c.jpg",
        "https://u:p@x.example/c.jpg",
        "",
    ],
)
def test_a_cover_address_that_is_not_http_is_ignored(bad):
    image = f'<itunes:image href="{bad}"/>'
    assert parse_podcast(rss(NEW, image=image), FEED).image_url is None


def test_an_empty_feed_is_fine():
    assert parse_podcast(rss(), FEED).episodes == []


def test_odd_items_are_skipped_not_fatal():
    no_enclosure = item("a", "no file", "Fri, 31 Oct 2026 12:00:00 +0000", url="")
    no_date = item("b", "no date", "not a date")
    bad_url = item("c", "bad url", "Fri, 31 Oct 2026 12:00:00 +0000", url="ftp://x.example/e.mp3")
    private_syntax = item(
        "d", "port", "Fri, 31 Oct 2026 12:00:00 +0000", url="https://x.example:22/e.mp3"
    )
    got = parse_podcast(rss(no_enclosure, no_date, bad_url, private_syntax, NEW), FEED)
    assert [e.title for e in got.episodes] == ["Newer & better"]


def test_without_a_guid_the_enclosure_address_identifies_the_episode():
    no_guid = item(None, "x", "Fri, 31 Oct 2026 12:00:00 +0000", "https://cdn.example/a.mp3")
    got = parse_podcast(rss(no_guid), FEED).episodes[0]
    assert got.key == episode_key(FEED, "https://cdn.example/a.mp3")


def test_the_same_guid_twice_in_a_feed_is_one_episode():
    got = parse_podcast(rss(NEW, NEW), FEED)
    assert len(got.episodes) == 1


def test_a_missing_title_gets_a_date_name():
    untitled = item("g", "", "Fri, 31 Oct 2026 12:00:00 +0000")
    assert parse_podcast(rss(untitled), FEED).episodes[0].title == "Episode 2026-10-31"


def test_the_number_of_episodes_is_capped():
    many = [
        item(f"g{i}", f"t{i}", f"Fri, 31 Oct 2026 12:{i % 60:02d}:00 +0000")
        for i in range(MAX_EPISODES + 40)
    ]
    assert len(parse_podcast(rss(*many), FEED).episodes) == MAX_EPISODES


def test_keys_are_stable_hashes_that_differ_per_feed():
    assert episode_key(FEED, "g") == episode_key(FEED, "g")
    assert episode_key(FEED, "g") != episode_key("https://other.example/feed", "g")
    assert episode_key(FEED, "g") != episode_key(FEED, "h")
    key = episode_key(FEED, "../../etc/passwd\n")
    assert len(key) == 40
    assert set(key) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "text",
    [
        "Fri, 31 Oct 2026 12:00:00 +0000",
        "Fri, 31 Oct 2026 14:00:00 +0200",
        "Fri, 31 Oct 2026 12:00:00 GMT",
        "2026-10-31T12:00:00Z",
        "2026-10-31T14:00:00+02:00",
        "Fri, 31 Oct 2026 12:00:00",
    ],
)
def test_dates_in_rss_and_iso_form_give_the_same_moment(text):
    assert parse_date(text) == datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text", ["", "   ", "yesterday", "31/10/2026", "Fri, 99 Oct 2026 12:00:00 +0000"]
)
def test_unreadable_dates_are_none(text):
    assert parse_date(text) is None


# --- hostile feeds ------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "prefix",
    [
        '<!DOCTYPE rss [<!ENTITY a "aaaa">]>',
        '<!DOCTYPE rss SYSTEM "http://192.168.178.1/evil.dtd">',
        '<!doctype rss [<!ENTITY x SYSTEM "file:///etc/passwd">]>',
        '<!ENTITY lol "lol">',
    ],
)
def test_a_dtd_or_entity_declaration_is_refused(prefix):
    data = rss(NEW).decode().replace("<rss", prefix + "<rss", 1).encode()
    with pytest.raises(PodcastError) as error:
        parse_podcast(data, FEED)
    assert error.value.kind == "bad_feed"


@pytest.mark.security
def test_a_billion_laughs_feed_is_refused_before_parsing():
    lol = '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
    data = (
        '<?xml version="1.0"?>' + lol + "<rss><channel><title>&lol2;</title></channel></rss>"
    ).encode()
    with pytest.raises(PodcastError):
        parse_podcast(data, FEED)


@pytest.mark.security
def test_utf16_cannot_hide_a_dtd():
    data = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss [<!ENTITY a "b">]><rss/>'.encode(
        "utf-16"
    )
    with pytest.raises(PodcastError) as error:
        parse_podcast(data, FEED)
    assert "UTF-8" in error.value.message


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not xml",
        b"<rss>",
        b"<html><body>login</body></html>",
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>',
        b"<rss version='2.0'/>",
        b"\xff\xfe\x00",
    ],
)
def test_things_that_are_not_rss_are_refused(data):
    with pytest.raises(PodcastError) as error:
        parse_podcast(data, FEED)
    assert error.value.kind == "bad_feed"


@pytest.mark.security
def test_an_oversized_feed_is_refused():
    with pytest.raises(PodcastError):
        parse_podcast(b"<rss>" + b" " * MAX_FEED_BYTES + b"</rss>", FEED)


@pytest.mark.security
def test_markup_in_titles_stays_text():
    hostile = item("g", "&lt;script&gt;alert(1)&lt;/script&gt;", "Fri, 31 Oct 2026 12:00:00 +0000")
    title = parse_podcast(rss(hostile), FEED).episodes[0].title
    assert title == "<script>alert(1)</script>"  # text; the UI only ever uses textContent


# --- addresses and file types -------------------------------------------------------------------


def test_the_feed_address_is_normalised_for_keys():
    assert normalise_feed_url(" HTTPS://Podcast.Example/feed.xml#top ") == FEED


@pytest.mark.parametrize(
    "bad",
    ["", "ftp://x.example/f", "https://u:p@x.example/f", "https://x.example:22/f", "feed.xml"],
)
def test_bad_feed_addresses_raise_podcast_errors(bad):
    with pytest.raises(PodcastError):
        normalise_feed_url(bad)


@pytest.mark.parametrize(
    ("hints", "ext"),
    [
        ([("url", "https://x.example/a/b.MP3?x=1")], ".mp3"),
        ([("url", "https://x.example/redirect.mp3/https://y.example/z")], None),
        ([("url", "https://x.example/get?id=1"), ("type", "audio/mp4")], ".m4a"),
        ([("type", "audio/mpeg; charset=binary")], ".mp3"),
        ([("url", "https://x.example/a.exe"), ("type", "audio/ogg")], ".ogg"),
        ([("url", "https://x.example/a.php"), ("type", "application/octet-stream")], None),
        ([("url", "https://x.example/a.sh")], None),
        ([], None),
    ],
)
def test_the_extension_comes_from_a_fixed_list(hints, ext):
    assert choose_extension(*hints) == ext


def test_filenames_start_with_the_date():
    assert (
        episode_filename(date(2026, 10, 31), "Folge 12: Ärger", ".mp3")
        == "2026-10-31 - Folge 12: Ärger.mp3"
    )


def test_filenames_never_carry_a_path():
    name = episode_filename(date(2026, 1, 2), "../../etc/passwd", ".mp3")
    assert "/" not in name
    assert name.endswith(".mp3")


def test_long_titles_are_shortened_to_fit_the_file_system():
    name = episode_filename(date(2026, 1, 2), "ä" * 200, ".m4a")
    assert len(name.encode()) <= 255 - len(" (99)")
    assert name.startswith("2026-01-02 - ä")
    assert name.endswith(".m4a")


def test_an_untitled_episode_still_gets_a_name():
    assert episode_filename(date(2026, 1, 2), "...", ".mp3") == "2026-01-02 - Episode.mp3"


# --- filing -------------------------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "Podcasts"
    folder.mkdir()
    return folder


def download(tmp_path, content=b"audio", name="episode.part"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_an_episode_is_filed_under_the_podcast(library, tmp_path):
    source = download(tmp_path)
    target = place_episode(source, library, "Test Cast", date(2026, 10, 31), "Pilot", ".mp3")
    assert target == library / "Test Cast" / "2026-10-31 - Pilot.mp3"
    assert target.read_bytes() == b"audio"
    assert not source.exists()


@pytest.mark.security
def test_a_different_file_with_the_same_name_is_never_overwritten(library, tmp_path):
    first = place_episode(
        download(tmp_path, b"one"), library, "P", date(2026, 1, 1), "Pilot", ".mp3"
    )
    second = place_episode(
        download(tmp_path, b"second!", "b.part"), library, "P", date(2026, 1, 1), "Pilot", ".mp3"
    )
    third = place_episode(
        download(tmp_path, b"third!!!!", "c.part"), library, "P", date(2026, 1, 1), "Pilot", ".mp3"
    )
    assert first.read_bytes() == b"one"
    assert second.name == "2026-01-01 - Pilot (2).mp3"
    assert second.read_bytes() == b"second!"
    assert third.name == "2026-01-01 - Pilot (3).mp3"


def test_filing_the_same_episode_twice_does_not_make_a_copy(library, tmp_path):
    """A job interrupted after filing runs again: the identical file is recognised."""
    first = place_episode(
        download(tmp_path, b"same"), library, "P", date(2026, 1, 1), "Pilot", ".mp3"
    )
    source = download(tmp_path, b"same", "again.part")
    again = place_episode(source, library, "P", date(2026, 1, 1), "Pilot", ".mp3")
    assert again == first
    assert not source.exists()
    assert sorted(p.name for p in (library / "P").iterdir()) == ["2026-01-01 - Pilot.mp3"]


@pytest.mark.security
def test_a_podcast_folder_that_points_outside_the_library_is_refused(library, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (library / "Evil").symlink_to(outside)
    with pytest.raises(PodcastError):
        place_episode(download(tmp_path), library, "Evil", date(2026, 1, 1), "Pilot", ".mp3")
    assert list(outside.iterdir()) == []


def test_a_symlink_in_the_way_is_not_followed(library, tmp_path):
    folder = library / "P"
    folder.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (folder / "2026-01-01 - Pilot.mp3").symlink_to(victim)
    target = place_episode(download(tmp_path), library, "P", date(2026, 1, 1), "Pilot", ".mp3")
    assert victim.read_text() == "keep"
    assert target.name == "2026-01-01 - Pilot (2).mp3"


# --- covers -------------------------------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


def test_a_cover_is_saved_once_per_podcast(library):
    first = save_cover(library, "P", JPEG, ".jpg")
    assert first == library / "P" / "folder.jpg"
    assert save_cover(library, "P", PNG, ".png") is None
    assert (library / "P" / "folder.jpg").read_bytes() == JPEG
    assert not (library / "P" / "folder.png").exists()


@pytest.mark.parametrize("existing", ["cover.jpg", "folder.png", "poster.jpeg", "folder.jpg"])
def test_a_cover_the_owner_put_there_is_kept(library, existing):
    (library / "P").mkdir()
    (library / "P" / existing).write_bytes(b"mine")
    assert has_cover(library / "P")
    assert save_cover(library, "P", JPEG, ".jpg") is None
    assert (library / "P" / existing).read_bytes() == b"mine"


class StubFetcher:
    def __init__(self, body=b"", content_type="image/jpeg", error=None):
        self.body, self.content_type, self.error = body, content_type, error
        self.calls = []

    def fetch_bytes(self, url, *, kind, max_bytes):
        self.calls.append((url, kind, max_bytes))
        if self.error:
            raise self.error
        return Fetched(url, self.content_type, self.body, len(self.body))


def test_the_cover_is_checked_by_its_first_bytes():
    assert PodcastFeedClient(StubFetcher(JPEG)).cover("https://x.example/c") == (JPEG, ".jpg")
    assert PodcastFeedClient(StubFetcher(PNG, "image/png")).cover("https://x.example/c") == (
        PNG,
        ".png",
    )
    assert PodcastFeedClient(StubFetcher(b"<html>")).cover("https://x.example/c") is None
    assert PodcastFeedClient(StubFetcher(b"GIF89a....")).cover("https://x.example/c") is None


def test_a_cover_that_cannot_be_fetched_is_none_not_an_error():
    client = PodcastFeedClient(StubFetcher(error=FetchError("blocked", "no")))
    assert client.cover("https://x.example/c") is None


def test_loading_a_feed_turns_fetch_errors_into_podcast_errors():
    client = PodcastFeedClient(StubFetcher(error=FetchError("not_found", "gone")))
    with pytest.raises(PodcastError) as error:
        client.load(FEED)
    assert (error.value.kind, error.value.message) == ("not_found", "gone")


def test_loading_a_feed_reads_it_from_the_normalised_address():
    fetcher = StubFetcher(rss(NEW), "application/rss+xml")
    podcast = PodcastFeedClient(fetcher).load("HTTPS://Podcast.Example/feed.xml#x")
    assert fetcher.calls[0][:2] == (FEED, "feed")
    assert podcast.feed_url == FEED
    assert podcast.episodes[0].key == episode_key(FEED, "g-new")


def test_os_replace_is_used_so_a_rename_is_atomic(library, tmp_path, monkeypatch):
    calls = []
    real = os.replace
    monkeypatch.setattr(
        os, "replace", lambda a, b: (calls.append((Path(a).name, Path(b).name)), real(a, b))[1]
    )
    place_episode(download(tmp_path), library, "P", date(2026, 1, 1), "Pilot", ".mp3")
    assert calls == [("episode.part", "2026-01-01 - Pilot.mp3")]
