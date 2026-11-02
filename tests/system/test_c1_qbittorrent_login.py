"""A real qBittorrent accepts the seeded login and the VPN binding (issue #3).

Starts the linuxserver image on its own (no VPN needed for this question) with a
config written by scripts/qbittorrent/seed_config.py, then asks its Web API.
This is what proves the INI key spelling and the PBKDF2 format against the real
program; the unit tests can only check them against our own reading of the format.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import docker
from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT

pytestmark = [pytest.mark.docker, pytest.mark.network, pytest.mark.slow, pytest.mark.security]

SEED_SCRIPT = REPO_ROOT / "scripts" / "qbittorrent" / "seed_config.py"
CATEGORIES_SCRIPT = REPO_ROOT / "scripts" / "qbittorrent" / "setup_categories.py"
IMAGE = "lscr.io/linuxserver/qbittorrent:" + parse_env_file(ENV_EXAMPLE)["QBITTORRENT_TAG"]
PASSWORD = "a long test password"  # a test value, not a secret
USER = "tester"
CURL = ["curl", "-s", "-c", "/tmp/jar", "-b", "/tmp/jar"]


def sh(container: str, *args: str) -> str:
    result = docker.run(["exec", container, *args], timeout=60)
    return result.stdout.strip()


def seed(config_dir: Path, *extra: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(SEED_SCRIPT),
            "--config",
            str(config_dir / "qBittorrent" / "qBittorrent.conf"),
            "--password-stdin",  # the password comes in through stdin, as the installer does it
            *extra,
        ],
        input=PASSWORD + "\n",
        text=True,
        check=True,
        capture_output=True,
    )


def start_qbit(config_dir: Path, label: str) -> str:
    """Start the real image on `config_dir` and wait until its Web UI answers; returns its name."""
    name = docker.unique_name(label)
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
    wait_ready(name)
    return name


def wait_ready(name: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if sh(name, *CURL, "-o", "/dev/null", "-w", "%{http_code}", "http://localhost:8080/") in {
            "200",
            "401",
            "403",
        }:
            return
        time.sleep(2)
    pytest.fail("qBittorrent did not answer on :8080\n" + docker.run(["logs", name]).stdout)


@pytest.fixture(scope="module")
def qbittorrent(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    config_dir = tmp_path_factory.mktemp("qbit-config")
    seed(config_dir, "--user", USER)
    name = start_qbit(config_dir, "qbit-login")
    try:
        yield name
    finally:
        docker.run(["rm", "-f", name])


def login(
    container: str, password: str, *, keep_session: bool = False, headers: tuple[str, ...] = ()
) -> bool:
    """True if qBittorrent accepts the login. Current versions answer a good login with 204
    and no body, a bad one with 401; older ones answer 200 "Ok." / "Fails.".

    By default NO cookie is sent: qBittorrent answers a login that arrives with a valid session
    cookie by simply saying "Ok" without looking at the password (AuthController::loginAction),
    so a shared cookie jar would make a wrong password look accepted after a good login.
    keep_session=True starts from an empty jar and leaves the session in it for later calls."""
    jar = CURL if keep_session else ["curl", "-s"]
    if keep_session:
        sh(container, "rm", "-f", "/tmp/jar")
    answer = sh(
        container,
        *jar,
        "-w",
        "\n%{http_code}",
        "--data-urlencode",
        f"username={USER}",
        "--data-urlencode",
        f"password={password}",
        *[arg for header in headers for arg in ("-H", header)],
        "http://localhost:8080/api/v2/auth/login",
    )
    body, _, code = answer.rpartition("\n")
    return code == "204" or (code == "200" and body.strip() == "Ok.")


def test_the_seeded_password_logs_in(qbittorrent):
    assert login(qbittorrent, PASSWORD)


def test_a_wrong_password_and_the_old_default_do_not(qbittorrent):
    assert login(qbittorrent, PASSWORD, keep_session=True)  # a good login first, as in real life
    assert not login(qbittorrent, "wrong password")
    assert not login(qbittorrent, "adminadmin")


def test_localhost_is_not_let_in_without_a_login(qbittorrent):
    """WebUI\\LocalHostAuth=true is honoured: the container's own loopback needs the login too."""
    code = sh(
        qbittorrent, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "http://localhost:8080/api/v2/app/version",
    )  # fmt: skip
    assert code == "403"


def test_no_temporary_password_is_generated(qbittorrent):
    logs = docker.run(["logs", qbittorrent]).stdout
    assert "temporary password" not in logs.lower()


