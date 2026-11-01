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


def start(image: str, env: dict[str, str], data: Path | None = None) -> str:
    name = docker.unique_name("app")
    args = ["run", "-d", "--name", name, "--user", "4321:4321", *env_args(env)]
    if data is not None:
        args += ["-v", f"{data}:/data:ro"]
    started = docker.run([*args, image], timeout=120)
    assert started.returncode == 0, started.stderr
    return name


def test_the_container_becomes_healthy_as_a_non_root_user(image, tmp_path):
    name = start(image, ENV, tmp_path)
    try:
        deadline = time.monotonic() + 120
        status = ""
        while time.monotonic() < deadline and status != "healthy":
            time.sleep(3)
            status = docker.run(["inspect", "-f", "{{.State.Health.Status}}", name]).stdout.strip()
        assert status == "healthy", docker.run(["logs", name]).stdout
        assert docker.run(["exec", name, "id", "-u"]).stdout.strip() == "4321"
        assert docker.run(["port", name]).stdout.strip() == ""
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
