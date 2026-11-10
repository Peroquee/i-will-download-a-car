"""GET /api/dashboard and the alert endpoints (issue #38)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from ytp import alertlog, jobs, storage
from ytp.clients.qbittorrent import Torrent
from ytp.config import Settings
from ytp.jobs_api import TorrentProgress
from ytp.main import create_app
from ytp.scheduler import set_state

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hb",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
H1, H2 = "1" * 40, "2" * 40
MOVIE = '{"type": "movies", "title": "Big Film", "year": 2001}'
VIDEO = '{"type": "youtube", "video_id": "aaaaaaaaaaa", "channel": "Blender", "title": "Clip"}'


class FakeQbit:
    def __init__(self, torrents):
        self.torrents = torrents

    def list_torrents(self):
        return self.torrents


class FakeWatchdog:
    def __init__(self, status):
        self.status = status

    def fetch_status(self):
        return self.status


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    config = tmp_path / "config"
    config.mkdir()
    values = {
        **ENV,
        "DATA_ROOT": str(tmp_path),
        "CONFIG_DIR": str(config),
        "SUBSCRIPTION_POLL_MINUTES": "0",
        "PODCAST_POLL_MINUTES": "0",
    }
    with TestClient(create_app(Settings.from_env(values)), base_url="http://nas.lan") as c:
        c.app.state.watchdog_status = FakeWatchdog({"state": "ok", "egress": "ok"})
        yield c


def add_job(client, job_type, external_id, metadata, status):
    db = client.app.state.db
    job, _ = jobs.submit_job(db, job_type, external_id, metadata, "magnet:?xt=urn:btih:secret")
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job.id))
    return job


# --- the alert endpoints --------------------------------------------------------------------


def test_the_alert_list_has_unread_count_and_newest_first(client):
    db = client.app.state.db
    alertlog.add(db, "disk_low", "warning", "Disk", "d", {"free_percent": 4})
    second = alertlog.add(db, "vpn_down", "error", "VPN", "d")
    body = client.get("/api/alerts").json()
    assert body["unread"] == 2
    assert body["alerts"][0]["id"] == second.id
    assert set(body["alerts"][0]) == {
        "id", "time", "kind", "severity", "title", "detail", "params", "acknowledged",
    }  # fmt: skip


def test_limit_and_unread_filters(client):
    db = client.app.state.db
    ids = [alertlog.add(db, "disk_low", "warning", f"t{i}").id for i in range(5)]
    alertlog.acknowledge(db, ids[4])
    assert len(client.get("/api/alerts?limit=2").json()["alerts"]) == 2
    unread = client.get("/api/alerts?unread=1").json()
    assert ids[4] not in [a["id"] for a in unread["alerts"]]
    assert unread["unread"] == 4
    assert len(client.get("/api/alerts?limit=0").json()["alerts"]) == 1, "limit is clamped"


def test_one_alert_by_id_and_404_for_none(client):
    alert = alertlog.add(client.app.state.db, "vpn_down", "error", "VPN")
    assert client.get(f"/api/alerts/{alert.id}").json()["kind"] == "vpn_down"
    assert client.get("/api/alerts/999").status_code == 404
    assert client.get("/api/alerts/abc").status_code == 422


def test_acknowledging_one_and_all(client):
    db = client.app.state.db
    a = alertlog.add(db, "disk_low", "warning", "a")
    alertlog.add(db, "disk_low", "warning", "b")
    reply = client.post(f"/api/alerts/{a.id}/ack")
    assert reply.status_code == 200
    assert reply.json()["acknowledged"] is True
    assert client.post("/api/alerts/999/ack").status_code == 404
    assert client.get("/api/alerts").json()["unread"] == 1
    assert client.post("/api/alerts/ack-all").json() == {"acknowledged": 1}
    assert client.get("/api/alerts").json()["unread"] == 0


@pytest.mark.security
@pytest.mark.parametrize("path", ["/api/alerts/ack-all", "/api/alerts/1/ack"])
def test_acknowledging_from_another_site_is_refused(client, path):
    alertlog.add(client.app.state.db, "disk_low", "warning", "a")
    reply = client.post(
        path, headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    )
    assert reply.status_code == 403
    assert client.get("/api/alerts").json()["unread"] == 1


def test_alerts_answer_503_without_a_database(tmp_path):
    settings = Settings.from_env(
        {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(tmp_path / "missing")}
    )
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/alerts").status_code == 503
        assert c.get("/api/dashboard").status_code == 503
        assert c.post("/api/alerts/ack-all").status_code == 503


# --- the dashboard --------------------------------------------------------------------------


def torrent(hash_, progress, size, left, speed, eta):
    return Torrent(
        hash=hash_, name="n", state="downloading", progress=progress, size=size,
        amount_left=left, dlspeed=speed, eta=eta,
    )  # fmt: skip


def test_the_active_list_has_live_numbers_for_a_torrent(client):
    add_job(client, "magnet", H1, MOVIE, "downloading")
    add_job(client, "magnet", H2, MOVIE, "queued")
    client.app.state.torrent_progress = TorrentProgress(
        FakeQbit([torrent(H1, 0.25, 4000, 3000, 500, 6)]), ttl=0
    )
    rows = client.get("/api/dashboard").json()["active"]
    running, waiting = rows
    assert (running["label"], running["status"]) == ("Big Film (2001)", "downloading")
    assert running["progress"] == 25
    assert (running["done_bytes"], running["total_bytes"]) == (1000, 4000)
    assert (running["speed"], running["eta"]) == (500, 6)
    assert waiting["status"] == "queued"
    assert waiting["progress"] is None
    assert waiting["speed"] is None


def test_the_lists_mark_high_priority_jobs(client):
    high = add_job(client, "magnet", H1, MOVIE, "downloading")
    normal = add_job(client, "magnet", H2, MOVIE, "downloading")
    finished = add_job(client, "magnet", "3" * 40, MOVIE, "done")
    with client.app.state.db.transaction() as conn:
        conn.execute("UPDATE jobs SET priority = 1 WHERE id IN (?, ?)", (high.id, finished.id))
    body = client.get("/api/dashboard").json()
    active = {row["id"]: row for row in body["active"]}
    assert active[high.id]["high_priority"] is True
    assert active[normal.id]["high_priority"] is False
    assert body["recent"][0]["high_priority"] is True


def test_an_unknown_eta_and_a_torrent_without_metadata_are_null(client):
    add_job(client, "magnet", H1, MOVIE, "downloading")
    client.app.state.torrent_progress = TorrentProgress(
        FakeQbit([torrent(H1, 0.0, 0, 0, 0, 8640000)]), ttl=0
    )
    row = client.get("/api/dashboard").json()["active"][0]
    assert row["eta"] is None
    assert row["speed"] is None
    assert row["total_bytes"] is None


def test_a_youtube_download_shows_what_the_worker_reports(client):
    job = add_job(client, "youtube", "aaaaaaaaaaa", VIDEO, "downloading")
    worker = client.app.state.youtube_worker
    worker.progress[job.id] = 40
    worker.details[job.id] = {"done_bytes": 400, "total_bytes": 1000, "speed": 100, "eta": 6}
    row = client.get("/api/dashboard").json()["active"][0]
    assert row["label"] == "Blender - Clip"
    assert (row["progress"], row["done_bytes"], row["total_bytes"]) == (40, 400, 1000)
    assert (row["speed"], row["eta"]) == (100, 6)


def test_an_organizing_job_is_at_100_and_finished_ones_are_not_active(client):
    add_job(client, "magnet", H1, MOVIE, "organizing")
    add_job(client, "magnet", H2, MOVIE, "done")
    rows = client.get("/api/dashboard").json()["active"]
    assert [r["progress"] for r in rows] == [100]


def test_the_recent_list_has_reasons_and_retry_for_failed_jobs(client):
    done = add_job(client, "magnet", H1, MOVIE, "done")
    failed = add_job(client, "magnet", H2, MOVIE, "failed")
    with client.app.state.db.transaction() as conn:
        conn.execute("UPDATE jobs SET error = 'no sources for 7 days' WHERE id = ?", (failed.id,))
        conn.execute(
            "UPDATE jobs SET updated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?", (done.id,)
        )
    rows = client.get("/api/dashboard").json()["recent"]
    assert [r["id"] for r in rows] == [failed.id, done.id], "newest change first"
    assert rows[0]["error"] == "no sources for 7 days"
    assert rows[0]["can_retry"] is True
    assert rows[1]["can_retry"] is False
    assert "source" not in rows[0], "the magnet link is never part of an answer"
    assert "secret" not in client.get("/api/dashboard").text


def test_a_done_job_with_a_note_shows_it_in_the_recent_list(client):
    """A job that finished although files were left out says so on the dashboard (#69)."""
    done = add_job(client, "magnet", H1, MOVIE, "done")
    with client.app.state.db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET note = '2 of 10 files skipped: x.mkv: no episode' WHERE id = ?",
            (done.id,),
        )
    row = client.get("/api/dashboard").json()["recent"][0]
    assert row["note"] == "2 of 10 files skipped: x.mkv: no episode"


