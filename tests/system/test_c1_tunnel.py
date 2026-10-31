"""The real stack: the tunnel comes up and qBittorrent leaves through it (issues #2, #3).

Needs real ExpressVPN credentials, so it runs only when YTP_VPN_ENV_FILE points at a
filled-in env file (on the media VM: YTP_VPN_ENV_FILE=.env make test-system). Without
it the tests skip, with that reason, even under REQUIRE_CAPABILITIES. The killswitch
itself (traffic blocked when the tunnel drops) is verified in #4, not here.

The stack is started under a throwaway project name (ytp-test-...), with its config
and data in a temp folder, and removed afterwards.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import docker
from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import COMPOSE_FILE

ENV_FILE = os.environ.get("YTP_VPN_ENV_FILE")
IP_URL = "https://api.ipify.org"

pytestmark = [
    pytest.mark.docker,
    pytest.mark.tun,
    pytest.mark.network,
    pytest.mark.slow,
    pytest.mark.security,
    pytest.mark.skipif(not ENV_FILE, reason="set YTP_VPN_ENV_FILE to a real .env to run this"),
]


class Stack:
    def __init__(self, project: str, workdir: Path):
        self.project = project
        self.workdir = workdir

    def compose(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        env_file = self.workdir / ".env"
        cmd = ["docker", "compose", "-p", self.project, "--project-directory", str(self.workdir)]
        cmd += ["-f", str(COMPOSE_FILE), "--env-file", str(env_file), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

    def container(self, service: str) -> str:
        return self.compose("ps", "-q", service).stdout.strip()

    def exec(self, service: str, *args: str) -> str:
        return docker.run(["exec", self.container(service), *args], timeout=60).stdout.strip()


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    workdir = tmp_path_factory.mktemp("ytp-stack")
    values = parse_env_file(Path(ENV_FILE or ""))
    values["DATA_ROOT"] = str(workdir / "data")  # keep the real media volume out of it
    (workdir / "data").mkdir()
    (workdir / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    (workdir / ".env").chmod(0o600)
    s = Stack(docker.unique_name("stack"), workdir)
    try:
        started = s.compose("up", "-d", "--wait", "--wait-timeout", "240", "gluetun", "qbittorrent")
        assert started.returncode == 0, started.stdout + started.stderr
        yield s
    finally:
        s.compose("down", "--volumes", "--remove-orphans")


def test_gluetun_reports_healthy(stack):
    status = docker.run(["inspect", "-f", "{{.State.Health.Status}}", stack.container("gluetun")])
    assert status.stdout.strip() == "healthy"


def test_the_tunnel_interface_exists_in_the_shared_namespace(stack):
    # Both containers see tun0: qbittorrent really lives in gluetun's namespace.
    assert "tun0" in stack.exec("gluetun", "ip", "link", "show")
    assert "tun0" in stack.exec("qbittorrent", "ip", "link", "show")


def test_qbittorrent_has_no_published_ports_of_its_own(stack):
    ports = docker.run(["port", stack.container("qbittorrent")]).stdout.strip()
    assert ports == ""


def test_traffic_leaves_through_the_vpn(stack):
    tunnel_ip = stack.exec("gluetun", "wget", "-qO-", "-T", "20", IP_URL)
    qbit_ip = stack.exec("qbittorrent", "curl", "-s", "-m", "20", IP_URL)
    assert tunnel_ip
    assert qbit_ip == tunnel_ip, "qbittorrent must exit through gluetun's tunnel"
    # And it is not our own address: ask from a container with the host's normal route.
    host = docker.run(
        ["run", "--rm", docker.BUSYBOX, "wget", "-qO-", "-T", "20", IP_URL], timeout=120
    )
    assert host.stdout.strip() not in {"", tunnel_ip}


def test_the_web_ui_answers_on_the_published_port(stack):
    deadline = time.monotonic() + 60
    code = ""
    while time.monotonic() < deadline and code not in {"200", "401", "403"}:
        code = stack.exec(
            "qbittorrent", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "localhost:8080"
        )
        time.sleep(2)
    assert code in {"200", "401", "403"}
