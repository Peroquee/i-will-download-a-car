"""Channels are listed with yt-dlp, the RSS feed is the fallback (the feed answers 404 for whole
networks, 2026-11): the lister, the walk through the list, the errors and the single alert."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.helpers.fake_probe import FakeTraitsProbe
from tests.unit.test_feeds import entry, feed
from tests.unit.test_subscription_poller import (
    CID,
    NOW,
    FakeFeeds,
    add,
    make_poller,
)
from ytp import alertlog, jobs, seen_videos, skipped_videos, subscriptions
from ytp.channel_videos import ChannelLister, _list_with_ytdlp
from ytp.db import Database
from ytp.feeds import (
    CHANNEL_GONE,
    SERVICE_BOT,
    SERVICE_DOWN,
    SERVICE_RSS_404,
    SERVICE_WIDE,
    FeedError,
    is_service_wide,
)
from ytp.monitor import AlertMonitor
from ytp.subscription_poller import MAX_PROBES_PER_POLL
from ytp.video_filter import VideoTraits
from ytp.youtube import ProbeError, YoutubeProbe


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


VIDS = [f"vid{i:08d}"[-11:] for i in range(60)]  # 11 characters each
assert all(len(v) == 11 for v in VIDS)


def v(n: int) -> str:
    return VIDS[n]


def traits_at(days: float) -> VideoTraits:
    """What yt-dlp says about a video published `days` after NOW."""
    return VideoTraits(live_status="not_live", duration=600, width=1920, height=1080,
                       published=NOW + timedelta(days=days))  # fmt: skip


class FakeListing:
    """The `extract` of a ChannelLister: the channel's list, or an error, no network."""

    def __init__(self, *video_ids: str):
        self.videos = list(video_ids)  # newest first
        self.error: Exception | None = None
        self.calls: list[tuple[str, dict, int]] = []

    def __call__(self, url, options, limit):
        self.calls.append((url, options, limit))
        if self.error is not None:
            raise self.error
        return [{"id": video, "title": f"title {video}", "_type": "url"} for video in self.videos]


def lister_with(listing: FakeListing, **kw) -> ChannelLister:
    return ChannelLister(YoutubeProbe(proxy="http://gluetun:8888"), extract=listing, **kw)


def poller_for(db, listing, probe=None, feeds=None, **kw):
    probe = probe if probe is not None else FakeTraitsProbe()
    return make_poller(db, feeds, probe=probe, lister=lister_with(listing), **kw), probe


def queued_ids(db):
    return [j.metadata["video_id"] for j in sorted(jobs.list_jobs(db), key=lambda j: j.id)]


# --- the lister ---


def test_the_lister_asks_for_the_videos_tab_of_the_channel_flat_through_the_proxy():
    listing = FakeListing(v(1), v(2))
    entries = lister_with(listing, limit=30).list(CID)
    ((url, options, limit),) = listing.calls
    assert url == f"https://www.youtube.com/channel/{CID}/videos"
    assert limit == 30
    assert options["extract_flat"] == "in_playlist"
    assert options["allowed_extractors"] == ["youtube", "youtube:tab"]
    assert options["proxy"] == "http://gluetun:8888"
    assert options["skip_download"] is True
    assert options["cachedir"] is False
    assert [e.video_id for e in entries] == [v(1), v(2)], "newest first, as YouTube lists them"
    assert all(e.published is None and not e.short_link for e in entries)


def test_without_a_proxy_the_listing_asks_directly():
    listing = FakeListing(v(1))
    ChannelLister(YoutubeProbe(proxy=""), extract=listing).list(CID)
    assert "proxy" not in listing.calls[0][1]


def test_odd_entries_are_dropped_and_titles_cleaned():
    raw = [
        {"id": v(1), "title": "Fine\x00 title"},
        {"id": "short", "title": "bad id"},
        {"id": None},
        {"title": "no id"},
        {"id": v(2), "title": 5},
        "not a dict",
    ]
    lister = ChannelLister(YoutubeProbe(), extract=lambda u, o, n: raw)  # type: ignore[arg-type,return-value]
    got = lister.list(CID)
    assert [(e.video_id, e.title) for e in got] == [(v(1), "Fine title"), (v(2), "")]


