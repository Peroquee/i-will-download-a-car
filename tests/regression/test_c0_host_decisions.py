"""Regression guards for issue #1 (C0): decisions that must never silently change.

Each test names the decision it pins. If one fails, the change it caught needs
an explicit issue and a deliberate update here, not a quick fix to the test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.helpers.compose import load_compose, services
from tests.helpers.compose_cli import REPO_ROOT, normalized_config, rendered_config
from tests.helpers.shell import StubBin, isolated_bin, run_script

CHECK_HOST = REPO_ROOT / "scripts" / "host" / "check-host.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "host" / "bootstrap.sh"
SNAPSHOT = Path(__file__).parent / "snapshots" / "c0-compose-config.json"


# --- decision: VM, not LXC ---


@pytest.mark.parametrize("container", ["lxc", "lxc-libvirt", "docker"])
def test_c0_host_check_refuses_docker_inside_a_container(tmp_path, container):
    """Decision: Docker runs in a full VM. Inside (unprivileged) LXC the gluetun
    killswitch, which depends on Docker network-namespace sharing, is unreliable."""
    stubs = StubBin(tmp_path / "stubs")
    stubs.path.mkdir()
    stubs.add(
        "systemd-detect-virt",
        f'[ "$1" = "--container" ] && {{ echo {container}; exit 0; }}; echo none; exit 1',
    )
    result = run_script(
        CHECK_HOST, ["--only", "virt"], stubs=stubs, isolate_path=isolated_bin(tmp_path)
    )
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL virt")


# --- decision: killswitch premise ---


@pytest.mark.security
def test_c0_qbittorrent_has_no_network_of_its_own():
    """Decision: qbittorrent lives in gluetun's network namespace and has no
    network config of its own. Any of these keys would give it a path around the VPN."""
    qbit = services(load_compose(REPO_ROOT / "docker-compose.yml"))["qbittorrent"]
    assert qbit["network_mode"] == "service:gluetun"
    for key in ("ports", "networks", "dns", "extra_hosts", "expose", "hostname"):
        assert key not in qbit, f"qbittorrent must not define {key!r}"


@pytest.mark.security
def test_c0_gluetun_keeps_tunnel_capabilities():
    gluetun = services(load_compose(REPO_ROOT / "docker-compose.yml"))["gluetun"]
    assert "NET_ADMIN" in gluetun["cap_add"]
    assert "/dev/net/tun:/dev/net/tun" in gluetun["devices"]


# --- decision: safe host scripts ---


@pytest.mark.security
def test_c0_host_check_never_executes_the_env_file(tmp_path):
    """Decision: .env holds VPN secrets and is parsed, never sourced."""
    marker = tmp_path / "pwned"
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATA_ROOT=/data\nPUID=1000\nPGID=1000\nUMASK=$(touch {marker})\n")
    run_script(
        CHECK_HOST,
        ["--only", "media", "--env-file", str(env_file)],
        env={"CHECK_ROOT": str(tmp_path)},
    )
    assert not marker.exists()


def test_c0_bootstrap_never_formats_a_disk_that_has_a_filesystem(tmp_path):
    """Decision: --format-ext4 only ever touches an empty device; media is irreplaceable."""
    stubs = StubBin(tmp_path / "stubs")
    stubs.path.mkdir()
    stubs.add("blkid", 'case "$*" in *TYPE*) echo ext4 ;; *UUID*) echo keep-me ;; esac')
    stubs.add("getent", "exit 0")
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=debian\nVERSION_CODENAME=bookworm\n")
    result = run_script(
        BOOTSTRAP,
        ["--user", "u", "--media-device", "/dev/sdb", "--format-ext4", "--dry-run"],
        stubs=stubs,
        env={"OS_RELEASE": str(os_release), "FSTAB_FILE": str(tmp_path / "fstab")},
    )
    assert result.returncode == 0, result.stderr
    assert "mkfs" not in result.stdout


# --- golden config ---


@pytest.mark.compose
def test_c0_rendered_compose_config_matches_snapshot():
    """Any change to what Compose would run must be deliberate.

    After an intended change, regenerate with:
        UPDATE_SNAPSHOTS=1 pytest tests/regression -k snapshot
    and review the snapshot diff in the merge request.
    """
    actual = normalized_config(rendered_config(profiles=("*",)))
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.skip("snapshot updated")
    assert SNAPSHOT.exists(), "no snapshot yet: run with UPDATE_SNAPSHOTS=1"
    expected = json.loads(SNAPSHOT.read_text())
    assert actual == expected
