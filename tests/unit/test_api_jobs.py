"""GET /api/jobs and POST /api/jobs/{id}/retry (issue #26): the job list and the retry action."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from ytp import jobs
from ytp.config import Settings
from ytp.jobs_api import TorrentProgress, job_label
from ytp.main import create_app
from ytp.youtube_download import DownloadFailed

HASH = "0123456789abcdef0123456789abcdef01234567"
HASH2 = "1123456789abcdef0123456789abcdef01234567"
TRACKER = "udp%3A%2F%2Ftracker.example%3A1337%2Fannounce%3Fpasskey%3Dsecret-passkey"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Big%20Buck%20Bunny&tr={TRACKER}"
MOVIE = {"type": "movies", "title": "Big Buck Bunny", "year": 2008}
VIDEO = "dQw4w9WgXcQ"
YT = {"type": "youtube", "video_id": VIDEO, "channel": "Blender", "title": "Big Buck Bunny"}
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


class AlwaysFails:
    """A downloader that never reaches the network: every video fails, quickly."""

    def download(self, ref, workdir, progress=None, *, audio_only=False, max_height=None):
        raise DownloadFailed("still not available")


@pytest.fixture(autouse=True)
def no_reconciler(monkeypatch):
    """The reconciler would mark the hand-made jobs below failed (their torrents are not there)."""
    monkeypatch.setattr("ytp.reconciler.Reconciler.run_once", lambda self: None)


@pytest.fixture
def fake() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    yield server
    server.stop()


def make_client(tmp_path: Path, fake: FakeQbittorrent | None) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    data = tmp_path / "data"
    for name in ENV.values():
        (data / name).mkdir(parents=True, exist_ok=True)
    (data / "downloads" / "incomplete").mkdir(parents=True, exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(data), "CONFIG_DIR": str(config)}
    if fake is not None:
        values |= {"QBIT_URL": fake.url, "QBIT_USER": USER, "QBIT_PASSWORD": PASSWORD}
    client = TestClient(create_app(Settings.from_env(values)))
    client.__enter__()
    worker = client.app.state.youtube_worker
    if worker is not None:
        worker.downloader = AlwaysFails()
    return client


@contextlib.contextmanager
def running(tmp_path: Path, fake: FakeQbittorrent | None) -> Iterator[TestClient]:
    test_client = make_client(tmp_path, fake)
    try:
        yield test_client
    finally:
        test_client.__exit__(None, None, None)


@pytest.fixture
def client(tmp_path, fake) -> Iterator[TestClient]:
    with running(tmp_path, fake) as test_client:
        yield test_client


def add_magnet_job(
    client, infohash=HASH, metadata=None, magnet=MAGNET, status="queued", error=None
):
    job, _ = jobs.submit_magnet_job(
        client.app.state.db, infohash, json.dumps(metadata or MOVIE), magnet
    )
    if status == "failed":
        jobs.mark_failed(client.app.state.db, job.id, error or "boom")
    elif status != "queued":
        jobs.transition(client.app.state.db, job.id, status, expected=("queued",))
    return job


def list_jobs(client, **query):
    return client.get("/api/jobs", params=query)


# --- labels ---


@pytest.mark.parametrize(
    ("meta", "label"),
    [
        ({"type": "movies", "title": "Big Buck Bunny", "year": 2008}, "Big Buck Bunny (2008)"),
        ({"type": "series", "title": "Tatort", "season": 3}, "Tatort S03"),
        ({"type": "series", "title": "Tatort", "season": 3, "episode": 7}, "Tatort S03E07"),
        (
            {"type": "series", "title": "Tatort", "seasons": [1, 2, 3]},
            f"Tatort S01{chr(0x2013)}S03",
        ),
        ({"type": "series", "title": "Tatort", "seasons": [1, 2, 5]}, "Tatort S01,S02,S05"),
        (
            {"type": "series", "title": "Tatort", "seasons": [1, 2, 3, 5]},
            "Tatort S01,S02,S03,S05",
        ),
        ({"type": "series", "title": "Tatort", "seasons": [3]}, "Tatort S03"),
        (
            {"type": "series", "title": "Tatort", "season": 3, "seasons": [3], "episode": 7},
            "Tatort S03E07",
        ),
        ({"type": "audiobooks", "author": "Michael Ende", "title": "Momo"}, "Michael Ende - Momo"),
        ({"type": "music", "artist": "Kraftwerk", "album": "Autobahn"}, "Kraftwerk - Autobahn"),
        (YT, "Blender - Big Buck Bunny"),
        ({}, "magnet abc"),
        ({"type": "movies", "title": 5}, "magnet abc"),
    ],
)
def test_the_label_is_made_from_the_metadata(meta, label):
    job = jobs.Job(1, "magnet", "abc", meta, "queued", None, "t", "t")
    assert job_label(job) == label


# --- the list ---


def test_an_empty_list_has_zero_counts(client):
    body = list_jobs(client).json()
    assert body == {
        "jobs": [],
        "counts": {
            "queued": 0,
            "downloading": 0,
            "organizing": 0,
            "done": 0,
            "failed": 0,
            "dismissed": 0,
        },
    }


def test_jobs_are_listed_newest_first_with_label_status_and_error(client):
    add_magnet_job(client)
    add_magnet_job(
        client,
        HASH2,
        {"type": "music", "artist": "Kraftwerk", "album": "Autobahn"},
        status="failed",
        error="no seeds",
    )
    body = list_jobs(client).json()
    assert [j["label"] for j in body["jobs"]] == ["Kraftwerk - Autobahn", "Big Buck Bunny (2008)"]
    failed, queued = body["jobs"]
    assert failed["status"] == "failed"
    assert failed["error"] == "no seeds"
    assert failed["can_retry"] is True
    assert failed["kind"] == "music"
    assert queued["can_retry"] is False
    assert queued["error"] is None
    assert body["counts"]["failed"] == 1
    assert body["counts"]["queued"] == 1


def test_the_list_marks_high_priority_jobs(client):
    high = add_magnet_job(client)
    normal = add_magnet_job(client, HASH2)
    with client.app.state.db.transaction() as conn:
        conn.execute("UPDATE jobs SET priority = 1 WHERE id = ?", (high.id,))
    rows = {job["id"]: job for job in list_jobs(client).json()["jobs"]}
    assert rows[high.id]["high_priority"] is True
    assert rows[normal.id]["high_priority"] is False


def test_the_list_never_contains_the_magnet_link_or_its_passkey(client):
    add_magnet_job(client)
    text = list_jobs(client).text
    assert "passkey" not in text
    assert "tracker.example" not in text
    assert "magnet:" not in text
    assert "source" not in list_jobs(client).json()["jobs"][0]


def test_the_status_filter_and_the_limit(client):
    add_magnet_job(client)
    add_magnet_job(client, HASH2, status="failed")
    only_failed = list_jobs(client, status="failed").json()
    assert [j["status"] for j in only_failed["jobs"]] == ["failed"]
    assert only_failed["counts"]["queued"] == 1, "the counts cover all jobs, not the filter"
    assert len(list_jobs(client, limit=1).json()["jobs"]) == 1
    assert len(list_jobs(client, limit=0).json()["jobs"]) == 1, "the limit is clamped to 1..200"


def test_an_unknown_status_filter_is_a_422(client):
    assert list_jobs(client, status="exploded").status_code == 422


def test_a_downloading_torrent_shows_qbittorrents_progress(client, fake):
    add_magnet_job(client, status="downloading")
    fake.torrents = [{"hash": HASH, "name": "x", "state": "downloading", "progress": 0.4237}]
    job = list_jobs(client).json()["jobs"][0]
    assert job["status"] == "downloading"
    assert job["progress"] == 42


def test_progress_is_null_when_qbittorrent_cannot_be_asked(client, fake):
    add_magnet_job(client, status="downloading")
    fake.list_status = 500
    response = list_jobs(client)
    assert response.status_code == 200
    assert response.json()["jobs"][0]["progress"] is None


def test_progress_by_status(client):
    queued = add_magnet_job(client)
    organizing = add_magnet_job(client, HASH2, status="organizing")
    by_id = {j["id"]: j for j in list_jobs(client).json()["jobs"]}
    assert by_id[queued.id]["progress"] is None
    assert by_id[organizing.id]["progress"] == 100


def test_a_running_youtube_download_shows_the_workers_progress(client):
    job, _ = jobs.submit_job(client.app.state.db, "youtube", VIDEO, json.dumps(YT))
    jobs.transition(client.app.state.db, job.id, "downloading", expected=("queued",))
    client.app.state.youtube_worker.progress[job.id] = 63
    assert list_jobs(client).json()["jobs"][0]["progress"] == 63


def test_a_finished_job_can_carry_a_note(client):
    """A `done` job whose organizer skipped files says so next to the status (#69)."""
    job = add_magnet_job(client, status="downloading")
    db = client.app.state.db
    assert jobs.transition(db, job.id, "organizing", expected=("downloading",))
    note = "3 of 10 files skipped: x.mkv: no episode"
    assert jobs.transition(db, job.id, "done", expected=("organizing",), note=note)
    listed = list_jobs(client).json()["jobs"][0]
    assert listed["note"] == "3 of 10 files skipped: x.mkv: no episode"
    assert listed["error"] is None


def test_the_torrent_progress_is_cached_for_a_moment(fake):
    fake.torrents = [{"hash": HASH, "name": "x", "state": "downloading", "progress": 0.5}]
    from ytp.clients.qbittorrent import QbitClient

    cache = TorrentProgress(QbitClient(fake.url, USER, PASSWORD), ttl=60)
    assert cache.get() == {HASH: 50}
    before = len(fake.calls("torrents/info"))
    cache.get()
    cache.get()
    assert len(fake.calls("torrents/info")) == before


def test_without_a_database_the_list_is_a_503(tmp_path):
    app = create_app(Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path)}))
    assert TestClient(app).get("/api/jobs").status_code == 503


