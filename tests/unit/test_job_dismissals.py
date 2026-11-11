"""Dismissing failed jobs: hidden for one account, never deleted, never somebody else's."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.helpers.accounts_app import client_for, db_of, running
from tests.helpers.accounts_app import people as make_people
from ytp import accounts, jobs

pytestmark = [pytest.mark.real_auth, pytest.mark.security]


@pytest.fixture
def app_client(tmp_path):
    with running(tmp_path) as client:
        client.app.state.youtube_worker = SimpleNamespace(
            wake=lambda: None, progress={}, details={}
        )
        yield client


@pytest.fixture
def people(app_client):
    return make_people(app_client)


def make_job(client, *owner_ids, external_id="aaaaaaaaaaa", failed=True):
    job, _ = jobs.submit_job(
        db_of(client), "youtube", external_id, f'{{"title": "Video {external_id}"}}'
    )
    for owner_id in owner_ids:
        accounts.add_job_owner(db_of(client), job.id, owner_id)
    if failed:
        jobs.mark_failed(db_of(client), job.id, "boom")
    return job


def failed_count(browser):
    return browser.get("/api/jobs").json()["counts"]["failed"]


def recent_ids(browser, query=""):
    return [j["id"] for j in browser.get(f"/api/dashboard{query}").json()["recent"]]


def test_dismissing_takes_a_failed_job_out_of_the_count_and_the_list(app_client, people):
    job = make_job(app_client, people.alice_id)
    assert failed_count(people.alice) == 1
    answer = people.alice.post(f"/api/jobs/{job.id}/dismiss")
    assert answer.status_code == 200
    assert answer.json() == {"id": job.id, "dismissed": True}
    counts = people.alice.get("/api/jobs").json()["counts"]
    assert counts["failed"] == 0
    assert counts["dismissed"] == 1
    assert recent_ids(people.alice) == []


def test_a_dismissed_job_is_not_deleted_and_can_be_shown_again(app_client, people):
    job = make_job(app_client, people.alice_id)
    people.alice.post(f"/api/jobs/{job.id}/dismiss")
    assert jobs.get_job(db_of(app_client), job.id).status == "failed"
    assert recent_ids(people.alice, "?dismissed=1") == [job.id]
    row = people.alice.get("/api/dashboard?dismissed=1").json()["recent"][0]
    assert row["dismissed"] is True
    assert row["can_retry"] is True
    listed = people.alice.get("/api/jobs").json()["jobs"]
    assert [j["dismissed"] for j in listed] == [True]
    assert people.alice.delete(f"/api/jobs/{job.id}/dismiss").json() == {
        "id": job.id,
        "dismissed": False,
    }
    assert failed_count(people.alice) == 1
    assert recent_ids(people.alice) == [job.id]


def test_the_dashboard_counts_the_dismissed_jobs_either_way(app_client, people):
    job = make_job(app_client, people.alice_id)
    make_job(app_client, people.alice_id, external_id="bbbbbbbbbbb")
    people.alice.post(f"/api/jobs/{job.id}/dismiss")
    assert people.alice.get("/api/dashboard").json()["dismissed"] == 1
    assert people.alice.get("/api/dashboard?dismissed=1").json()["dismissed"] == 1


def test_dismissing_twice_is_fine(app_client, people):
    job = make_job(app_client, people.alice_id)
    assert people.alice.post(f"/api/jobs/{job.id}/dismiss").status_code == 200
    assert people.alice.post(f"/api/jobs/{job.id}/dismiss").status_code == 200
    assert people.alice.get("/api/jobs").json()["counts"]["dismissed"] == 1
    assert people.alice.delete(f"/api/jobs/{job.id}/dismiss").status_code == 200
    assert people.alice.delete(f"/api/jobs/{job.id}/dismiss").status_code == 200


def test_only_a_failed_job_can_be_dismissed(app_client, people):
    running_job = make_job(app_client, people.alice_id, failed=False)
    answer = people.alice.post(f"/api/jobs/{running_job.id}/dismiss")
    assert answer.status_code == 409
    assert people.alice.get("/api/jobs").json()["counts"]["dismissed"] == 0


def test_a_user_cannot_dismiss_or_restore_somebody_elses_job(app_client, people):
    job = make_job(app_client, people.alice_id)
    foreign = people.bob.post(f"/api/jobs/{job.id}/dismiss")
    missing = people.bob.post("/api/jobs/987654/dismiss")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert people.bob.delete(f"/api/jobs/{job.id}/dismiss").status_code == 404
    assert failed_count(people.alice) == 1


def test_a_dismissal_is_per_account(app_client, people):
    job = make_job(app_client, people.alice_id, people.bob_id)
    people.alice.post(f"/api/jobs/{job.id}/dismiss")
    assert failed_count(people.alice) == 0
    assert failed_count(people.bob) == 1
    assert failed_count(people.admin) == 1
    assert recent_ids(people.bob) == [job.id]


def test_the_admin_may_dismiss_any_job_for_themselves_only(app_client, people):
    job = make_job(app_client, people.alice_id)
    assert people.admin.post(f"/api/jobs/{job.id}/dismiss").status_code == 200
    assert failed_count(people.admin) == 0
    assert recent_ids(people.admin) == []
    assert failed_count(people.alice) == 1  # the owner still sees their failure


def test_dismiss_all_failed_covers_only_what_the_account_may_see(app_client, people):
    mine = make_job(app_client, people.alice_id, external_id="aaaaaaaaaaa")
    also_mine = make_job(app_client, people.alice_id, external_id="bbbbbbbbbbb")
    theirs = make_job(app_client, people.bob_id, external_id="ccccccccccc")
    queued = make_job(app_client, people.alice_id, external_id="ddddddddddd", failed=False)
    answer = people.alice.post("/api/jobs/dismiss-failed")
    assert answer.json() == {"dismissed": 2}
    assert failed_count(people.alice) == 0
    assert failed_count(people.bob) == 1
    assert recent_ids(people.bob) == [theirs.id]
    assert set(recent_ids(people.alice, "?dismissed=1")) == {mine.id, also_mine.id}
    assert jobs.get_job(db_of(app_client), queued.id).status == "queued"
    assert people.alice.post("/api/jobs/dismiss-failed").json() == {"dismissed": 0}


def test_the_admin_can_dismiss_all_failed_jobs_of_everybody_for_themselves(app_client, people):
    make_job(app_client, people.alice_id, external_id="aaaaaaaaaaa")
    make_job(app_client, people.bob_id, external_id="bbbbbbbbbbb")
    assert people.admin.post("/api/jobs/dismiss-failed").json() == {"dismissed": 2}
    assert failed_count(people.admin) == 0
    assert failed_count(people.alice) == 1
    assert failed_count(people.bob) == 1


def test_a_retry_clears_the_dismissal_so_a_new_failure_shows_again(app_client, people):
    job = make_job(app_client, people.alice_id)
    people.alice.post(f"/api/jobs/{job.id}/dismiss")
    assert people.alice.post(f"/api/jobs/{job.id}/retry").status_code == 200
    assert people.alice.get("/api/jobs").json()["counts"]["dismissed"] == 0
    jobs.mark_failed(db_of(app_client), job.id, "boom again")
    assert failed_count(people.alice) == 1
    assert recent_ids(people.alice) == [job.id]


def test_a_job_that_is_dismissed_and_then_deleted_leaves_no_dismissal(app_client, people):
    job = make_job(app_client, people.alice_id)
    people.alice.post(f"/api/jobs/{job.id}/dismiss")
    with db_of(app_client).transaction() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
    assert db_of(app_client).query("SELECT COUNT(*) AS n FROM job_dismissals")[0]["n"] == 0


def test_the_dismiss_routes_need_a_login(app_client):
    anonymous = client_for(app_client, None)
    for method, path in (
        ("POST", "/api/jobs/1/dismiss"),
        ("DELETE", "/api/jobs/1/dismiss"),
        ("POST", "/api/jobs/dismiss-failed"),
    ):
        assert anonymous.request(method, path).status_code in (401, 403)
