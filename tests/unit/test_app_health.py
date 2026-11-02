"""app/ytp/main.py: the /health endpoint, startup logging and the exposed surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ytp import __version__
from ytp.config import Settings
from ytp.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


def make(tmp_path: Path, **env: str) -> TestClient:
    """An app whose /config is a real folder (tmp_path/config), as the bind mount is."""
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    settings = Settings.from_env(
        {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), **env}
    )
    return TestClient(create_app(settings))


def test_health_is_ok_when_the_data_root_is_there(tmp_path):
    with make(tmp_path) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "checks": {"data_root": "ok", "database": "ok", "scheduler": "ok"},
    }


def test_health_is_503_degraded_when_the_media_volume_is_missing(tmp_path):
    """So Docker's health check notices an empty mount point or a detached volume."""
    with make(tmp_path, DATA_ROOT=str(tmp_path / "not-mounted")) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["data_root"] == "missing"


def test_health_does_not_depend_on_qbittorrent_or_jellyfin(tmp_path):
    # An unreachable Jellyfin address must not change the answer.
    with make(tmp_path, JELLYFIN_URL="http://192.0.2.1:8096", JELLYFIN_API_KEY="k") as client:
        assert client.get("/health").status_code == 200


@pytest.mark.security
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_no_interactive_api_docs_are_served(tmp_path, path):
    with make(tmp_path) as client:
        assert client.get(path).status_code == 404


def test_the_app_exposes_only_the_known_routes(tmp_path):
    """Every route is a deliberate decision: add new ones here, with their issue."""
    app = create_app(Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path)}))
    paths = {getattr(route, "path", None) for route in app.routes}
    assert paths == {
        "/health",
        "/api/magnet",
        "/api/vpn",
        "/api/youtube/probe",
        "/api/youtube/download",
        "/api/jobs",  # the job list (#26)
        "/api/jobs/{job_id}/retry",
        "/api/subscriptions",  # channel subscriptions (#19)
        "/api/subscriptions/{subscription_id}",
        "/api/subscriptions/{subscription_id}/poll",  # check now (#27)
        "/api/podcasts/probe",  # podcasts (#34)
        "/api/podcasts/episode",
        "/api/podcasts",
        "/api/podcasts/{subscription_id}",
        "/api/podcasts/{subscription_id}/poll",
        "/api/alerts",  # the alert log (#38)
        "/api/alerts/ack-all",
        "/api/alerts/{alert_id}",
        "/api/alerts/{alert_id}/ack",
        "/api/dashboard",
        "/api/version",  # the static version, to notice an update (#43)
        "/reload-needed",  # where a form goes that the scripts did not take over
        "/api/auth/login",  # accounts (#44)
        "/api/auth/logout",
        "/api/auth/me",
        "/api/auth/password",
        "/api/accounts",
        "/api/accounts/{account_id}",
        "/api/accounts/{account_id}/password",
        "/",
        "/dashboard",  # the same page, in its dashboard view (#38)
        "/static",  # the form's files (#25)
    }


def test_startup_logs_the_configuration_without_the_api_key(tmp_path, capsys):
    key = "s3cr3t-key-value"
    with make(tmp_path, JELLYFIN_URL="http://j:8096", JELLYFIN_API_KEY=key):
        pass
    out = capsys.readouterr().out
    lines = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    starting = next(x for x in lines if x["msg"] == "starting")
    assert starting["version"] == __version__
    assert starting["libraries"]["audiobooks"] == "Hörbücher"
    assert starting["jellyfin_configured"] is True
    assert key not in out


def test_startup_says_when_jellyfin_is_not_configured(tmp_path, capsys):
    with make(tmp_path):
        pass
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert any(x["level"] == "WARNING" and "jellyfin" in x["msg"] for x in lines)


def test_create_app_reads_the_environment_by_default(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    env = {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(tmp_path / "config")}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def run_module(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if not k.startswith(("MEDIA_DIR_", "JELLYFIN_"))}
    full.update(env)
    full["PYTHONPATH"] = str(REPO_ROOT / "app")
    return subprocess.run(
        [sys.executable, "-I", "-m", "ytp"],
        capture_output=True,
        text=True,
        env=full,
        check=False,
        timeout=30,
        cwd=REPO_ROOT,
    )


def test_a_bad_configuration_stops_the_process_with_a_json_error_and_exit_2():
    # -I ignores PYTHONPATH, so run without it via cwd=app instead.
    full = {k: v for k, v in os.environ.items() if not k.startswith(("MEDIA_DIR_", "JELLYFIN_"))}
    result = subprocess.run(
        [sys.executable, "-m", "ytp"],
        capture_output=True,
        text=True,
        env=full,
        check=False,
        timeout=30,
        cwd=REPO_ROOT / "app",
    )
    assert result.returncode == 2
    error = json.loads(result.stdout.strip().splitlines()[-1])
    assert error["msg"] == "invalid configuration"
    assert any("MEDIA_DIR_MOVIES" in p for p in error["problems"])


def test_health_is_503_when_the_database_folder_is_missing(tmp_path, capfd):
    """/config not mounted: the app stays up, says why in the log and in /health."""
    with make(tmp_path, CONFIG_DIR=str(tmp_path / "no-such-config")) as client:
        response = client.get("/health")
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["checks"]["data_root"] == "ok"
    assert body["checks"]["database"].startswith("unavailable: database folder")
    log = capfd.readouterr().out
    assert '"msg": "database unavailable"' in log
    assert "make layout" in log


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_health_is_503_when_the_database_folder_is_not_writable(tmp_path):
    """The VM case: Docker made ./config/app as root, the app runs as another user."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config").chmod(0o555)
    try:
        with make(tmp_path) as client:
            response = client.get("/health")
    finally:
        (tmp_path / "config").chmod(0o755)
    assert response.status_code == 503
    assert "not writable" in response.json()["checks"]["database"]


def test_qbittorrent_client_exists_only_with_a_password_and_does_not_touch_the_network(tmp_path):
    with make(tmp_path, QBIT_PASSWORD="a password 12", QBIT_URL="http://127.0.0.1:9") as client:
        qbit = client.app.state.qbit
        assert qbit is not None
        assert qbit.base_url == "http://127.0.0.1:9"
        assert "a password 12" not in repr(qbit)
        # Nothing was contacted at startup: port 9 is closed, yet /health is fine.
        assert client.get("/health").status_code == 200


def test_without_a_qbittorrent_password_downloads_are_disabled_and_the_log_says_so(tmp_path, capfd):
    with make(tmp_path) as client:
        assert client.app.state.qbit is None
        assert client.get("/health").status_code == 200, "health does not depend on qBittorrent"
    assert "qbittorrent is not configured" in capfd.readouterr().out


def test_health_notices_a_database_that_stops_being_usable(tmp_path):
    with make(tmp_path) as client:
        assert client.get("/health").status_code == 200
        client.app.state.db.close()
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"]["database"].startswith("unavailable: query failed")


def test_the_database_is_closed_when_the_app_stops(tmp_path):
    with make(tmp_path) as client:
        db = client.app.state.db
        assert db is not None
    with pytest.raises(Exception, match="closed"):
        db.query("SELECT 1")


def test_a_broken_database_file_does_not_stop_the_app(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "app.db").write_bytes(b"this is not a database" * 100)
    with make(tmp_path) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"]["database"].startswith("unavailable:")
