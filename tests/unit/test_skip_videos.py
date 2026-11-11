"""Subscriptions leave out lives, premieres and Shorts (issue #39): the verdict rules, the poller
with fake probe results, what is remembered and what the subscription list shows."""

from __future__ import annotations

import logging

import pytest

from tests.helpers.fake_probe import ORDINARY, FakeTraitsProbe
from tests.helpers.feedxml import CID, entry, feed
from tests.unit.test_subscription_poller import (
    FakeFeeds,
    FakeWorker,
    add,
    iso,
    make_poller,
    queued,
)
from ytp import jobs, skipped_videos, subscriptions
from ytp.db import Database
from ytp.feeds import parse_feed
from ytp.video_filter import SHORT_MAX_SECONDS, VideoTraits, skip_reason
from ytp.youtube import ProbeError, VideoRef, YoutubeProbe, classify

V1, V2, V3, V4 = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


# --- the verdict -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("traits", "expected"),
    [
        (VideoTraits(live_status="not_live", duration=600, width=1920, height=1080), None),
        (VideoTraits(), None),
        (VideoTraits(live_status="is_live"), "live"),
        (VideoTraits(is_live=True), "live"),
        (VideoTraits(live_status="was_live", duration=7200), "live"),
        (VideoTraits(was_live=True), "live"),
        (VideoTraits(media_type="livestream"), "live"),
        (VideoTraits(live_status="is_upcoming"), "premiere"),
        (VideoTraits(media_type="short"), "short"),
        (VideoTraits(webpage_url="https://www.youtube.com/shorts/aaaaaaaaaaa"), "short"),
        # short AND vertical
        (VideoTraits(duration=45, width=1080, height=1920), "short"),
        (VideoTraits(duration=SHORT_MAX_SECONDS, width=720, height=1280), "short"),
        # short but landscape, long but vertical, size unknown: ordinary videos
        (VideoTraits(duration=45, width=1920, height=1080), None),
        (VideoTraits(duration=SHORT_MAX_SECONDS + 1, width=720, height=1280), None),
        (VideoTraits(duration=45), None),
        (VideoTraits(duration=45, width=1080, height=1080), None),
    ],
)
def test_the_verdict_for_what_yt_dlp_reports(traits, expected):
    assert skip_reason(traits) == expected


def test_a_live_stream_wins_over_the_other_signals():
    assert skip_reason(VideoTraits(live_status="is_live", media_type="short")) == "live"


def test_traits_are_read_from_yt_dlps_answer_with_the_tallest_format_as_the_size():
    info = {
        "live_status": "not_live",
        "duration": 59.7,
        "formats": [
            {"width": 360, "height": 640},
            {"width": 1080, "height": 1920},
            "junk",
            {"width": None, "height": 5000},
        ],
        "title": "ignored",
    }
    traits = VideoTraits.from_info(info)
    assert (traits.duration, traits.width, traits.height) == (59, 1080, 1920)
    assert skip_reason(traits) == "short"


def test_odd_values_in_the_answer_do_not_crash():
    traits = VideoTraits.from_info(
        {"live_status": 5, "is_live": "yes", "duration": "long", "width": -1, "formats": "x"}
    )
    assert traits == VideoTraits()


def test_the_probe_returns_the_traits_through_the_same_options():
    seen = {}

    def extract(url, options):
        seen["url"], seen["options"] = url, options
        return {"live_status": "is_upcoming"}

    probe = YoutubeProbe(proxy="http://gluetun:8888", extract=extract)
    traits = probe.traits_blocking(VideoRef(V1))
    assert traits.live_status == "is_upcoming"
    assert seen["url"] == f"https://www.youtube.com/watch?v={V1}"
    assert seen["options"]["proxy"] == "http://gluetun:8888"
    assert seen["options"]["skip_download"] is True


# --- the free signal in the feed -------------------------------------------------------------


def shorts_entry(video_id, title, published):
    return entry(video_id, title, published).replace(
        f"https://www.youtube.com/watch?v={video_id}", f"https://www.youtube.com/shorts/{video_id}"
    )


def test_a_shorts_link_in_the_feed_is_noticed():
    got = parse_feed(feed(shorts_entry(V1, "S", iso(-1)), entry(V2, "N", iso(-2))), CID)
    assert {e.video_id: e.short_link for e in got} == {V1: True, V2: False}


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/shorts/aaaaaaaaaaa",
        "https://www.youtube.com.evil.example/shorts/aaaaaaaaaaa",
        "https://www.youtube.com/watch?v=aaaaaaaaaaa&amp;x=/shorts/",
    ],
)
def test_only_a_youtube_shorts_address_counts(href):
    text = entry(V1, "S", iso(-1)).replace(f"https://www.youtube.com/watch?v={V1}", href)
    assert parse_feed(feed(text), CID)[0].short_link is False


# --- the poller ----------------------------------------------------------------------------------