def test_the_recent_list_is_limited(client):
    for i in range(25):
        add_job(client, "youtube", f"{i:011d}", VIDEO, "done")
    assert len(client.get("/api/dashboard").json()["recent"]) == 20


def test_the_vpn_part_has_the_safe_status_and_the_history(client):
    db = client.app.state.db
    alertlog.record_vpn_event(db, "down", True, "gluetun stopped")
    alertlog.record_vpn_event(db, "ok", False, "")
    client.app.state.watchdog_status = FakeWatchdog(
        {"state": "ok", "egress": "ok", "egress_country": "Switzerland", "since": "t",
         "last_poll": "x", "ip": "1.2.3.4", "egress_checked": "z"}
    )  # fmt: skip
    vpn = client.get("/api/dashboard").json()["vpn"]
    assert vpn["current"] == {
        "state": "ok", "since": "t", "egress": "ok", "egress_country": "Switzerland",
    }  # fmt: skip
    assert [e["condition"] for e in vpn["history"]] == ["ok", "down"]
    assert "1.2.3.4" not in str(vpn)


def test_a_missing_watchdog_is_unknown_not_an_error(client):
    client.app.state.watchdog_status = None
    assert client.get("/api/dashboard").json()["vpn"]["current"] == {"state": "unknown"}


