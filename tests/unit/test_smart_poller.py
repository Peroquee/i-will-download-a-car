# ruff: noqa: E501, F811
"""The poller and the smart mode (issue #80): the publication history it keeps, and which feeds are
due when."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.fake_probe import ORDINARY, FakeTraitsProbe
from tests.unit.test_feeds import CID, entry, feed
from tests.unit.test_podcast_jobs import world  # noqa: F401
from tests.unit.test_subscription_poller import (  # noqa: F401
    FakeFeeds,
    FakeWorker,
    add,
    db,
    make_poller,
)
from ytp import podcast_subscriptions, publish_history, subscriptions
from ytp.config import Settings
from ytp.feeds import FeedEntry, FeedError
from ytp.poll_schedule import Schedule
from ytp.server_defaults import ServerDefaults
from ytp.video_filter import VideoTraits

BERLIN = ZoneInfo("Europe/Berlin")
# a Tuesday at 12:00 Berlin; the channel publishes Tuesdays 17:00
NOW = datetime(2026, 11, 10, 11, 0, tzinfo=UTC)


def tuesdays(count=10, hour=17, minute=0, end=NOW):
    """`count` publication times: the Tuesdays before `end`, at hour:minute Berlin time."""
    found = []
    day = end.astimezone(BERLIN).date()
    while len(found) < count:
        day -= timedelta(days=1)
        if day.weekday() == 1:
            found.append(
                datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN).astimezone(UTC)
            )
    return found


def teach(db, sub_id, times, kind="channel"):
    publish_history.record(db, kind, sub_id, [(f"v{n}", when) for n, when in enumerate(times)])


def poller_at(db, now, feeds=None, **kw):
    poller = make_poller(db, feeds, **kw)
    poller._now = lambda: now
    poller.zone = BERLIN
    return poller


def at(hour, minute=0, day=10):
    return datetime(2026, 11, day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


# --- the history store ----------------------------------------------------------------------------------


def test_publication_times_are_kept_once_per_item_and_come_back_newest_first(db):
    first = tuesdays(3)
    assert (
        publish_history.record(
            db, "channel", 1, [("a", first[2]), ("b", first[1]), ("c", first[0])]
        )
        == 3
    )
    assert publish_history.record(db, "channel", 1, [("a", first[2]), ("d", at(9))]) == 1, (
        "a known item is ignored"
    )
    times = publish_history.times(db, "channel", 1)
    assert times == sorted(times, reverse=True)
    assert publish_history.count(db, "channel", 1) == 4
    assert publish_history.count(db, "podcast", 1) == 0, "the kinds are separate"
    assert publish_history.count(db, "channel", 2) == 0


def test_only_the_newest_hundred_are_kept(db):
    base = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    items = [(f"v{n}", base + timedelta(days=n)) for n in range(130)]
    publish_history.record(db, "channel", 1, items)
    kept = publish_history.times(db, "channel", 1)
    assert len(kept) == publish_history.KEEP == 100
    assert kept[0] == base + timedelta(days=129)
    assert kept[-1] == base + timedelta(days=30)


@pytest.mark.parametrize(
    "moment",
    [datetime(2026, 11, 3, 23, 59, 59, tzinfo=UTC), datetime(2026, 11, 3, 0, 0, tzinfo=UTC)],
)
def test_a_date_without_a_time_of_day_is_not_remembered(db, moment):
    """yt-dlp's upload_date stands in as 23:59:59 UTC: it would teach a wrong hour."""
    assert publish_history.record(db, "channel", 1, [("a", moment)]) == 0
    assert publish_history.count(db, "channel", 1) == 0


def test_unsubscribing_forgets_the_history_so_a_reused_id_starts_clean(db):
    sub_ = add(db)
    teach(db, sub_.id, tuesdays(4))
    subscriptions.delete(db, sub_.id)
    assert publish_history.count(db, "channel", sub_.id) == 0
    pod, _ = podcast_subscriptions.add(db, "https://example.org/feed.xml", "Cast")
    teach(db, pod.id, tuesdays(4), kind="podcast")
    podcast_subscriptions.delete(db, pod.id)
    assert publish_history.count(db, "podcast", pod.id) == 0


