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
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


def make(tmp_path: Path, **env: str) -> TestClient:
    settings = Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path), **env})
    return TestClient(create_app(settings))


def test_health_is_ok_when_the_data_root_is_there(tmp_path):
    with make(tmp_path) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "checks": {"data_root": "ok"},
    }


def test_health_is_503_degraded_when_the_media_volume_is_missing(tmp_path):
    """So Docker's health check notices an empty mount point or a detached volume."""
    with make(tmp_path / "not-mounted") as client:
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


def test_the_app_has_only_the_health_route(tmp_path):
    app = create_app(Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path)}))
    paths = {getattr(route, "path", None) for route in app.routes}
    assert paths == {"/health"}


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
    for name, value in {**ENV, "DATA_ROOT": str(tmp_path)}.items():
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
