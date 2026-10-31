"""Is this Docker host alive and fit to run the stack? Fast; runs first on any real host.

In CI these run on the ytp-host runner (once YTP_HOST_RUNNER=1) against its Docker
daemon. On the media VM run `make smoke-host`, which sets SMOKE_FULL_HOST=1 and
SMOKE_ENV_FILE=.env so the full host checks (VM, tun, media volume) run against the
real configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers import docker
from tests.helpers.compose_cli import ENV_EXAMPLE, compose
from tests.helpers.shell import run_script

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_HOST = REPO_ROOT / "scripts" / "host" / "check-host.sh"
FULL_HOST = os.environ.get("SMOKE_FULL_HOST") == "1"
ENV_FILE = Path(os.environ.get("SMOKE_ENV_FILE", str(ENV_EXAMPLE)))


@pytest.mark.docker
def test_docker_and_compose_meet_minimums():
    result = run_script(CHECK_HOST, ["--only", "docker,compose"])
    assert result.returncode == 0, result.stdout


@pytest.mark.docker
def test_daemon_can_run_a_container():
    result = docker.run(["run", "--rm", docker.BUSYBOX, "true"], timeout=180)
    assert result.returncode == 0, result.stderr


@pytest.mark.compose
def test_stack_definition_resolves_with_this_env_file():
    result = compose("config", "--quiet", env_file=ENV_FILE)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not FULL_HOST, reason="full host checks run only on the media VM")
def test_full_host_check_passes():
    result = run_script(CHECK_HOST, ["--env-file", str(ENV_FILE)])
    assert result.returncode == 0, result.stdout