def test_the_vpn_interface_binding_is_active(qbittorrent):
    assert login(qbittorrent, PASSWORD, keep_session=True)
    prefs = sh(qbittorrent, *CURL, "http://localhost:8080/api/v2/app/preferences")
    assert '"current_network_interface":"tun0"' in prefs.replace(" ", "")


# --- behind the proxy on :8081 ---------------------------------------------------------------
# qBittorrent answers 401 to a Host header whose port is not its own unless host header
# validation is off. The proxy sends "<lan address>:8081" to the Web UI on :8080.

PROXY_HOST = "Host: 192.168.178.123:8081"


def test_a_host_header_with_the_proxys_port_is_accepted(qbittorrent):
    assert login(qbittorrent, PASSWORD, headers=(PROXY_HOST,))


def test_the_host_header_of_the_proxy_works_for_the_session_too(qbittorrent):
    assert login(qbittorrent, PASSWORD, keep_session=True, headers=(PROXY_HOST,))
    code = sh(
        qbittorrent, *CURL, "-H", PROXY_HOST, "-o", "/dev/null", "-w", "%{http_code}",
        "http://localhost:8080/api/v2/app/version",
    )  # fmt: skip
    assert code == "200"


@pytest.mark.security
def test_csrf_protection_is_still_on_a_foreign_origin_is_refused(qbittorrent):
    """Validation of the Host header is off, but a page on another site still cannot log in or
    act: qBittorrent compares Origin with Host and answers 401."""
    foreign = ("Origin: http://evil.example",)
    assert not login(qbittorrent, PASSWORD, headers=foreign)
    assert not login(
        qbittorrent, PASSWORD, headers=(PROXY_HOST, "Origin: http://192.168.178.99:8081")
    )
    assert login(qbittorrent, PASSWORD, headers=(PROXY_HOST, "Origin: http://192.168.178.123:8081"))


def container_ip(name: str) -> str:
    return docker.run(["inspect", "-f", "{{.NetworkSettings.IPAddress}}", name]).stdout.strip()


def effective_settings(container: str) -> str:
    """What qBittorrent really uses and what its file says (for the message of a failing test)."""
    login(container, PASSWORD, keep_session=True)
    prefs = sh(container, *CURL, "http://localhost:8080/api/v2/app/preferences")
    wanted = ("web_ui_host_header_validation_enabled", "web_ui_csrf_protection_enabled")
    lines = sh(
        container, "grep", "-E", "HostHeaderValidation|CSRFProtection|^\\[",
        "/config/qBittorrent/qBittorrent.conf",
    )  # fmt: skip
    shown = {k: v for k, v in json.loads(prefs).items() if k in wanted}
    return f"preferences: {shown}\nconf:\n{lines}"


def test_an_existing_config_without_the_setting_is_repaired_through_the_api(tmp_path: Path):
    """The owner's case: a qBittorrent.conf from before the proxy answers 401 to the proxy's Host
    header. `setup_categories.py` sets the preferences through the Web API (no stop, no file
    editing), and the setting survives a restart of the container."""
    config_dir = tmp_path / "old-config"
    seed(config_dir, "--user", USER)
    conf = config_dir / "qBittorrent" / "qBittorrent.conf"
    kept = [
        line
        for line in conf.read_text().splitlines()
        if "HostHeaderValidation" not in line and "CSRFProtection" not in line
    ]
    conf.write_text("\n".join(kept) + "\n")  # the config as it was before the fix
    name = start_qbit(config_dir, "qbit-old")
    try:
        assert login(name, PASSWORD), "the plain login works"
        assert not login(name, PASSWORD, headers=(PROXY_HOST,)), "this is the 401 of the bug"
        done = subprocess.run(
            [
                sys.executable, "-I", str(CATEGORIES_SCRIPT), "--url", f"http://{container_ip(name)}:8080",
                "--user", USER, "--password-stdin",
            ],
            input=PASSWORD + "\n", text=True, capture_output=True, check=False,
        )  # fmt: skip
        assert done.returncode == 0, done.stdout + done.stderr
        assert "web_ui_host_header_validation_enabled" in done.stdout
        assert login(name, PASSWORD, headers=(PROXY_HOST,)), effective_settings(name)
        assert not login(name, PASSWORD, headers=("Origin: http://evil.example",)), "CSRF is on"
        # persisted: a restart of the container keeps it
        assert docker.run(["restart", name], timeout=180).returncode == 0
        wait_ready(name)
        assert login(name, PASSWORD, headers=(PROXY_HOST,)), effective_settings(name)
    finally:
        docker.run(["rm", "-f", name])