# --- the poll fills it ------------------------------------------------------------------------------------


def test_a_poll_of_the_rss_feed_remembers_the_times_of_all_its_entries(db):
    add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(
        entry("aaaaaaaaaaa", "One", "2026-11-03T16:00:12+00:00"),
        entry("bbbbbbbbbbb", "Two", "2026-10-27T16:03:40+00:00"),
    )
    make_poller(db, feeds).run_once()
    sub_ = subscriptions.list_all(db)[0]
    assert publish_history.count(db, "channel", sub_.id) == 2
    assert publish_history.times(db, "channel", sub_.id)[0] == datetime(
        2026, 11, 3, 16, 0, 12, tzinfo=UTC
    )


class Lister:
    """yt-dlp's list: newest first, and no times."""

    def __init__(self, *ids):
        self.ids = ids

    def list(self, channel_id):
        return [FeedEntry(video_id, f"title {video_id}", None) for video_id in self.ids]


def test_with_a_ytdlp_list_the_first_poll_fills_the_history_from_the_rss_feed(db):
    """The list has no times; the very first poll asks the RSS feed once, so no weeks of waiting."""
    add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(
        *[entry(f"v{n:010d}", "x", t.isoformat()) for n, t in enumerate(tuesdays(8))]
    )
    poller = make_poller(db, feeds, lister=Lister("zzzzzzzzzzz"), probe=FakeTraitsProbe())
    poller.run_once()
    sub_ = subscriptions.list_all(db)[0]
    assert publish_history.count(db, "channel", sub_.id) >= 8
    assert feeds.fetched == [CID], "one request to the feed"
    assert sub_.history_checked_at is not None


def test_the_history_is_not_asked_for_again_within_a_week_even_if_it_stays_thin(db):
    sub_ = add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(entry("aaaaaaaaaaa", "One", "2026-11-03T16:00:12+00:00"))
    clock = [NOW]
    poller = make_poller(db, feeds, lister=Lister("zzzzzzzzzzz"), probe=FakeTraitsProbe())
    poller._now = lambda: clock[0]
    poller.poll(subscriptions.get(db, sub_.id))
    clock[0] = NOW + timedelta(days=3)
    poller.poll(subscriptions.get(db, sub_.id))
    assert feeds.fetched == [CID]
    clock[0] = NOW + timedelta(days=8)
    poller.poll(subscriptions.get(db, sub_.id))
    assert feeds.fetched == [CID, CID], "after a week it is asked again"


def test_a_channel_whose_feed_is_gone_is_left_to_the_interval_without_an_error(db):
    sub_ = add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = FeedError("not_found", "no feed")
    poller = make_poller(db, feeds, lister=Lister("zzzzzzzzzzz"), probe=FakeTraitsProbe())
    poller.poll(subscriptions.get(db, sub_.id))
    after = subscriptions.get(db, sub_.id)
    assert after.last_error is None
    assert after.history_checked_at is not None, "and not asked again at every poll"
    assert publish_history.count(db, "channel", sub_.id) == 0


def test_a_full_history_is_not_refilled(db):
    sub_ = add(db)
    teach(db, sub_.id, tuesdays(25))
    feeds = FakeFeeds()
    make_poller(db, feeds, lister=Lister("zzzzzzzzzzz"), probe=FakeTraitsProbe()).poll(
        subscriptions.get(db, sub_.id)
    )
    assert feeds.fetched == []


def test_a_video_yt_dlp_was_asked_about_adds_its_exact_time(db):
    sub_ = add(db)
    when = datetime(2026, 11, 4, 16, 2, 30, tzinfo=UTC)
    probe = FakeTraitsProbe(
        {
            "nnnnnnnnnnn": VideoTraits(
                live_status="not_live", duration=600, width=1920, height=1080, published=when
            )
        }
    )
    feeds = FakeFeeds()
    feeds.answers[CID] = feed()  # an empty feed: nothing to learn from the fill
    make_poller(db, feeds, lister=Lister("nnnnnnnnnnn"), probe=probe).poll(
        subscriptions.get(db, sub_.id)
    )
    assert when in publish_history.times(db, "channel", sub_.id)
    assert ORDINARY.published is None


