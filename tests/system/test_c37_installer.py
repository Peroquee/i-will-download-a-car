"""The installer in a throwaway Debian 12 container (issue #37). Needs Docker AND internet. Opt-in:

    YTP_NETWORK_TESTS=1 make test-system

* `install.sh --dry-run --answers FILE` in `debian:12`: the real wrapper, the real Python program,
  the real Debian, a complete answers file; nothing is installed or changed, but every command the
  installer would run is printed, in the right order, and no password appears anywhere.
* The same script in `ubuntu:22.04` is refused with the Debian-12 message.

What this cannot do: install Docker and start the VPN stack inside a container (that needs a
real VM with /dev/net/tun; docs/host-setup.md and `make test-system` on the media VM cover it).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers import docker
from tests.helpers.compose_cli import REPO_ROOT

pytestmark = [
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

VPN_PASSWORD = "vpn-pass-SECRET-1"
WEB_PASSWORD = "web-pass-SECRET-2"


def answers(tmp_path: Path) -> Path:
    ovpn = tmp_path / "my.ovpn"
    ovpn.write_text("client\ndev tun\nremote vpn.example.net 1195\n")
    path = tmp_path / "answers"
    path.write_text(
        "vpn_kind=ovpn\n"
        f"ovpn_source=/answers/my.ovpn\n"
        f"vpn_user=vpn-user\nvpn_password={VPN_PASSWORD}\nweb_password={WEB_PASSWORD}\n"
        "lan_address=192.168.178.20\nlan_subnet=192.168.178.0/24\n"
    )
    path.chmod(0o600)
    return path


def test_the_dry_run_in_debian_12_shows_every_step_and_hides_every_secret(tmp_path):
    path = answers(tmp_path)
    script = (
        "set -e; apt-get update -qq && apt-get install -y -qq python3 >/dev/null; "
        "cp -r /src /work && cd /work && "
        "./install.sh --dry-run --lang en --answers /answers/answers"
    )
    result = docker.run(
        ["run", "--rm", "-v", f"{REPO_ROOT}:/src:ro", "-v", f"{tmp_path}:/answers:ro",
         "debian:12", "bash", "-c", script],
        timeout=900,
    )  # fmt: skip
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "TEST RUN" in output
    for needle in ("bootstrap.sh", "create-layout.sh", "render_ovpn.py", "set_password.py",
                   "seed_config.py", "setup_key.py", "docker compose up -d --build",
                   "setup_categories.py", "verify_proxy.py"):  # fmt: skip
        assert needle in output, needle
    assert VPN_PASSWORD not in output
    assert WEB_PASSWORD not in output
    assert path.exists()


def test_another_system_is_refused(tmp_path):
    result = docker.run(
        ["run", "--rm", "-v", f"{REPO_ROOT}:/src:ro", "ubuntu:22.04", "bash", "-c",
         "cp -r /src /work && cd /work && ./install.sh --dry-run"],
        timeout=300,
    )  # fmt: skip
    assert result.returncode == 1
    assert "Debian 12" in result.stderr