def test_the_dashboard_reports_the_free_disk_space(client):
    """Free and total stay live; the category sizes are null until the first walk (#82)."""
    disk = client.get("/api/dashboard").json()["disk"]
    assert {"free_percent", "free_gb", "total_gb"} <= set(disk)
    assert {"total_bytes", "free_bytes"} <= set(disk)
    assert disk["categories"] is None
    assert (disk["downloads"], disk["other"], disk["at"]) == (None, None, None)


def test_the_dashboard_reports_the_cached_category_sizes(client):
    set_state(
        client.app.state.db,
        storage.CACHE_KEY,
        json.dumps(
            {
                "at": "2026-11-10T14:05:00.000Z",
                "categories": {
                    kind: 1000
                    for kind in ("movies", "series", "audiobooks", "music", "podcasts", "youtube")
                },
                "downloads": 300,
                "other": 400,
            }
        ),
    )
    disk = client.get("/api/dashboard").json()["disk"]
    assert set(disk["categories"]) == {
        "movies",
        "series",
        "audiobooks",
        "music",
        "podcasts",
        "youtube",
    }
    assert disk["categories"]["movies"] == 1000
    assert (disk["downloads"], disk["other"]) == (300, 400)
    assert disk["at"] == "2026-11-10T14:05:00.000Z"
    assert disk["free_percent"] is not None, "still live"


def test_the_app_starts_the_storage_sampler(client):
    assert client.app.state.storage is not None
    assert client.app.state.youtube_worker._on_done is not None
    assert client.app.state.podcast_worker._on_done is not None


def test_the_dashboard_page_is_the_same_page_as_the_form(client):
    assert client.get("/dashboard").text == client.get("/").text
    assert client.get("/dashboard").headers["cache-control"] == "no-cache"


# --- worker progress details ----------------------------------------------------------------


def test_the_youtube_hook_records_size_speed_and_eta(client):
    worker = client.app.state.youtube_worker
    job = add_job(client, "youtube", "bbbbbbbbbbb", VIDEO, "downloading")
    hook = worker._progress(job)
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 250,
            "total_bytes": 1000,
            "speed": 50.5,
            "eta": 15,
        }
    )
    assert worker.details[job.id] == {
        "done_bytes": 250,
        "total_bytes": 1000,
        "speed": 50,
        "eta": 15,
    }
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 300,
            "total_bytes_estimate": 1000,
            "speed": None,
            "eta": None,
        }
    )
    assert worker.details[job.id]["speed"] is None
    assert worker.details[job.id]["eta"] is None


def test_the_podcast_hook_records_size_and_derives_speed(client):
    worker = client.app.state.podcast_worker
    job = add_job(client, "podcast", "k" * 40, '{"type": "podcast"}', "downloading")
    hook = worker._progress(job)
    hook(500, 1000)
    details = worker.details[job.id]
    assert (details["done_bytes"], details["total_bytes"]) == (500, 1000)
    assert details["speed"] is None or details["speed"] > 0
    hook(0, None)  # unknown size: nothing recorded, nothing breaks


# --- the automatic update's status (#42) ---------------------------------------------------


def test_the_dashboard_shows_the_automatic_updates_status_when_there_is_one(client, tmp_path):
    assert client.get("/api/dashboard").json()["deploy"] is None
    status = {
        "enabled": True, "paused": False, "halted": False, "deployed": "abc123def456",
        "deployed_at": "2026-11-02T10:00:00Z", "result": "deployed", "message": "m",
        "checked_at": "2026-11-02T10:05:00Z", "secret_extra": "must not pass",
    }  # fmt: skip
    (tmp_path / "config" / "autodeploy-status.json").write_text(json.dumps(status))
    deploy = client.get("/api/dashboard").json()["deploy"]
    assert deploy["deployed"] == "abc123def456"
    assert deploy["result"] == "deployed"
    assert "secret_extra" not in deploy
    assert "enabled" not in deploy


@pytest.mark.parametrize("text", ["not json", "[]", '{"enabled": false}', ""])
def test_a_broken_or_disabled_status_file_is_ignored(client, tmp_path, text):
    (tmp_path / "config" / "autodeploy-status.json").write_text(text)
    assert client.get("/api/dashboard").json()["deploy"] is None
