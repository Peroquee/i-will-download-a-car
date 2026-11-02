"""Whose back catalogue it is (#44 x #45): the asker sees the run and its episodes."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.helpers.accounts_app import ADMIN, add_account, client_for, running
from tests.unit.test_podcast_backlog import FEED, GB, episodes, finish_all
from tests.unit.test_podcast_jobs import FakeFetcher, feed_of
from ytp import accounts, jobs, podcast_backlog
from ytp.podcast_jobs import PodcastWorker
from ytp.podcasts import PodcastFeedClient

pytestmark = [pytest.mark.real_auth, pytest.mark.security]


@pytest.fixture
def app_client(tmp_path, monkeypatch) -> Iterator:
    monkeypatch.setattr(PodcastWorker, "run", lambda self, stop: None)
    (tmp_path / "Podcasts").mkdir()
    with running(tmp_path, PODCAST_POLL_MINUTES="0") as client:
        fetcher = FakeFetcher()
        fetcher.feeds[FEED] = feed_of(*episodes(12))
        client.app.state.podcast_feeds = PodcastFeedClient(fetcher)
        feeder = client.app.state.backlog_feeder
        feeder._disk_usage = lambda path: type("U", (), {"free": 500 * GB, "total": 1000 * GB})()
        yield client


@pytest.fixture
def people(app_client):
    alice = add_account(app_client, "alice")
    bob = add_account(app_client, "bob")
    return alice, client_for(app_client, alice), bob, client_for(app_client, bob)


def test_the_episodes_belong_to_the_user_who_asked(app_client, people):
    alice, alice_page, _, bob_page = people
    assert alice_page.post("/api/podcasts/backlog", json={"feed": FEED}).status_code == 201
    mine = {j["id"] for j in alice_page.get("/api/jobs").json()["jobs"]}
    assert len(mine) == 2
    assert bob_page.get("/api/jobs").json()["jobs"] == []
    assert all(accounts.owns_job(app_client.app.state.db, i, alice.id) for i in mine)


def test_other_users_do_not_see_the_run_in_the_list_or_the_dashboard(app_client, people):
    _, alice_page, _, bob_page = people
    alice_page.post("/api/podcasts/backlog", json={"feed": FEED})
    assert len(alice_page.get("/api/podcasts/backlog").json()["backlogs"]) == 1
    assert bob_page.get("/api/podcasts/backlog").json()["backlogs"] == []
    assert bob_page.get("/api/dashboard").json()["backlogs"] == []
    assert len(alice_page.get("/api/dashboard").json()["backlogs"]) == 1


def test_the_admin_sees_every_run_and_may_stop_it(app_client, people):
    _, alice_page, _, _ = people
    run = alice_page.post("/api/podcasts/backlog", json={"feed": FEED}).json()["backlog"]
    admin = client_for(app_client, ADMIN)
    assert [r["id"] for r in admin.get("/api/podcasts/backlog").json()["backlogs"]] == [run["id"]]
    assert admin.delete(f"/api/podcasts/backlog/{run['id']}").status_code == 204


def test_nobody_else_can_stop_a_run_and_cannot_tell_it_exists(app_client, people):
    _, alice_page, _, bob_page = people
    run = alice_page.post("/api/podcasts/backlog", json={"feed": FEED}).json()["backlog"]
    foreign = bob_page.delete(f"/api/podcasts/backlog/{run['id']}")
    missing = bob_page.delete("/api/podcasts/backlog/987654")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert podcast_backlog.get(app_client.app.state.db, run["id"]).state == "running"


def test_a_second_user_asking_for_the_same_feed_joins_and_gets_the_later_episodes(
    app_client, people
):
    alice, alice_page, bob, bob_page = people
    first = alice_page.post("/api/podcasts/backlog", json={"feed": FEED}).json()
    joined = bob_page.post("/api/podcasts/backlog", json={"feed": FEED})
    assert joined.status_code == 200
    assert joined.json()["joined"] is True
    assert joined.json()["backlog"]["id"] == first["backlog"]["id"]
    db = app_client.app.state.db
    before = {j.id for j in jobs.list_jobs(db)}
    finish_all(db)
    app_client.app.state.backlog_feeder.tick()
    later = [j for j in jobs.list_jobs(db) if j.id not in before]
    assert later
    assert all(accounts.owns_job(db, j.id, alice.id) for j in later)
    assert all(accounts.owns_job(db, j.id, bob.id) for j in later)
    assert len(bob_page.get("/api/podcasts/backlog").json()["backlogs"]) == 1


def test_there_is_only_one_run_for_a_feed_however_many_ask(app_client, people):
    _, alice_page, _, bob_page = people
    alice_page.post("/api/podcasts/backlog", json={"feed": FEED})
    bob_page.post("/api/podcasts/backlog", json={"feed": FEED})
    assert len(podcast_backlog.visible(app_client.app.state.db)) == 1
    assert len(jobs.list_jobs(app_client.app.state.db)) == 2, "nothing is downloaded twice"


def test_a_visitor_can_do_nothing_here(app_client):
    visitor = client_for(app_client, None)
    assert visitor.post("/api/podcasts/backlog", json={"feed": FEED}).status_code == 401
    assert visitor.get("/api/podcasts/backlog").status_code == 401
    assert visitor.delete("/api/podcasts/backlog/1").status_code == 401