def run(db, entries, probe, **kw):
    sub = add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(*entries)
    worker = FakeWorker()
    poller = make_poller(db, feeds, worker=worker, probe=probe, **kw)
    return sub, poller, feeds, poller.run_once()


def test_ordinary_videos_are_queued_and_probed_once(db):
    probe = FakeTraitsProbe()
    _, _, _, result = run(db, [entry(V1, "A", iso(-1)), entry(V2, "B", iso(-0.5))], probe)
    assert result.queued == 2
    assert sorted(probe.asked) == [V1, V2]
    assert skipped_videos.skipped_ids(db, 1) == set()


def test_lives_premieres_and_shorts_are_not_queued_but_remembered(db):
    probe = FakeTraitsProbe(
        {
            V1: VideoTraits(live_status="is_live"),
            V2: VideoTraits(live_status="is_upcoming"),
            V3: VideoTraits(duration=30, width=1080, height=1920),
            V4: ORDINARY,
        }
    )
    sub, _, _, result = run(
        db,
        [entry(v, f"title {v}", iso(-1 + i / 100)) for i, v in enumerate((V1, V2, V3, V4))],
        probe,
    )
    assert result.queued == 1
    assert [ext for ext, _, _ in queued(db)] == [V4]
    info = skipped_videos.summary(db, sub.id)
    assert info["count"] == 3
    assert info["by_reason"] == {"live": 1, "premiere": 1, "short": 1}
    assert {r["video_id"]: r["reason"] for r in info["recent"]} == {
        V1: "live",
        V2: "premiere",
        V3: "short",
    }
    assert subscriptions.get(db, sub.id).last_error is None, "a skip is not a failure"


def test_a_shorts_link_needs_no_probe(db):
    probe = FakeTraitsProbe()
    sub, _, _, result = run(db, [shorts_entry(V1, "S", iso(-1))], probe)
    assert result.queued == 0
    assert probe.asked == []
    assert skipped_videos.summary(db, sub.id)["by_reason"]["short"] == 1


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("ERROR: [youtube] x: Premieres in 2 hours", "premiere"),
        ("ERROR: [youtube] x: This live event will begin in 3 days", "premiere"),
        ("ERROR: [youtube] x: This live stream recording is not available.", "live"),
    ],
)
def test_yt_dlp_refusing_a_video_is_classified_by_its_own_words(db, message, reason):
    probe = FakeTraitsProbe({V1: classify(message)})
    sub, _, _, result = run(db, [entry(V1, "P", iso(-1))], probe)
    assert result.queued == 0
    assert skipped_videos.skipped_ids(db, sub.id) == {V1}
    assert skipped_videos.summary(db, sub.id)["by_reason"][reason] == 1


def test_yt_dlp_refusing_an_upcoming_video_means_premiere(db):
    probe = FakeTraitsProbe({V1: ProbeError("live", "Premieres in 2 hours", upcoming=True)})
    sub, _, _, result = run(db, [entry(V1, "P", iso(-1))], probe)
    assert result.queued == 0
    assert skipped_videos.summary(db, sub.id)["by_reason"]["premiere"] == 1


def test_a_skipped_video_is_not_probed_again(db):
    probe = FakeTraitsProbe({V1: VideoTraits(live_status="is_live")})
    _, poller, _, _ = run(db, [entry(V1, "L", iso(-1))], probe)
    assert probe.asked == [V1]
    with db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET last_checked = NULL")
    assert poller.run_once().queued == 0
    assert probe.asked == [V1], "judged once"


def test_a_video_that_already_has_a_job_is_never_probed(db):
    jobs.submit_job(db, "youtube", V1, '{"type": "youtube"}')
    jobs.mark_failed(db, 1, "it failed")
    probe = FakeTraitsProbe()
    _, _, _, result = run(db, [entry(V1, "A", iso(-1))], probe)
    assert probe.asked == []
    assert result.queued == 0
    assert jobs.get_job(db, 1).status == "failed", "still not re-queued by the poller"


def test_without_a_probe_every_new_video_is_queued_as_before(db):
    _, _, _, result = run(db, [entry(V1, "A", iso(-1))], None)
    assert result.queued == 1


@pytest.mark.parametrize("kind", ["network", "bot_check"])
def test_a_probe_that_cannot_reach_youtube_postpones_the_rest_to_the_next_poll(db, kind):
    probe = FakeTraitsProbe({V1: ProbeError(kind, "down")})
    sub, poller, _, result = run(db, [entry(V1, "A", iso(-1)), entry(V2, "B", iso(-0.5))], probe)
    assert result.queued == 0
    assert probe.asked == [V1], "no point asking again while YouTube is out of reach"
    assert skipped_videos.skipped_ids(db, sub.id) == set(), "nothing is remembered"
    assert subscriptions.get(db, sub.id).last_error is None
    probe.answers.clear()  # YouTube is back
    with db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET last_checked = NULL")
    assert poller.run_once().queued == 2