@pytest.mark.parametrize("bad", ["", "UCshort", CID + "x", "../etc", "@handle", f"{CID}/../x"])
def test_only_a_channel_id_is_listed_and_nothing_is_asked_otherwise(bad):
    listing = FakeListing(v(1))
    with pytest.raises(ProbeError):
        lister_with(listing).list(bad)
    assert listing.calls == []


def test_errors_of_yt_dlp_become_probe_errors_and_bugs_too():
    failing = FakeListing()
    failing.error = ProbeError("bot_check", "proof")
    with pytest.raises(ProbeError) as error:
        lister_with(failing).list(CID)
    assert error.value.kind == "bot_check"
    failing.error = KeyError("boom")
    with pytest.raises(ProbeError) as error:
        lister_with(failing).list(CID)
    assert error.value.kind == "unknown"


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL: a lazy channel of 1000 videos, or an error."""

    pulled = 0
    error: Exception | None = None

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download, process):
        assert (download, process) == (False, False)
        if self.error is not None:
            raise self.error

        def pages():
            for number in range(1000):
                FakeYoutubeDL.pulled += 1
                yield {"id": f"{number:011d}", "title": str(number)}

        return {"_type": "playlist", "entries": pages()}


@pytest.fixture
def fake_ytdlp(monkeypatch):
    import yt_dlp

    FakeYoutubeDL.pulled = 0
    FakeYoutubeDL.error = None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


@pytest.mark.real_ytdlp
def test_only_the_asked_number_of_entries_is_pulled_from_the_endless_list(fake_ytdlp):
    entries = _list_with_ytdlp("https://www.youtube.com/channel/x/videos", {}, 30)
    assert len(entries) == 30
    assert fake_ytdlp.pulled <= 31, "the rest of the channel was not walked through"


@pytest.mark.real_ytdlp
@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("ERROR: [youtube:tab] UC..: YouTube said: This channel does not exist.", "not_found"),
        ("ERROR: Unable to download API page: HTTP Error 404: Not Found", "not_found"),
        ("ERROR: This account has been terminated", "not_found"),
        ("Sign in to confirm you're not a bot", "bot_check"),
        ("<urlopen error [Errno 111] Connection refused>", "network"),
        ("something nobody expected", "unknown"),
    ],
)
def test_yt_dlp_failures_are_sorted_into_kinds(fake_ytdlp, message, kind):
    import yt_dlp

    fake_ytdlp.error = yt_dlp.utils.DownloadError(message)
    with pytest.raises(ProbeError) as error:
        _list_with_ytdlp("https://www.youtube.com/channel/x/videos", {}, 30)
    assert error.value.kind == kind


# --- the walk through the list ---


def test_nothing_new_costs_one_look_the_first_time_and_none_after_that(db):
    sub = add(db)
    listing = FakeListing(*(v(n) for n in range(1, 6)))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-30)  # older than the subscription (2026-10-20)
    assert poller.poll(sub) == 0
    assert probe.asked == [v(1)], "only the newest video is dated: it is already old"
    assert seen_videos.ids(db, sub.id) == {v(n) for n in range(1, 6)}
    assert poller.poll(sub) == 0
    assert probe.asked == [v(1)], "the second poll stops at the first video it knows"
    assert jobs.list_jobs(db) == []


def test_a_new_video_is_dated_judged_and_queued_with_one_look(db):
    sub = add(db)
    listing = FakeListing(*(v(n) for n in range(1, 4)))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-30)
    poller.poll(sub)
    probe.asked.clear()
    listing.videos = [v(10), *listing.videos]
    probe.answers[v(10)] = traits_at(-1)
    assert poller.poll(sub) == 1
    assert queued_ids(db) == [v(10)]
    assert probe.asked == [v(10)], "date and verdict came from the same look"
    assert v(10) in seen_videos.ids(db, sub.id)


def test_several_new_videos_are_queued_oldest_first_and_the_walk_stops_at_the_first_old_one(db):
    sub = add(db)
    listing = FakeListing(v(3), v(2), v(1), v(0))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-30)
    probe.answers[v(3)] = traits_at(-1)
    probe.answers[v(2)] = traits_at(-2)
    probe.answers[v(1)] = traits_at(-3)
    assert poller.poll(sub) == 3
    assert queued_ids(db) == [v(1), v(2), v(3)]
    assert probe.asked == [v(3), v(2), v(1), v(0)], "newest to oldest, one look each, then old"


def test_the_back_catalogue_is_never_taken_but_backfill_takes_the_newest_older_ones(db):
    sub = add(db, backfill=2)
    listing = FakeListing(v(4), v(3), v(2), v(1))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-30)
    probe.answers[v(4)] = traits_at(-1)
    assert poller.poll(sub) == 3
    assert queued_ids(db) == [v(2), v(3), v(4)]
    # and not again at the next poll
    again = subscriptions.get(db, sub.id)
    assert poller.poll(again) == 0


def test_an_older_subscription_does_not_download_what_was_downloaded_before(db):
    """The 12 subscriptions that exist when this is installed: videos with a job are known, the
    walk needs one look at the first video that is neither known nor settled."""
    sub = add(db, checked=(NOW - timedelta(hours=3)).isoformat())
    top, below = v(1), v(2)
    for video in (top, below):
        jobs.submit_job(db, "youtube", video, '{"type": "youtube"}')
    listing = FakeListing(top, below, v(3), v(4))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-30)
    assert poller.poll(sub) == 0
    assert probe.asked == [v(3)], "only the first video without a job needed a date"


def test_a_passing_failure_of_a_look_postpones_everything_without_marking_anything(db):
    sub = add(db)
    listing = FakeListing(v(2), v(1))
    probe = FakeTraitsProbe({v(2): ProbeError("network", "down")})
    poller, _ = poller_for(db, listing, probe=probe)
    assert poller.poll(sub) == 0
    assert jobs.list_jobs(db) == []
    assert seen_videos.ids(db, sub.id) == set(), "nothing is settled, the next poll starts again"
    assert subscriptions.get(db, sub.id).last_error is None, "not an error of the channel"
    probe.answers[v(2)] = traits_at(-1)
    probe.default = traits_at(-30)
    assert poller.poll(subscriptions.get(db, sub.id)) == 1
    assert queued_ids(db) == [v(2)]


def test_a_bot_check_stops_the_looks_of_this_poll(db):
    sub = add(db)
    listing = FakeListing(*(v(n) for n in range(1, 6)))
    probe = FakeTraitsProbe()
    probe.default = ProbeError("bot_check", "proof")
    poller, _ = poller_for(db, listing, probe=probe)
    poller.poll(sub)
    assert probe.asked == [v(1)], "no more looks once YouTube asks for proof"


def test_only_a_few_looks_per_poll_and_the_rest_follow_at_the_next_one(db):
    sub = add(db)
    count = MAX_PROBES_PER_POLL + 4
    listing = FakeListing(*(v(n) for n in range(count, 0, -1)))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-1)  # all of them are new
    first = poller.poll(sub)
    assert first == MAX_PROBES_PER_POLL
    assert seen_videos.ids(db, sub.id) == set()
    probe.asked.clear()
    second = poller.poll(subscriptions.get(db, sub.id))
    assert first + second == count
    assert len(queued_ids(db)) == len(set(queued_ids(db))) == count, "nothing queued twice"


def test_the_looks_already_made_are_not_repeated_after_a_partial_poll(db):
    sub = add(db)
    count = MAX_PROBES_PER_POLL + 3
    listing = FakeListing(*(v(n) for n in range(count, 0, -1)))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-1)
    poller.poll(sub)
    probe.asked.clear()
    poller.poll(subscriptions.get(db, sub.id))
    newest_first = [v(n) for n in range(count, 0, -1)]
    assert not set(probe.asked) & set(newest_first[:MAX_PROBES_PER_POLL]), "they have jobs"


def test_a_live_stream_or_a_short_is_judged_once_and_not_queued(db):
    sub = add(db)
    listing = FakeListing(v(3), v(2), v(1))
    probe = FakeTraitsProbe()
    probe.default = traits_at(-30)
    probe.answers[v(3)] = VideoTraits(live_status="is_live", is_live=True, published=NOW)
    probe.answers[v(2)] = VideoTraits(
        live_status="not_live", duration=40, width=1080, height=1920, published=NOW
    )
    poller, _ = poller_for(db, listing, probe=probe)
    assert poller.poll(sub) == 0
    assert skipped_videos.skipped_ids(db, sub.id) == {v(3), v(2)}
    probe.asked.clear()
    assert poller.poll(subscriptions.get(db, sub.id)) == 0
    assert probe.asked == [], "judged once"


def test_a_premiere_that_cannot_be_read_yet_is_skipped_as_one(db):
    sub = add(db)
    listing = FakeListing(v(2), v(1))
    probe = FakeTraitsProbe({v(2): ProbeError("live", "premiere", upcoming=True)})
    probe.default = traits_at(-30)
    poller, _ = poller_for(db, listing, probe=probe)
    poller.poll(sub)
    ((video, reason),) = [
        (r["video_id"], r["reason"])
        for r in db.query("SELECT video_id, reason FROM skipped_videos")
    ]
    assert (video, reason) == (v(2), "premiere")


def test_a_video_that_can_never_be_looked_at_is_queued_so_the_job_names_the_reason(db):
    sub = add(db)
    listing = FakeListing(v(2), v(1))
    probe = FakeTraitsProbe({v(2): ProbeError("members", "members only")})
    probe.default = traits_at(-30)
    poller, _ = poller_for(db, listing, probe=probe)
    assert poller.poll(sub) == 1
    assert queued_ids(db) == [v(2)]


def test_a_podcast_subscription_gets_the_date_from_the_look(db):
    sub = add(db, target="podcast")
    listing = FakeListing(v(2), v(1))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-30)
    probe.answers[v(2)] = traits_at(-2)
    poller.poll(sub)
    (job,) = jobs.list_jobs(db)
    assert job.metadata["published"] == (NOW + timedelta(days=-2)).date().isoformat()


def test_the_followers_of_a_channel_own_its_new_videos(db):
    from ytp import accounts

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO accounts (id, username, password_hash, role) "
            "VALUES (7, 'ann', 'x', 'user')"
        )
    sub = add(db)
    accounts.follow(db, "channel", sub.id, 7)
    listing = FakeListing(v(2), v(1))
    poller, probe = poller_for(db, listing)
    probe.default = traits_at(-30)
    probe.answers[v(2)] = traits_at(-1)
    poller.poll(sub)
    (job,) = jobs.list_jobs(db)
    assert accounts.owns_job(db, job.id, 7)


def test_the_seen_list_goes_with_the_subscription_and_stays_short(db):
    sub = add(db)
    seen_videos.add(db, sub.id, [f"{n:011d}" for n in range(seen_videos.KEEP + 50)])
    assert len(seen_videos.ids(db, sub.id)) == seen_videos.KEEP
    subscriptions.delete(db, sub.id)
    assert db.query("SELECT COUNT(*) AS n FROM subscription_seen_videos")[0]["n"] == 0


# --- the fallback and the errors ---


VPN_DOWN = "the feed could not be fetched (is the VPN up?)"


def rss_feeds(**answers):
    feeds = FakeFeeds()
    feeds.answers.update(answers)
    return feeds


def test_when_yt_dlp_fails_the_rss_feed_is_used_and_the_poll_succeeds(db):
    sub = add(db)
    listing = FakeListing()
    listing.error = ProbeError("bot_check", "proof")
    feeds = rss_feeds(
        **{
            CID: feed(
                entry("n" * 11, "New one", (NOW - timedelta(hours=1)).isoformat()), channel=CID
            )
        }
    )
    poller, _ = poller_for(db, listing, feeds=feeds)
    assert poller.poll(sub) == 1
    assert feeds.fetched == [CID]
    assert subscriptions.get(db, sub.id).last_error is None


def test_the_rss_feed_is_not_asked_when_yt_dlp_works(db):
    """The poll itself never uses it. (A thin publication history is filled from it once, #80: see
    test_smart_poller; with a full history it is not asked at all.)"""
    from ytp import publish_history

    sub = add(db)
    base = NOW - timedelta(days=200)
    publish_history.record(
        db, "channel", sub.id, [(f"h{n}", base + timedelta(days=n, hours=n % 5)) for n in range(25)]
    )
    feeds = rss_feeds()
    poller, probe = poller_for(db, FakeListing(v(1)), feeds=feeds)
    probe.default = traits_at(-30)
    poller.poll(sub)
    assert feeds.fetched == []


@pytest.mark.parametrize(
    ("listing_kind", "rss_error", "kind", "text"),
    [
        ("unknown", FeedError("not_found", SERVICE_RSS_404), "temporary", SERVICE_DOWN),
        ("timeout", FeedError("temporary", "HTTP 503"), "temporary", SERVICE_DOWN),
        ("bot_check", FeedError("not_found", SERVICE_RSS_404), "temporary", SERVICE_BOT),
        ("unknown", FeedError("bad_feed", "garbled"), "temporary", SERVICE_DOWN),
        ("network", FeedError("not_found", SERVICE_RSS_404), "temporary", SERVICE_DOWN),
        ("network", FeedError("network", VPN_DOWN), "network", VPN_DOWN),
    ],
)  # fmt: skip
def test_when_both_fail_the_error_says_it_is_youtube_not_the_channel(
    db, listing_kind, rss_error, kind, text
):
    sub = add(db)
    listing = FakeListing()
    listing.error = ProbeError(listing_kind, "x")
    poller, _ = poller_for(db, listing, feeds=rss_feeds(**{CID: rss_error}))
    with pytest.raises(FeedError) as error:
        poller.poll(sub)
    assert (error.value.kind, error.value.message) == (kind, text)
    assert subscriptions.get(db, sub.id).last_error == text
    assert "no feed for this channel" not in text


def test_a_channel_that_does_not_exist_says_so_and_does_not_ask_the_feed(db):
    sub = add(db)
    listing = FakeListing()
    listing.error = ProbeError("not_found", "gone")
    feeds = rss_feeds()
    poller, _ = poller_for(db, listing, feeds=feeds)
    with pytest.raises(FeedError) as error:
        poller.poll(sub)
    assert (error.value.kind, error.value.message) == ("not_found", CHANNEL_GONE)
    assert feeds.fetched == []
    assert not is_service_wide(CHANNEL_GONE)


def test_the_rss_feed_alone_no_longer_claims_the_channel_has_no_feed(db):
    sub = add(db)
    poller = make_poller(db, rss_feeds(**{CID: FeedError("not_found", SERVICE_RSS_404)}))
    with pytest.raises(FeedError) as error:
        poller.poll(sub)
    assert "not about this channel" in error.value.message


def test_the_service_wide_texts_are_what_the_monitor_recognises():
    assert {SERVICE_DOWN, SERVICE_BOT, SERVICE_RSS_404} == SERVICE_WIDE
    assert all(is_service_wide(t) for t in SERVICE_WIDE)
    assert not is_service_wide("something about one channel")
    assert not is_service_wide(None)


def test_a_run_over_many_channels_records_the_same_service_error_for_each(db):
    ids = [f"UC{n:022d}" for n in range(12)]
    for number, cid in enumerate(ids):
        add(db, cid, f"Channel {number}")
    listing = FakeListing()
    listing.error = ProbeError("unknown", "x")
    poller, _ = poller_for(
        db, listing, feeds=rss_feeds(**{c: FeedError("not_found", SERVICE_RSS_404) for c in ids})
    )
    result = poller.run_once()
    assert result.failed == len(ids)
    assert {s.last_error for s in subscriptions.list_all(db)} == {SERVICE_DOWN}


def test_with_the_vpn_down_a_run_stops_after_the_first_channel(db):
    for number in range(5):
        add(db, f"UC{number:022d}", f"C{number}")
    listing = FakeListing()
    listing.error = ProbeError("network", "x")
    down = FeedError("network", "the feed could not be fetched (is the VPN up?)")
    feeds = FakeFeeds()
    feeds.answers = {f"UC{n:022d}": down for n in range(5)}
    poller, _ = poller_for(db, listing, feeds=feeds)
    result = poller.run_once()
    assert result.failed == 1
    assert len(feeds.fetched) == 1, "the other four channels were not tried against a dead line"


# --- one alert for all channels ---


def monitor(db):
    class Status:
        def fetch_status(self):
            return {}

    return AlertMonitor(db, Status(), "/tmp")


def fail_all(db, text, count=12):
    for number in range(count):
        cid = f"UC{number:022d}"
        sub = subscriptions.get(db, number + 1) or add(db, cid, f"Channel {number}")
        subscriptions.record_poll(db, sub.id, text, NOW)


def kinds(db):
    return [a.kind for a in alertlog.list_alerts(db)]


def test_twelve_channels_with_the_same_youtube_error_raise_one_alert(db):
    fail_all(db, SERVICE_DOWN)
    monitor(db).check_feeds()
    monitor(db).check_feeds()
    assert kinds(db) == ["youtube_unavailable"]
    (alert,) = alertlog.list_alerts(db)
    assert alert.params["reason"] == SERVICE_DOWN
    assert alert.severity == "warning"


def test_a_channel_with_an_error_of_its_own_still_gets_its_own_alert(db):
    fail_all(db, SERVICE_DOWN, count=3)
    subscriptions.record_poll(db, 1, CHANNEL_GONE, NOW)
    monitor(db).check_feeds()
    assert sorted(kinds(db)) == ["feed_error", "youtube_unavailable"]


def test_channels_failing_one_after_the_other_do_not_add_alerts(db):
    for number in range(4):
        add(db, f"UC{number:022d}", f"C{number}")
    for number in range(1, 5):
        subscriptions.record_poll(db, number, SERVICE_DOWN, NOW)
        monitor(db).check_feeds()  # the monitor looks between two channels of one run
    assert kinds(db) == ["youtube_unavailable"]


def test_when_it_works_again_and_fails_again_it_is_a_new_alert(db):
    fail_all(db, SERVICE_DOWN, count=3)
    monitor(db).check_feeds()
    for number in range(1, 4):
        subscriptions.record_poll(db, number, None, NOW)
    monitor(db).check_feeds()
    fail_all(db, SERVICE_DOWN, count=3)
    monitor(db).check_feeds()
    assert kinds(db) == ["youtube_unavailable", "youtube_unavailable"]


def test_a_different_youtube_error_is_a_second_alert(db):
    fail_all(db, SERVICE_DOWN, count=2)
    monitor(db).check_feeds()
    fail_all(db, SERVICE_BOT, count=2)
    monitor(db).check_feeds()
    assert kinds(db) == ["youtube_unavailable", "youtube_unavailable"]


def test_a_podcast_with_that_text_is_not_swallowed(db):
    """Only channels are grouped; a podcast feed error keeps its own alert."""
    from ytp import podcast_subscriptions

    sub, _ = podcast_subscriptions.add(db, "https://p.example/f.xml", "Show")
    podcast_subscriptions.record_poll(db, sub.id, SERVICE_DOWN)
    monitor(db).check_feeds()
    assert kinds(db) == ["feed_error"]


def test_somebody_new_following_the_channel_makes_the_next_poll_walk_the_list_again(db):
    sub = add(db)
    seen_videos.add(db, sub.id, [v(1), v(2)])
    seen_videos.clear(db, sub.id)
    assert seen_videos.ids(db, sub.id) == set()