# --- the retry ---


def test_retrying_a_failed_magnet_queues_it_again_and_hands_it_to_qbittorrent(client, fake):
    job = add_magnet_job(client, status="failed", error="qBittorrent was down")
    response = client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["error"] is None
    stored = jobs.get_job(client.app.state.db, job.id)
    assert stored.status == "queued"
    assert stored.error is None
    adds = fake.calls("torrents/add")
    assert len(adds) == 1
    assert adds[0].form["urls"][0] == MAGNET, "the original link, trackers included"
    assert adds[0].form["category"] == ["movies"]


def test_retry_of_an_old_job_without_a_stored_link_uses_the_bare_infohash(client, fake):
    job = add_magnet_job(client, magnet=None, status="failed")
    assert client.post(f"/api/jobs/{job.id}/retry").status_code == 200
    assert fake.calls("torrents/add")[0].form["urls"][0] == f"magnet:?xt=urn:btih:{HASH}"


def test_retrying_a_magnet_whose_torrent_is_still_there_skips_the_add(client, fake):
    """The reconciler fails a job but leaves its torrent in qBittorrent (#69).

    Adding the magnet again would be refused ("Fails.") and the retry could never work;
    the retry must not add it a second time.
    """
    job = add_magnet_job(client, status="failed", error="organizing failed: boom")
    fake.torrents = [{"hash": HASH, "name": "x", "state": "stalledUP", "progress": 1.0}]
    response = client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert fake.calls("torrents/add") == [], "qBittorrent already has it"
    assert fake.calls("torrents/recheck") == [], "nothing is wrong with it"
    assert fake.calls("torrents/start") == [], "it is not paused"
    stored = jobs.get_job(client.app.state.db, job.id)
    assert (stored.status, stored.error) == ("queued", None)