def test_a_timeout_postpones_only_that_video(db):
    probe = FakeTraitsProbe({V1: ProbeError("timeout", "slow")})
    _, _, _, result = run(db, [entry(V1, "A", iso(-1)), entry(V2, "B", iso(-0.5))], probe)
    assert [ext for ext, _, _ in queued(db)] == [V2]
    assert result.queued == 1


@pytest.mark.parametrize("kind", ["private", "unavailable", "members", "age", "region", "unknown"])
def test_a_video_the_probe_cannot_read_for_its_own_reasons_is_queued_so_the_job_says_why(db, kind):
    probe = FakeTraitsProbe({V1: ProbeError(kind, "no")})
    _, _, _, result = run(db, [entry(V1, "A", iso(-1))], probe)
    assert result.queued == 1


def test_at_most_a_few_probes_per_poll_the_rest_wait(db):
    from ytp.subscription_poller import MAX_PROBES_PER_POLL

    ids = [f"{chr(97 + i)}" * 11 for i in range(MAX_PROBES_PER_POLL + 3)]
    probe = FakeTraitsProbe()
    entries = [entry(v, v, iso(-1 + i / 1000)) for i, v in enumerate(ids)]
    _, poller, _, result = run(db, entries, probe)
    assert result.queued == MAX_PROBES_PER_POLL
    with db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET last_checked = NULL")
    assert poller.run_once().queued == 3
    assert len(set(probe.asked)) == len(ids)


def test_probes_are_spread_out_by_a_pause(db):
    probe = FakeTraitsProbe()
    _, poller, _, _ = run(
        db, [entry(V1, "A", iso(-1)), entry(V2, "B", iso(-0.5)), entry(V3, "C", iso(-0.4))], probe
    )
    assert len(poller.sleeps) == 2, "none before the first probe"
    assert all(1.0 <= s <= 3.0 for s in poller.sleeps)


def test_check_now_filters_like_the_scheduled_poll(db):
    sub = add(db)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(entry(V1, "L", iso(-1)), entry(V2, "N", iso(-0.5)))
    probe = FakeTraitsProbe({V1: VideoTraits(live_status="is_live")})
    poller = make_poller(db, feeds, probe=probe)
    assert poller.poll_now(sub) == 1
    assert skipped_videos.skipped_ids(db, sub.id) == {V1}


def test_a_podcast_subscription_to_a_channel_is_filtered_too(db):
    sub = add(db, target="podcast", audio_only=True)
    feeds = FakeFeeds()
    feeds.answers[CID] = feed(entry(V1, "L", iso(-1)))
    probe = FakeTraitsProbe({V1: VideoTraits(live_status="is_live")})
    assert make_poller(db, feeds, probe=probe).poll(sub) == 0
    assert skipped_videos.skipped_ids(db, sub.id) == {V1}


def test_the_skip_is_logged_without_anything_private(db, caplog):
    probe = FakeTraitsProbe({V1: VideoTraits(live_status="is_live")})
    with caplog.at_level(logging.INFO, logger="ytp.subscription_poller"):
        run(db, [entry(V1, "L", iso(-1))], probe)
    record = next(r for r in caplog.records if r.getMessage() == "video skipped")
    assert (record.video_id, record.reason) == (V1, "live")


# --- what is stored ---------------------------------------------------------------------------


def test_recording_twice_keeps_one_row_and_a_bad_reason_is_refused(db):
    sub = add(db)
    skipped_videos.record(db, sub.id, V1, "short", "T", None)
    skipped_videos.record(db, sub.id, V1, "live", "T", None)
    assert skipped_videos.summary(db, sub.id)["count"] == 1
    assert skipped_videos.summary(db, sub.id)["recent"][0]["reason"] == "short"
    with pytest.raises(ValueError, match="reason"):
        skipped_videos.record(db, sub.id, V2, "boring", "T", None)


def test_the_summary_lists_the_five_newest(db):
    sub = add(db)
    for i in range(8):
        skipped_videos.record(db, sub.id, f"{chr(97 + i)}" * 11, "short", f"T{i}", None)
    info = skipped_videos.summary(db, sub.id)
    assert info["count"] == 8
    assert [r["title"] for r in info["recent"]] == ["T7", "T6", "T5", "T4", "T3"]


def test_unsubscribing_removes_the_skipped_rows(db):
    sub = add(db)
    skipped_videos.record(db, sub.id, V1, "short", "T", None)
    assert subscriptions.delete(db, sub.id)
    assert db.query("SELECT COUNT(*) AS n FROM skipped_videos")[0]["n"] == 0


def test_the_same_video_can_be_skipped_in_two_subscriptions(db):
    a = add(db)
    b = add(db, "UC" + "b" * 22, "Other")
    skipped_videos.record(db, a.id, V1, "short", "T", None)
    skipped_videos.record(db, b.id, V1, "short", "T", None)
    assert skipped_videos.skipped_ids(db, a.id) == skipped_videos.skipped_ids(db, b.id) == {V1}
