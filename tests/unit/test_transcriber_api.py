"""The transcription PC's HTTP routes (docs/transcription.md): `ytp.transcriber_api`.

The real app with a temporary database and DATA_ROOT; the PC is the test client.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ytp import transcriptions
from ytp.config import Settings
from ytp.main import create_app

TOKEN = "k" * 43
HEADERS = {"X-Transcriber-Token": TOKEN}
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
SRT = "1\n00:00:00,000 --> 00:00:01,500\nHallo.\n"


def start(tmp_path: Path, **env: str) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    data = tmp_path / "data"
    for name in ENV.values():
        (data / name).mkdir(parents=True, exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(data), "CONFIG_DIR": str(config), **env}
    client = TestClient(create_app(Settings.from_env(values)))
    client.__enter__()
    return client


@pytest.fixture
def client(tmp_path):
    c = start(tmp_path, TRANSCRIBER_TOKEN=TOKEN)
    yield c
    c.__exit__(None, None, None)


def queue_video(client, name="Clip [aqz-KE-bpKQ].mp4", content=b"0123456789" * 100) -> Path:
    data = client.app.state.settings.data_root
    video = data / "Youtube" / "Chan" / name
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(content)
    transcriptions.enqueue(client.app.state.db, data, video, language="de")
    return video


def claim(client):
    return client.post("/api/transcriber/claim", json={"worker": "main-pc"}, headers=HEADERS)


def test_without_a_token_in_the_settings_the_routes_do_not_exist(tmp_path):
    c = start(tmp_path)
    try:
        assert c.get("/api/transcriber/status", headers=HEADERS).status_code == 404
        assert (
            c.post("/api/transcriber/claim", json={"worker": "x"}, headers=HEADERS).status_code
            == 404
        )
    finally:
        c.__exit__(None, None, None)


@pytest.mark.real_auth
def test_the_token_is_required_and_no_login_is(client):
    assert client.get("/api/transcriber/status").status_code == 401
    assert (
        client.get("/api/transcriber/status", headers={"X-Transcriber-Token": "x" * 43}).status_code
        == 401
    )
    answer = client.get("/api/transcriber/status", headers=HEADERS)
    assert answer.status_code == 200
    assert answer.json() == {
        "queued": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "pending_downloads": 0,
        "worker_last_seen": None,
    }


@pytest.mark.real_auth
def test_claim_stream_and_result_file_the_subtitle(client):
    video = queue_video(client)
    answer = claim(client)
    assert answer.status_code == 200
    job = answer.json()
    assert (job["id"], job["attempt"], job["language"]) == (1, 1, "de")
    assert job["name"] == "Clip [aqz-KE-bpKQ].mp4"
    assert job["size"] == 1000
    assert job["media_url"].startswith("/api/transcriber/jobs/1/media?key=")
    assert claim(client).status_code == 204  # nothing else waiting

    # the media link needs no token, ranges work (ffmpeg seeks in mp4 files)
    media = client.get(job["media_url"], headers={"Range": "bytes=10-19"})
    assert media.status_code == 206
    assert media.content == b"0123456789"
    assert client.get(job["media_url"][:-3] + "abc").status_code == 404

    beat = client.post("/api/transcriber/jobs/1/heartbeat", json={"progress": 40}, headers=HEADERS)
    assert beat.status_code == 200

    done = client.post(
        "/api/transcriber/jobs/1/result", json={"language": "de", "srt": SRT}, headers=HEADERS
    )
    assert done.status_code == 200
    assert done.json() == {"status": "done", "subtitle": "Clip [aqz-KE-bpKQ].de.srt"}
    assert video.with_name("Clip [aqz-KE-bpKQ].de.srt").read_text(encoding="utf-8") == SRT
    # the attempt is over: its link and its heartbeat are gone
    assert client.get(job["media_url"]).status_code == 404
    late = client.post("/api/transcriber/jobs/1/heartbeat", json={}, headers=HEADERS)
    assert late.status_code == 409


@pytest.mark.real_auth
def test_bad_results_are_refused(client):
    queue_video(client)
    claim(client)
    not_srt = client.post(
        "/api/transcriber/jobs/1/result", json={"language": "de", "srt": "hello"}, headers=HEADERS
    )
    assert not_srt.status_code == 422
    bad_lang = client.post(
        "/api/transcriber/jobs/1/result", json={"language": "x/", "srt": SRT}, headers=HEADERS
    )
    assert bad_lang.status_code == 422
    unknown = client.post(
        "/api/transcriber/jobs/9/result", json={"language": "de", "srt": SRT}, headers=HEADERS
    )
    assert unknown.status_code == 409


@pytest.mark.real_auth
def test_failures_are_retried_then_given_up(client):
    queue_video(client)
    for attempt in (1, 2, 3):
        assert claim(client).json()["attempt"] == attempt
        answer = client.post(
            "/api/transcriber/jobs/1/fail", json={"error": "CUDA out of memory"}, headers=HEADERS
        )
        assert answer.json()["status"] == ("failed" if attempt == 3 else "queued")
    assert claim(client).status_code == 204
    state = client.get("/api/transcriber/status", headers=HEADERS).json()
    assert state["failed"] == 1
    assert state["worker_last_seen"]


@pytest.mark.real_auth
def test_a_bad_worker_name_is_a_422(client):
    answer = client.post("/api/transcriber/claim", json={"worker": "../x"}, headers=HEADERS)
    assert answer.status_code == 422