def test_retrying_a_magnet_whose_torrent_is_in_an_error_state_rechecks_it(client, fake):
    job = add_magnet_job(client, status="failed")
    fake.torrents = [{"hash": HASH, "name": "x", "state": "error", "progress": 1.0}]
    assert client.post(f"/api/jobs/{job.id}/retry").status_code == 200
    assert fake.calls("torrents/add") == []
    assert len(fake.calls("torrents/recheck")) == 1
    assert fake.calls("torrents/recheck")[0].form["hashes"] == [HASH]


def test_retrying_a_magnet_whose_torrent_is_paused_resumes_it(client, fake):
    job = add_magnet_job(client, status="failed")
    fake.torrents = [{"hash": HASH, "name": "x", "state": "pausedUP", "progress": 1.0}]
    assert client.post(f"/api/jobs/{job.id}/retry").status_code == 200
    assert fake.calls("torrents/add") == []
    assert len(fake.calls("torrents/start")) == 1
    assert fake.calls("torrents/start")[0].form["hashes"] == [HASH]


def test_a_retry_that_fails_again_marks_the_job_failed_with_the_new_reason(client, fake):
    job = add_magnet_job(client, status="failed")
    fake.add_result = "Fails."
    response = client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 502
    assert response.json()["job_id"] == job.id
    stored = jobs.get_job(client.app.state.db, job.id)
    assert stored.status == "failed"
    assert "Fails." in stored.error