def test_a_failed_history_never_fails_the_poll(db, monkeypatch):
    sub_ = add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(entry("aaaaaaaaaaa", "One", "2026-11-03T16:00:12+00:00"))

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(publish_history, "record", boom)
    make_poller(db, feeds).poll(subscriptions.get(db, sub_.id))
    assert subscriptions.get(db, sub_.id).last_error is None


def test_a_podcast_poll_remembers_every_episode_time_and_a_podcast_is_smart_too(world):
    from tests.unit import test_podcast_jobs as pj

    world.fetcher.feeds[pj.FEED] = pj.feed_of(
        pj.episode_item("e1", "One", -3),
        pj.episode_item("e2", "Two", -10),
        pj.episode_item("e3", "Three", -17),
    )
    sub_ = pj.subscribe(world)
    pj.make_poller(world).poll(podcast_subscriptions.get(world.db, sub_.id))
    assert publish_history.count(world.db, "podcast", sub_.id) == 3
    assert publish_history.count(world.db, "channel", sub_.id) == 0
    poller = pj.make_poller(world)
    assert poller.kind == "podcast"
    assert poller.schedule_of(podcast_subscriptions.get(world.db, sub_.id)).mode == "smart"
    assert poller._fill_history(sub_) is None, "a podcast feed has its times on every poll"


# --- which feeds are due ------------------------------------------------------------------------------------


def test_with_a_learned_pattern_a_channel_is_checked_when_the_window_begins_and_not_before(db):
    sub_ = add(db, checked=(at(14, 0)).isoformat())
    teach(db, sub_.id, tuesdays(10))
    assert [s.id for s in poller_at(db, at(16, 40)).due()] == []
    assert [s.id for s in poller_at(db, at(16, 55)).due()] == [sub_.id]


def test_inside_the_window_it_is_checked_every_few_minutes_and_not_every_minute(db):
    sub_ = add(db, checked=(at(16, 56)).isoformat())
    teach(db, sub_.id, tuesdays(10))
    assert poller_at(db, at(17, 0)).due() == [], "4 minutes after the last check"
    assert [s.id for s in poller_at(db, at(17, 3)).due()] == [sub_.id]


def test_without_a_pattern_the_channel_follows_its_interval(db):
    sub_ = add(db, checked=(at(10, 0)).isoformat())
    assert poller_at(db, at(10, 40)).due() == []
    assert [s.id for s in poller_at(db, at(12, 0)).due()] == [sub_.id], (
        "60 min interval (x 0.8..1.2)"
    )


def test_the_fixed_mode_ignores_the_pattern(db):
    sub_ = add(db, checked=(at(14, 0)).isoformat())
    teach(db, sub_.id, tuesdays(10))
    subscriptions.update_settings(db, sub_.id, {"poll_mode": "fixed"})
    assert [s.id for s in poller_at(db, at(16, 55)).due()] == [sub_.id], (
        "due by the 60 min interval, 2 h ago"
    )


def test_a_subscriptions_own_mode_beats_the_server_default(db, tmp_path):
    defaults = ServerDefaults(lambda: db, Settings.from_env({
        "MEDIA_DIR_MOVIES": "Filme", "MEDIA_DIR_SERIES": "Serien", "MEDIA_DIR_AUDIOBOOKS": "Hb",
        "MEDIA_DIR_MUSIC": "Musik", "MEDIA_DIR_PODCASTS": "Podcasts", "MEDIA_DIR_YOUTUBE": "Youtube",
        "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(tmp_path),
    }), zone=BERLIN)  # fmt: skip
    smart_one = add(db, "UC" + "a" * 22, "A", checked=(at(15, 0)).isoformat())
    fixed_one = add(db, "UC" + "b" * 22, "B", checked=(at(15, 0)).isoformat())
    teach(db, smart_one.id, tuesdays(10))
    teach(db, fixed_one.id, tuesdays(10))
    subscriptions.update_settings(db, fixed_one.id, {"poll_mode": "fixed", "poll_minutes": 1440})
    poller = poller_at(db, at(16, 0))
    poller.defaults = defaults
    assert poller.due() == [], "smart: base interval, and the window is at 16:50; fixed: 24 h"
    defaults.update({"poll_mode": "fixed"})
    subscriptions.update_settings(db, smart_one.id, {"poll_mode": "smart"})
    assert poller.schedule_of(subscriptions.get(db, smart_one.id)).mode == "smart"
    assert poller.schedule_of(subscriptions.get(db, fixed_one.id)).mode == "fixed"
    other = add(db, "UC" + "c" * 22, "C")
    assert poller.schedule_of(other).mode == "fixed", (
        "a subscription without a mode follows the server's"
    )


