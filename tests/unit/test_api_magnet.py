"""POST /api/magnet end to end: real app, real SQLite file, fake qBittorrent HTTP server."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from ytp.config import Settings
from ytp.main import create_app

HASH = "0123456789abcdef0123456789abcdef01234567"
TRACKER = "udp%3A%2F%2Ftracker.example%3A1337%2Fannounce%3Fpasskey%3Dsecret-passkey"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Big%20Buck%20Bunny&tr={TRACKER}"
MOVIE = {"type": "movies", "title": "Big Buck Bunny", "year": 2008}
SERIES = {"type": "series", "title": "Tatort", "season": 3}
AUDIOBOOK = {"type": "audiobooks", "author": "Michael Ende", "title": "Momo"}
MUSIC = {"type": "music", "artist": "Kraftwerk", "album": "Autobahn"}
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


@pytest.fixture
def fake() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    yield server
    server.stop()


def make_client(tmp_path: Path, fake: FakeQbittorrent | None, **env: str) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config)}
    if fake is not None:
        values |= {"QBIT_URL": fake.url, "QBIT_USER": USER, "QBIT_PASSWORD": PASSWORD}
    return TestClient(create_app(Settings.from_env({**values, **env})))


@pytest.fixture
def client(tmp_path, fake) -> Iterator[TestClient]:
    with make_client(tmp_path, fake) as test_client:
        yield test_client


def post(client: TestClient, magnet: str = MAGNET, metadata: dict | None = None, **extra):
    body = {"magnet": magnet, "metadata": metadata or MOVIE, **extra}
    return client.post("/api/magnet", json=body)


def rows(client: TestClient) -> list[dict]:
    return [dict(r) for r in client.app.state.db.query("SELECT * FROM jobs ORDER BY id")]


# --- the happy path ----------------------


def test_a_new_magnet_creates_a_queued_job_and_goes_to_qbittorrent(client, fake):
    response = post(client)
    assert response.status_code == 201
    assert response.json() == {
        "id": 1,
        "infohash": HASH,
        "type": "movies",
        "status": "queued",
        "duplicate": False,
    }
    (row,) = rows(client)
    assert (row["type"], row["external_id"], row["status"], row["error"]) == (
        "magnet",
        HASH,
        "queued",
        None,
    )
    assert json.loads(row["metadata"]) == MOVIE
    (sent,) = fake.calls("torrents/add")
    assert sent.form == {"urls": [MAGNET], "category": ["movies"], "autoTMM": ["true"]}


@pytest.mark.parametrize(
    ("metadata", "category"),
    [(MOVIE, "movies"), (SERIES, "series"), (AUDIOBOOK, "audiobooks"), (MUSIC, "music")],
)
def test_the_media_type_picks_the_qbittorrent_category(client, fake, metadata, category):
    assert post(client, metadata=metadata).status_code == 201
    assert fake.calls("torrents/add")[0].form["category"] == [category]
    assert json.loads(rows(client)[0]["metadata"])["type"] == category


def test_a_pasted_trailing_newline_and_spaces_are_stripped(client, fake):
    assert post(client, f"  {MAGNET}\r\n").status_code == 201
    assert fake.calls("torrents/add")[0].form["urls"] == [MAGNET]


def test_the_stored_metadata_is_the_normalised_json(client):
    post(client, metadata={**SERIES, "title": "  Tatort  "})
    assert json.loads(rows(client)[0]["metadata"]) == {
        "type": "series",
        "title": "Tatort",
        "season": 3,
    }


# --- validation: field-level 422, nothing stored or sent ----------------------------------------


@pytest.mark.parametrize(
    "magnet",
    [
        "",
        "http://example.com/a.torrent",
        "magnet:?dn=no-hash",
        f"{MAGNET}\nhttp://169.254.169.254/",
        "magnet:?xt=urn:btih:xyz",
    ],
)
def test_a_bad_magnet_is_a_422_on_the_magnet_field(client, fake, magnet):
    response = post(client, magnet)
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "magnet"]
    assert error["type"] == "magnet_invalid"
    assert rows(client) == []
    assert fake.calls("torrents/add") == []


def test_bad_metadata_is_a_422_naming_the_field(client, fake):
    response = post(client, metadata={**MOVIE, "year": 1066, "title": ""})
    assert response.status_code == 422
    found = {(tuple(e["loc"]), e["type"]) for e in response.json()["detail"]}
    assert found == {
        (("body", "metadata", "movies", "year"), "year_out_of_range"),
        (("body", "metadata", "movies", "title"), "name_empty"),
    }
    assert rows(client) == []
    assert fake.calls("torrents/add") == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"magnet": MAGNET},
        {"metadata": MOVIE},
        {"magnet": MAGNET, "metadata": {"type": "youtube", "title": "x"}},
        {"magnet": MAGNET, "metadata": MOVIE, "category": "music"},  # not accepted: type decides
        {"magnet": 5, "metadata": MOVIE},
        [],
    ],
)
def test_malformed_bodies_are_a_422(client, fake, body):
    assert client.post("/api/magnet", json=body).status_code == 422
    assert rows(client) == []
    assert fake.calls("torrents/add") == []


def test_a_body_that_is_not_json_is_a_422(client):
    response = client.post(
        "/api/magnet", content=b"magnet please", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_only_post_is_allowed(client):
    assert client.get("/api/magnet").status_code == 405


# --- dedupe ----------------------


def test_the_same_magnet_twice_returns_the_existing_job_and_does_not_add_again(client, fake):
    first = post(client).json()
    second = post(client)
    assert second.status_code == 200
    assert second.json() == {**first, "duplicate": True}
    assert len(rows(client)) == 1
    assert len(fake.calls("torrents/add")) == 1


def test_the_same_torrent_written_differently_is_the_same_job(client, fake):
    base32 = base64.b32encode(bytes.fromhex(HASH)).decode()
    post(client)
    for magnet in (f"magnet:?xt=urn:btih:{HASH.upper()}", f"magnet:?xt=urn:btih:{base32}&dn=x"):
        response = post(client, magnet)
        assert response.status_code == 200
        assert response.json()["duplicate"] is True
    assert len(rows(client)) == 1
    assert len(fake.calls("torrents/add")) == 1


def test_a_different_torrent_is_a_new_job(client):
    post(client)
    other = post(client, f"magnet:?xt=urn:btih:{'f' * 40}")
    assert other.status_code == 201
    assert other.json()["id"] == 2


def test_a_duplicate_does_not_overwrite_the_metadata(client):
    post(client)
    post(client, metadata={**MOVIE, "title": "Something Else"})
    assert json.loads(rows(client)[0]["metadata"])["title"] == "Big Buck Bunny"


def test_a_job_in_progress_is_a_duplicate_whatever_its_status(client, fake):
    post(client)
    for status in ("downloading", "organizing", "done"):
        with client.app.state.db.transaction() as conn:
            conn.execute("UPDATE jobs SET status = ?", (status,))
        response = post(client)
        assert (response.status_code, response.json()["status"]) == (200, status)
    assert len(fake.calls("torrents/add")) == 1


# --- qBittorrent trouble: a visible failed job, retried by sending the magnet again --


def test_qbittorrent_down_marks_the_job_failed_and_answers_503(tmp_path, fake):
    with make_client(tmp_path, fake) as client:
        fake.stop()
        response = post(client)
        assert response.status_code == 503
        body = response.json()
        assert body["job_id"] == 1
        assert "cannot reach qBittorrent" in body["detail"]
        (row,) = rows(client)
        assert row["status"] == "failed"
        assert "cannot reach qBittorrent" in row["error"]


def test_sending_a_failed_magnet_again_retries_it(tmp_path, fake):
    with make_client(tmp_path, fake) as client:
        fake.add_result = "Fails."
        assert post(client).status_code == 502
        assert rows(client)[0]["status"] == "failed"

        fake.add_result = "Ok."
        again = post(client, metadata={**MOVIE, "title": "Corrected Title"})
        assert again.status_code == 201, "a retry is answered like a new job"
        assert again.json()["id"] == 1, "same job, not a second row"
        (row,) = rows(client)
        assert (row["status"], row["error"]) == ("queued", None)
        assert json.loads(row["metadata"])["title"] == "Corrected Title"
        assert len(fake.calls("torrents/add")) == 2


def test_qbittorrent_refusing_the_torrent_is_a_502(client, fake):
    fake.add_result = "Fails."
    response = post(client)
    assert response.status_code == 502
    assert response.json()["job_id"] == 1
    assert rows(client)[0]["status"] == "failed"


def test_a_wrong_qbittorrent_password_is_reported_and_the_job_failed(tmp_path, fake):
    with make_client(tmp_path, fake, QBIT_PASSWORD="not the password") as client:
        response = post(client)
        assert response.status_code == 503
        assert "wrong user or password" in response.json()["detail"]
        assert rows(client)[0]["status"] == "failed"
        # And it stays refused without hammering qBittorrent (ban protection).
        post(client)
        assert fake.logins == 1


def test_without_a_qbittorrent_password_nothing_is_stored(tmp_path):
    with make_client(tmp_path, None) as client:
        response = post(client)
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]
        assert rows(client) == []


def test_without_a_database_the_answer_is_503_and_qbittorrent_is_not_asked(tmp_path, fake):
    with make_client(tmp_path, fake, CONFIG_DIR=str(tmp_path / "missing")) as client:
        response = post(client)
        assert response.status_code == 503
        assert "database" in response.json()["detail"]
        assert fake.calls("torrents/add") == []


# --- security and logging ----------------------


@pytest.mark.security
def test_user_text_cannot_reach_the_sql(client):
    title = "x'); DROP TABLE jobs;--"
    assert post(client, metadata={**MOVIE, "title": title}).status_code == 201
    assert json.loads(rows(client)[0]["metadata"])["title"] == title
    assert client.app.state.db.query("SELECT COUNT(*) FROM jobs")[0][0] == 1


@pytest.mark.security
def test_the_log_has_the_infohash_but_never_the_link_or_its_tracker_passkey(tmp_path, fake, capfd):
    with make_client(tmp_path, fake) as client:  # inside the test: capfd sees the app's logging
        post(client)
    log = capfd.readouterr().out
    assert HASH in log
    assert "passkey" not in log
    assert "secret-passkey" not in log
    assert MAGNET not in log


@pytest.mark.security
def test_a_hostile_title_cannot_become_a_path_or_a_second_torrent(client, fake):
    for title in ("../../etc/passwd", "a\nb", "a/b"):
        assert post(client, metadata={**MOVIE, "title": title}).status_code == 422
    assert fake.calls("torrents/add") == []


# --- the job function on its own ----------------------


def test_simultaneous_submissions_of_one_magnet_create_exactly_one_job(tmp_path):
    from ytp import jobs
    from ytp.db import Database

    database = Database.open(tmp_path / "app.db")
    outcomes: list[str] = []

    def submit():
        outcomes.append(jobs.submit_magnet_job(database, HASH, "{}")[1])

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["created"] + ["duplicate"] * 7
    assert database.query("SELECT COUNT(*) FROM jobs")[0][0] == 1
    database.close()


def test_mark_failed_cuts_a_very_long_error(tmp_path):
    from ytp import jobs
    from ytp.db import Database

    database = Database.open(tmp_path / "app.db")
    job, _ = jobs.submit_magnet_job(database, HASH, "{}")
    jobs.mark_failed(database, job.id, "x" * 5000)
    stored = jobs.get_job(database, job.id)
    assert stored is not None
    assert stored.status == "failed"
    assert len(stored.error or "") == jobs.MAX_ERROR_LENGTH
    assert jobs.get_job(database, 999) is None
    database.close()
