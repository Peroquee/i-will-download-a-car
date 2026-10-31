"""A real qBittorrent accepts the seeded login and the VPN binding (issue #3).

Starts the linuxserver image on its own (no VPN needed for this question) with a
config written by scripts/qbittorrent/seed_config.py, then asks its Web API.
This is what proves the INI key spelling and the PBKDF2 format against the real
program; the unit tests can only check them against our own reading of the format.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from tests.helpers import docker
from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT

pytestmark = [pytest.mark.docker, pytest.mark.network, pytest.mark.slow, pytest.mark.security]

SEED_SCRIPT = REPO_ROOT / "scripts" / "qbittorrent" / "seed_config.py"
IMAGE = "lscr.io/linuxserver/qbittorrent:" + parse_env_file(ENV_EXAMPLE)["QBITTORRENT_TAG"]
PASSWORD = "a long test password"  # a test value, not a secret
USER = "tester"
CURL = ["curl", "-s", "-c", "/tmp/jar", "-b", "/tmp/jar"]


def sh(container: str, *args: str) -> str:
    result = docker.run(["exec", container, *args], timeout=60)
    return result.stdout.strip()


@pytest.fixture(scope="module")
def qbittorrent(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    config_dir = tmp_path_factory.mktemp("qbit-config")
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(SEED_SCRIPT),
            "--user",
            USER,
            "--config",
            str(config_dir / "qBittorrent" / "qBittorrent.conf"),
            "--password-stdin",
        ],
        input=PASSWORD + "\n",
        text=True,
        check=True,
        capture_output=True,
    )
    name = docker.unique_name("qbit-login")
    started = docker.run(
        [
            "run", "-d", "--name", name,
            "-e", "PUID=1000", "-e", "PGID=1000", "-e", "TZ=UTC", "-e", "WEBUI_PORT=8080",
            "-v", f"{config_dir}:/config",
            IMAGE,
        ],
        timeout=600,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if sh(
                name, *CURL, "-o", "/dev/null", "-w", "%{http_code}", "http://localhost:8080/"
            ) in {
                "200",
                "401",
                "403",
            }:
                break
            time.sleep(2)
        else:
            pytest.fail("qBittorrent did not answer on :8080\n" + docker.run(["logs", name]).stdout)
        yield name
    finally:
        docker.run(["rm", "-f", name])


def login(container: str, password: str) -> str:
    return sh(
        container,
        *CURL,
        "--data-urlencode",
        f"username={USER}",
        "--data-urlencode",
        f"password={password}",
        "http://localhost:8080/api/v2/auth/login",
    )


def test_the_seeded_password_logs_in(qbittorrent):
    assert login(qbittorrent, PASSWORD) == "Ok."


def test_a_wrong_password_and_the_old_default_do_not(qbittorrent):
    assert login(qbittorrent, "wrong password") == "Fails."
    assert login(qbittorrent, "adminadmin") == "Fails."


def test_no_temporary_password_is_generated(qbittorrent):
    logs = docker.run(["logs", qbittorrent]).stdout
    assert "temporary password" not in logs.lower()


def test_the_vpn_interface_binding_is_active(qbittorrent):
    assert login(qbittorrent, PASSWORD) == "Ok."
    prefs = sh(qbittorrent, *CURL, "http://localhost:8080/api/v2/app/preferences")
    assert '"current_network_interface":"tun0"' in prefs.replace(" ", "")
