"""The real app image builds, runs as a non-root user and reports healthy (issue #8).

CI has no Docker daemon, so this runs only on a host that has one (the ytp-host runner, or
by hand on the VM: `make test-system`). It builds app/Dockerfile, starts the container as an
arbitrary uid like docker-compose.yml does (`user: PUID:PGID`), and watches Docker's own
health check turn healthy.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import docker
from tests.helpers.compose_cli import REPO_ROOT

pytestmark = [pytest.mark.docker, pytest.mark.network, pytest.mark.slow]

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


def env_args(env: dict[str, str]) -> list[str]:
    return [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = docker.unique_name("app-image")
    built = docker.run(["build", "-t", tag, str(REPO_ROOT / "app")], timeout=900)
    assert built.returncode == 0, built.stdout + built.stderr
    yield tag
    docker.run(["rmi", "-f", tag])


def start(
    image: str, env: dict[str, str], data: Path | None = None, config: Path | None = None
) -> str:
    name = docker.unique_name("app")
    args = ["run", "-d", "--name", name, "--user", "4321:4321", *env_args(env)]
    if data is not None:
        args += ["-v", f"{data}:/data:ro"]
    if config is not None:
        args += ["-v", f"{config}:/config"]
    started = docker.run([*args, image], timeout=120)
    assert started.returncode == 0, started.stderr
    return name


def folders(tmp_path: Path, config_mode: int = 0o777) -> tuple[Path, Path]:
    """A media folder and a /config folder that the container user (4321) may or may not write."""
    data, config = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    config.mkdir()
    config.chmod(config_mode)
    return data, config


def test_the_container_becomes_healthy_as_a_non_root_user(image, tmp_path):
    data, config = folders(tmp_path)
    name = start(image, ENV, data, config)
    try:
        deadline = time.monotonic() + 120
        status = ""
        while time.monotonic() < deadline and status != "healthy":
            time.sleep(3)
            status = docker.run(["inspect", "-f", "{{.State.Health.Status}}", name]).stdout.strip()
        assert status == "healthy", docker.run(["logs", name]).stdout
        assert docker.run(["exec", name, "id", "-u"]).stdout.strip() == "4321"
        assert docker.run(["port", name]).stdout.strip() == ""
        assert (config / "app.db").exists(), "the migrated database is created in /config"
    finally:
        docker.run(["rm", "-f", name])


def test_the_health_endpoint_is_503_without_the_media_volume(image):
    name = start(image, ENV)  # no /data mount with content: /data does not exist
    try:
        time.sleep(8)
        probe = (
            "import urllib.request, urllib.error\n"
            "try:\n urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)\n"
            "except urllib.error.HTTPError as e:\n print(e.code)\n"
        )
        assert docker.run(["exec", name, "python", "-c", probe]).stdout.strip() == "503"
    finally:
        docker.run(["rm", "-f", name])


def test_a_bad_configuration_exits_2_with_a_json_error(image):
    name = start(image, {})
    try:
        deadline = time.monotonic() + 30
        state = ""
        while time.monotonic() < deadline and state != "exited":
            time.sleep(1)
            state = docker.run(["inspect", "-f", "{{.State.Status}}", name]).stdout.strip()
        assert state == "exited"
        assert docker.run(["inspect", "-f", "{{.State.ExitCode}}", name]).stdout.strip() == "2"
        last = docker.run(["logs", name]).stdout.strip().splitlines()[-1]
        assert json.loads(last)["msg"] == "invalid configuration"
    finally:
        docker.run(["rm", "-f", name])


def test_a_config_folder_the_user_cannot_write_makes_the_app_unhealthy_with_a_hint(image, tmp_path):
    """What Docker's root:root ./config/app does to an app running as PUID:PGID."""
    data, config = folders(tmp_path, config_mode=0o755)  # owned by us, not by uid 4321
    name = start(image, ENV, data, config)
    try:
        time.sleep(8)
        probe = (
            "import urllib.request, urllib.error\n"
            "try:\n urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)\n"
            "except urllib.error.HTTPError as e:\n print(e.code, e.read().decode())\n"
        )
        answer = docker.run(["exec", name, "python", "-c", probe]).stdout
        assert answer.startswith("503"), answer
        assert "database" in answer
        logs = docker.run(["logs", name]).stdout
        assert '"msg": "database unavailable"' in logs
        assert "make layout" in logs
    finally:
        docker.run(["rm", "-f", name])


def wait_healthy(name: str, timeout: float = 120) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline and status != "healthy":
        time.sleep(2)
        status = docker.run(["inspect", "-f", "{{.State.Health.Status}}", name]).stdout.strip()
    return status


def test_the_scheduler_survives_a_container_restart(image, tmp_path):
    """The heartbeat of the first run is found by the second (issue #12)."""
    data, config = folders(tmp_path)
    name = start(image, ENV, data, config)
    try:
        assert wait_healthy(name) == "healthy", docker.run(["logs", name]).stdout
        assert docker.run(["restart", name], timeout=120).returncode == 0
        assert wait_healthy(name) == "healthy", docker.run(["logs", name]).stdout
        starts = [
            json.loads(line)
            for line in docker.run(["logs", name]).stdout.splitlines()
            if line.startswith("{") and '"scheduler started"' in line
        ]
        assert len(starts) == 2
        assert starts[0]["previous_heartbeat"] is None
        assert starts[1]["previous_heartbeat"], "the second start found the first run's heartbeat"
        assert starts[1]["jobs"] == ["heartbeat"]
    finally:
        docker.run(["rm", "-f", name])