def test_a_feed_in_its_window_is_checked_before_one_that_is_merely_overdue(db):
    overdue = add(
        db, "UC" + "a" * 22, "A", checked=(at(1, 0)).isoformat()
    )  # not smart-learned: interval
    inwindow = add(db, "UC" + "b" * 22, "B", checked=(at(16, 40)).isoformat())
    teach(db, inwindow.id, tuesdays(10))
    due = poller_at(db, at(17, 5)).due()
    assert [s.id for s in due] == [inwindow.id, overdue.id]


def test_a_paused_subscription_is_never_due_in_the_smart_mode(db):
    sub_ = add(db, checked=(at(14, 0)).isoformat())
    teach(db, sub_.id, tuesdays(10))
    subscriptions.update_settings(db, sub_.id, {"poll_minutes": 0})
    assert poller_at(db, at(17, 0)).due() == []


def test_a_found_video_ends_the_intensive_window(db):
    sub_ = add(db, checked=(at(17, 3)).isoformat())
    teach(db, sub_.id, tuesdays(10))
    publish_history.record(
        db, "channel", sub_.id, [("fresh", at(16, 58))]
    )  # published in the window
    assert poller_at(db, at(17, 20)).due() == [], "found: the rest of the window is checked slowly"


def test_the_schedule_of_a_new_subscription_is_smart_by_default_and_checks_at_once(db):
    sub_ = add(db)
    poller = poller_at(db, at(9, 0))
    assert poller.schedule_of(sub_).mode == "smart"
    assert [s.id for s in poller.due()] == [sub_.id]


# --- errors back off -------------------------------------------------------------------------------------------


def test_failed_checks_in_a_row_are_counted_and_a_good_one_resets_the_count(db):
    sub_ = add(db)
    for _ in range(3):
        subscriptions.record_poll(db, sub_.id, "down", NOW)
    assert subscriptions.get(db, sub_.id).poll_errors == 3
    subscriptions.record_poll(db, sub_.id, None, NOW)
    assert subscriptions.get(db, sub_.id).poll_errors == 0


def test_a_feed_that_keeps_failing_is_retried_later_and_later(db):
    sub_ = add(db)
    for _ in range(3):
        subscriptions.record_poll(db, sub_.id, "down", at(10, 0))
    # 3 failures in a row: 40 minutes, not 10
    assert poller_at(db, at(10, 30)).due() == []
    assert [s.id for s in poller_at(db, at(10, 41)).due()] == [sub_.id]


def test_the_podcast_store_counts_failures_too(db):
    pod, _ = podcast_subscriptions.add(db, "https://example.org/feed.xml", "Cast")
    podcast_subscriptions.record_poll(db, pod.id, "bad", NOW)
    podcast_subscriptions.record_poll(db, pod.id, "bad", NOW)
    assert podcast_subscriptions.get(db, pod.id).poll_errors == 2
    podcast_subscriptions.record_poll(db, pod.id, None, NOW)
    assert podcast_subscriptions.get(db, pod.id).poll_errors == 0


def test_the_schedule_dataclass_carries_the_mode():
    assert Schedule(60).mode == "fixed"
    assert Schedule(60, None, "smart").mode == "smart"
    assert FakeWorker().woken == 0
