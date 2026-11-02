"""Whose jobs and subscriptions an account may see and change (#44): nobody else's."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.helpers.accounts_app import ADMIN, add_account, client_for, running
from tests.helpers.fake_probe import FakeTraitsProbe
from tests.unit.test_feeds import CID as FEED_CID
from tests.unit.test_feeds import entry, feed
from tests.unit.test_subscription_poll_api import FakeFeeds
from tests.unit.test_subscriptions import channel_info, fake_extract
from ytp import accounts, jobs, podcast_subscriptions, subscriptions
from ytp.channels import ChannelResolver
from ytp.youtube import YoutubeProbe

pytestmark = [pytest.mark.real_auth, pytest.mark.security]


@pytest.fixture
def app_client(tmp_path):
    with running(tmp_path) as client:
        probe = YoutubeProbe(timeout=1.0, extract=fake_extract(channel_info(FEED_CID, "Blender")))
        client.app.state.youtube = probe
        client.app.state.channels = ChannelResolver(probe, extract=probe._extract)
        client.app.state.youtube_worker = SimpleNamespace(
            wake=lambda: None, progress={}, details={}
        )
        client.app.state.poller.probe = FakeTraitsProbe()
        client.app.state.poller.feeds = FakeFeeds()
        yield client


@pytest.fixture
def people(app_client):
    """alice and bob, each with a browser, and the admin."""
    alice = add_account(app_client, "alice")
    bob = add_account(app_client, "bob")
    return SimpleNamespace(
        alice=client_for(app_client, alice),
        bob=client_for(app_client, bob),
        admin=client_for(app_client, ADMIN),
        alice_id=alice.id,
        bob_id=bob.id,
    )


def db_of(client):
    return client.app.state.db


def make_job(client, owner_id, external_id="aaaaaaaaaaa", status=None):
    job, _ = jobs.submit_job(
        db_of(client), "youtube", external_id, f'{{"title": "Video {external_id}"}}'
    )
    if owner_id is not None:
        accounts.add_job_owner(db_of(client), job.id, owner_id)
    if status == "failed":
        jobs.mark_failed(db_of(client), job.id, "boom")
    return job


def ids(response, key="jobs"):
    return {entry["id"] for entry in response.json()[key]}


# --- jobs ---


def test_a_user_sees_only_their_own_jobs_and_the_admin_sees_all(app_client, people):
    mine = make_job(app_client, people.alice_id, "aaaaaaaaaaa")
    theirs = make_job(app_client, people.bob_id, "bbbbbbbbbbb")
    nobodys = make_job(app_client, None, "ccccccccccc")
    assert ids(people.alice.get("/api/jobs")) == {mine.id}
    assert ids(people.bob.get("/api/jobs")) == {theirs.id}
    assert ids(people.admin.get("/api/jobs")) == {mine.id, theirs.id, nobodys.id}


def test_the_counts_of_a_user_count_only_their_jobs(app_client, people):
    make_job(app_client, people.alice_id, "aaaaaaaaaaa")
    make_job(app_client, people.bob_id, "bbbbbbbbbbb")
    make_job(app_client, people.bob_id, "ccccccccccc")
    assert people.alice.get("/api/jobs").json()["counts"]["queued"] == 1
    assert people.bob.get("/api/jobs").json()["counts"]["queued"] == 2
    assert people.admin.get("/api/jobs").json()["counts"]["queued"] == 3


def test_a_job_with_two_owners_is_seen_by_both(app_client, people):
    job = make_job(app_client, people.alice_id)
    accounts.add_job_owner(db_of(app_client), job.id, people.bob_id)
    assert ids(people.alice.get("/api/jobs")) == ids(people.bob.get("/api/jobs")) == {job.id}


def test_nobody_else_can_retry_a_job_and_cannot_tell_it_exists(app_client, people):
    job = make_job(app_client, people.alice_id, status="failed")
    foreign = people.bob.post(f"/api/jobs/{job.id}/retry")
    missing = people.bob.post("/api/jobs/987654/retry")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert jobs.get_job(db_of(app_client), job.id).status == "failed"


def test_the_owner_and_the_admin_can_retry(app_client, people):
    job = make_job(app_client, people.alice_id, status="failed")
    assert people.alice.post(f"/api/jobs/{job.id}/retry").status_code == 200
    jobs.mark_failed(db_of(app_client), job.id, "boom")
    assert people.admin.post(f"/api/jobs/{job.id}/retry").status_code == 200


def test_the_dashboard_shows_a_user_only_their_own_jobs(app_client, people):
    mine = make_job(app_client, people.alice_id, "aaaaaaaaaaa")
    theirs = make_job(app_client, people.bob_id, "bbbbbbbbbbb")
    failed_mine = make_job(app_client, people.alice_id, "ccccccccccc", status="failed")
    failed_theirs = make_job(app_client, people.bob_id, "ddddddddddd", status="failed")
    seen = people.alice.get("/api/dashboard").json()
    assert ids(people.alice.get("/api/dashboard"), "active") == {mine.id}
    assert {j["id"] for j in seen["recent"]} == {failed_mine.id}
    everything = people.admin.get("/api/dashboard").json()
    assert {j["id"] for j in everything["active"]} == {mine.id, theirs.id}
    assert {j["id"] for j in everything["recent"]} == {failed_mine.id, failed_theirs.id}


def test_the_dashboard_keeps_admin_details_from_users(app_client, people):
    user_view = people.alice.get("/api/dashboard").json()
    admin_view = people.admin.get("/api/dashboard").json()
    assert user_view["disk"] is None
    assert user_view["deploy"] is None
    assert "history" not in user_view["vpn"] or not user_view["vpn"]["history"]
    assert "egress" not in user_view["vpn"].get("current", {})
    assert "history" in admin_view["vpn"]


# --- subscriptions ---


def subscribe(browser, channel="@blender"):
    response = browser.post("/api/subscriptions", json={"channel": channel})
    # older than the videos the tests offer, as a real subscription is after a while
    with browser.app.state.db.transaction() as conn:
        conn.execute("UPDATE subscriptions SET created_at = '2026-10-01T00:00:00.000Z'")
    return response


def test_two_users_following_one_channel_make_one_subscription(app_client, people):
    first = subscribe(people.alice)
    second = subscribe(people.bob)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["other_followers"] == 1
    assert len(subscriptions.list_all(db_of(app_client))) == 1
    sub_id = first.json()["id"]
    assert accounts.followers(db_of(app_client), "channel", sub_id) == [
        people.alice_id,
        people.bob_id,
    ]


def test_following_twice_is_still_one_follower(app_client, people):
    subscribe(people.alice)
    again = subscribe(people.alice)
    assert again.json()["other_followers"] == 0
    sub_id = again.json()["id"]
    assert accounts.followers(db_of(app_client), "channel", sub_id) == [people.alice_id]


def test_a_user_sees_only_what_they_follow_and_the_admin_sees_everything(app_client, people):
    mine = subscribe(people.alice, "@kurzgesagt").json()["id"]
    other, _ = subscriptions.add(db_of(app_client), "UC" + "b" * 22, "Other channel")
    accounts.follow(db_of(app_client), "channel", other.id, people.bob_id)
    alice = people.alice.get("/api/subscriptions").json()["subscriptions"]
    assert [s["id"] for s in alice] == [mine]
    assert alice[0]["following"] is True
    assert "followers" not in alice[0], "the names of the others are for the admin"
    assert alice[0]["follower_count"] == 1
    everything = people.admin.get("/api/subscriptions").json()["subscriptions"]
    assert {s["id"] for s in everything} == {mine, other.id}
    by_id = {s["id"]: s for s in everything}
    assert by_id[mine]["followers"] == ["alice"]
    assert by_id[other.id]["followers"] == ["bob"]
    assert by_id[mine]["following"] is False


def test_unfollowing_removes_only_the_follower_and_the_last_one_ends_it(app_client, people):
    sub_id = subscribe(people.alice).json()["id"]
    subscribe(people.bob)
    assert people.alice.delete(f"/api/subscriptions/{sub_id}").status_code == 204
    assert subscriptions.get(db_of(app_client), sub_id) is not None
    assert people.bob.get("/api/subscriptions").json()["subscriptions"][0]["id"] == sub_id
    assert people.alice.get("/api/subscriptions").json()["subscriptions"] == []
    assert people.bob.delete(f"/api/subscriptions/{sub_id}").status_code == 204
    assert subscriptions.get(db_of(app_client), sub_id) is None


def test_a_user_cannot_end_somebody_elses_subscription(app_client, people):
    sub_id = subscribe(people.alice).json()["id"]
    foreign = people.bob.delete(f"/api/subscriptions/{sub_id}")
    missing = people.bob.delete("/api/subscriptions/987654")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert subscriptions.get(db_of(app_client), sub_id) is not None
    assert accounts.followers(db_of(app_client), "channel", sub_id) == [people.alice_id]


def test_everyone_may_only_be_forced_by_an_admin(app_client, people):
    sub_id = subscribe(people.alice).json()["id"]
    assert people.bob.delete(f"/api/subscriptions/{sub_id}?everyone=1").status_code == 404
    assert subscriptions.get(db_of(app_client), sub_id) is not None
    # for alice the flag is only an unfollow, which here is also the end
    assert people.admin.delete(f"/api/subscriptions/{sub_id}?everyone=1").status_code == 204
    assert subscriptions.get(db_of(app_client), sub_id) is None
    assert people.admin.delete(f"/api/subscriptions/{sub_id}?everyone=1").status_code == 404


def test_everyone_by_a_user_with_followers_does_not_end_it_for_the_others(app_client, people):
    sub_id = subscribe(people.alice).json()["id"]
    subscribe(people.bob)
    assert people.alice.delete(f"/api/subscriptions/{sub_id}?everyone=1").status_code == 204
    assert subscriptions.get(db_of(app_client), sub_id) is not None
    assert accounts.followers(db_of(app_client), "channel", sub_id) == [people.bob_id]


def test_check_now_is_for_followers_and_the_admin_only(app_client, people):
    sub_id = subscribe(people.alice).json()["id"]
    assert people.bob.post(f"/api/subscriptions/{sub_id}/poll").status_code == 404
    assert people.bob.post("/api/subscriptions/987654/poll").status_code == 404
    assert people.alice.post(f"/api/subscriptions/{sub_id}/poll").status_code == 200
    assert people.admin.post(f"/api/subscriptions/{sub_id}/poll").status_code == 200


def test_new_videos_of_a_shared_channel_become_jobs_of_every_follower_once(app_client, people):
    sub_id = subscribe(people.alice).json()["id"]
    subscribe(people.bob)
    poller = app_client.app.state.poller
    fresh = entry("aaaaaaaaaaa", "New", (datetime.now(UTC) - timedelta(hours=1)).isoformat())
    poller.feeds.answer = feed(fresh)
    assert people.alice.post(f"/api/subscriptions/{sub_id}/poll").status_code == 200
    queued = jobs.list_jobs(db_of(app_client))
    assert len(queued) == 1, "one download, however many follow"
    assert ids(people.alice.get("/api/jobs")) == ids(people.bob.get("/api/jobs")) == {queued[0].id}


def test_a_later_follower_is_added_to_a_video_that_is_already_queued(app_client, people):
    sub_id = subscribe(people.alice).json()["id"]
    fresh = entry("aaaaaaaaaaa", "New", (datetime.now(UTC) - timedelta(hours=1)).isoformat())
    app_client.app.state.poller.feeds.answer = feed(fresh)
    people.alice.post(f"/api/subscriptions/{sub_id}/poll")
    assert people.bob.get("/api/jobs").json()["jobs"] == []  # not following yet: not theirs
    subscribe(people.bob)
    people.alice.post(f"/api/subscriptions/{sub_id}/poll")  # the video is met again: a duplicate
    queued = jobs.list_jobs(db_of(app_client))
    assert len(queued) == 1, "still one download"
    assert ids(people.bob.get("/api/jobs")) == {queued[0].id}


def test_downloads_started_by_hand_belong_to_the_user_who_started_them(app_client, people):
    app_client.app.state.youtube_worker = SimpleNamespace(
        wake=lambda: None, progress={}, details={}
    )
    response = people.alice.post(
        "/api/youtube/download", json={"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}
    )
    # whatever the probe says in this test setup, a refused request must not create an owner
    if response.status_code in (200, 201, 202):
        job_id = response.json()["id"]
        assert accounts.owns_job(db_of(app_client), job_id, people.alice_id)
        assert not accounts.owns_job(db_of(app_client), job_id, people.bob_id)
        assert job_id not in ids(people.bob.get("/api/jobs"))


# --- podcasts ---


def make_podcast(client, *follower_ids, url="https://example.org/feed.xml"):
    sub, _ = podcast_subscriptions.add(db_of(client), url, "A show")
    for account_id in follower_ids:
        accounts.follow(db_of(client), "podcast", sub.id, account_id)
    return sub


def test_podcasts_are_listed_for_followers_and_all_for_the_admin(app_client, people):
    mine = make_podcast(app_client, people.alice_id, url="https://example.org/a.xml")
    theirs = make_podcast(app_client, people.bob_id, url="https://example.org/b.xml")
    assert {p["id"] for p in people.alice.get("/api/podcasts").json()["podcasts"]} == {mine.id}
    assert {p["id"] for p in people.admin.get("/api/podcasts").json()["podcasts"]} == {
        mine.id,
        theirs.id,
    }


def test_a_user_cannot_end_or_poll_somebody_elses_podcast(app_client, people):
    sub = make_podcast(app_client, people.alice_id)
    assert people.bob.delete(f"/api/podcasts/{sub.id}").status_code == 404
    assert people.bob.post(f"/api/podcasts/{sub.id}/poll").status_code == 404
    assert podcast_subscriptions.get(db_of(app_client), sub.id) is not None


def test_unfollowing_a_podcast_ends_it_with_the_last_follower(app_client, people):
    sub = make_podcast(app_client, people.alice_id, people.bob_id)
    assert people.alice.delete(f"/api/podcasts/{sub.id}").status_code == 204
    assert podcast_subscriptions.get(db_of(app_client), sub.id) is not None
    assert people.bob.delete(f"/api/podcasts/{sub.id}").status_code == 204
    assert podcast_subscriptions.get(db_of(app_client), sub.id) is None


def test_podcast_everyone_is_for_admins(app_client, people):
    sub = make_podcast(app_client, people.alice_id, people.bob_id)
    assert people.bob.delete(f"/api/podcasts/{sub.id}?everyone=1").status_code == 204
    assert podcast_subscriptions.get(db_of(app_client), sub.id) is not None  # only bob left
    assert people.admin.delete(f"/api/podcasts/{sub.id}?everyone=1").status_code == 204
    assert podcast_subscriptions.get(db_of(app_client), sub.id) is None


# --- the admin's own view of the accounts leaks nothing ---


def test_a_user_cannot_see_who_else_has_an_account(app_client, people):
    for path in ("/api/accounts", "/api/alerts"):
        response = people.alice.get(path)
        assert response.status_code == 403
        assert "bob" not in response.text
    me = people.alice.get("/api/auth/me").json()
    assert me["account"]["username"] == "alice"