def test_retrying_a_job_clears_an_old_note(client, fake):
    job = add_magnet_job(client, status="failed")
    with client.app.state.db.transaction() as conn:
        conn.execute("UPDATE jobs SET note = 'an old note' WHERE id = ?", (job.id,))
    assert client.post(f"/api/jobs/{job.id}/retry").status_code == 200
    assert jobs.get_job(client.app.state.db, job.id).note is None


def test_retry_with_qbittorrent_down_is_a_503_and_the_job_stays_failed(tmp_path, fake):
    with running(tmp_path, fake) as client:
        job = add_magnet_job(client, status="failed")
        fake.fail_status = 500
        fake.stop()
        assert client.post(f"/api/jobs/{job.id}/retry").status_code == 503
        assert jobs.get_job(client.app.state.db, job.id).status == "failed"


@pytest.mark.parametrize("status", ["queued", "downloading", "organizing", "done"])
def test_only_a_failed_job_can_be_retried(client, fake, status):
    job = add_magnet_job(client, status=status)
    response = client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 409
    assert jobs.get_job(client.app.state.db, job.id).status == status
    assert fake.calls("torrents/add") == []


def test_retry_of_an_unknown_job_is_a_404(client):
    assert client.post("/api/jobs/999/retry").status_code == 404


def test_retry_without_qbittorrent_configured_is_a_503(tmp_path):
    with running(tmp_path, None) as client:
        job = add_magnet_job(client, status="failed")
        assert client.post(f"/api/jobs/{job.id}/retry").status_code == 503
        assert jobs.get_job(client.app.state.db, job.id).status == "failed"


def test_retrying_a_failed_video_runs_the_download_worker_again(client):
    db = client.app.state.db
    job, _ = jobs.submit_job(db, "youtube", VIDEO, json.dumps(YT))
    jobs.mark_failed(db, job.id, "temporary problem")
    response = client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 200
    assert response.json()["label"] == "Blender - Big Buck Bunny"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        stored = jobs.get_job(db, job.id)
        if stored.error == "still not available":
            break
        time.sleep(0.02)
    assert stored.status == "failed"
    assert stored.error == "still not available", "the worker ran it again and gave a new reason"


def test_the_retry_route_refuses_cross_site_requests_and_leaves_the_job_alone(client, fake):
    job = add_magnet_job(client, status="failed")
    for headers in (
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
    ):
        response = client.post(f"/api/jobs/{job.id}/retry", headers=headers)
        assert response.status_code == 403
    assert jobs.get_job(client.app.state.db, job.id).status == "failed"
    assert fake.calls("torrents/add") == []


def test_a_get_on_the_retry_route_is_not_allowed(client):
    assert client.get("/api/jobs/1/retry").status_code == 405


def test_migrated_old_rows_have_no_source_and_the_magnet_endpoint_stores_it(client):
    response = client.post("/api/magnet", json={"magnet": MAGNET, "metadata": MOVIE})
    assert response.status_code == 201
    assert jobs.get_source(client.app.state.db, response.json()["id"]) == MAGNET
