"""The Docker host really provides what the stack's design depends on (issue #1).

Black-box: each test starts throwaway containers on the real daemon and
observes behaviour. Nothing here knows about our compose file; these prove the
*host* supports the mechanisms the compose file relies on:

- a container with NET_ADMIN and /dev/net/tun can create a TUN interface (gluetun)
- without NET_ADMIN it cannot (so cap_add is load-bearing, not decoration)
- one container can join another's network namespace (qbittorrent in gluetun)
- PUID/PGID + UMASK 002 produce group-writable files on a volume
- restart: unless-stopped restarts a crashed container but respects a manual stop
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest

from tests.helpers import docker

pytestmark = [pytest.mark.docker, pytest.mark.slow]

# Opens /dev/net/tun and creates interface ytptest0 via the TUNSETIFF ioctl,
# then reports whether the kernel now lists it. Exit 0 on success, 1 on EPERM.
TUN_PROBE = r"""
import fcntl, os, struct, sys
TUNSETIFF, IFF_TUN, IFF_NO_PI = 0x400454CA, 0x0001, 0x1000
try:
    fd = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", b"ytptest0", IFF_TUN | IFF_NO_PI))
except PermissionError as exc:
    print("EPERM", exc); sys.exit(1)
print("created" if os.path.exists("/sys/class/net/ytptest0") else "missing")
"""


@pytest.fixture
def cleanup() -> Iterator[list[tuple[str, str]]]:
    """Collect ("container"|"volume", name) pairs; removed after the test, pass or fail."""
    created: list[tuple[str, str]] = []
    yield created
    for kind, name in reversed(created):
        if kind == "container":
            docker.run(["rm", "-f", name])
        else:
            docker.run(["volume", "rm", "-f", name])


@pytest.mark.tun
@pytest.mark.security
def test_net_admin_and_tun_device_allow_creating_a_tunnel():
    result = docker.run(
        [
            "run",
            "--rm",
            "--cap-add",
            "NET_ADMIN",
            "--device",
            "/dev/net/tun",
            docker.PYTHON,
            "python3",
            "-c",
            TUN_PROBE,
        ],
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "created"


@pytest.mark.tun
@pytest.mark.security
def test_without_net_admin_creating_a_tunnel_is_denied():
    result = docker.run(
        ["run", "--rm", "--device", "/dev/net/tun", docker.PYTHON, "python3", "-c", TUN_PROBE],
        timeout=300,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "EPERM" in result.stdout


@pytest.mark.security
def test_container_can_share_another_containers_network_namespace(cleanup):
    owner = docker.unique_name("netns-owner")
    cleanup.append(("container", owner))
    started = docker.run(["run", "-d", "--name", owner, docker.BUSYBOX, "sleep", "300"])
    assert started.returncode == 0, started.stderr

    def mac_and_hostname(*network_args: str) -> str:
        result = docker.run(
            [
                "run",
                "--rm",
                *network_args,
                docker.BUSYBOX,
                "sh",
                "-c",
                "cat /sys/class/net/eth0/address; hostname",
            ]
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    owner_view = docker.run(
        ["exec", owner, "sh", "-c", "cat /sys/class/net/eth0/address; hostname"]
    ).stdout
    joined_view = mac_and_hostname("--network", f"container:{owner}")
    separate_view = mac_and_hostname()

    assert joined_view == owner_view, "joined container must see the same interface"
    assert separate_view != owner_view, "a normal container must get its own interface"


def test_puid_pgid_and_umask_make_group_writable_files(cleanup):
    volume = docker.unique_name("media")
    cleanup.append(("volume", volume))
    assert docker.run(["volume", "create", volume]).returncode == 0
    # Prepare the volume like bootstrap.sh prepares /data: owned by PUID:PGID, setgid 2775.
    prep = docker.run(
        [
            "run",
            "--rm",
            "-v",
            f"{volume}:/data",
            docker.BUSYBOX,
            "sh",
            "-c",
            "chown 4242:4242 /data && chmod 2775 /data",
        ]
    )
    assert prep.returncode == 0, prep.stderr

    write = docker.run(
        [
            "run",
            "--rm",
            "--user",
            "4242:4242",
            "-v",
            f"{volume}:/data",
            docker.BUSYBOX,
            "sh",
            "-c",
            "umask 002 && touch /data/file && mkdir /data/dir && "
            "stat -c '%n %u:%g %a' /data/file /data/dir",
        ]
    )
    assert write.returncode == 0, write.stderr
    assert write.stdout.split("\n")[:2] == [
        "/data/file 4242:4242 664",
        "/data/dir 4242:4242 2775",  # setgid inherited from /data
    ]


def _inspect(name: str) -> dict:
    result = docker.run(["inspect", name])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)[0]


def test_unless_stopped_restarts_a_crash_but_respects_a_manual_stop(cleanup):
    name = docker.unique_name("restart")
    cleanup.append(("container", name))
    started = docker.run(
        [
            "run",
            "-d",
            "--name",
            name,
            "--restart",
            "unless-stopped",
            docker.BUSYBOX,
            "sh",
            "-c",
            "sleep 1; exit 3",
        ]
    )
    assert started.returncode == 0, started.stderr

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and _inspect(name)["RestartCount"] < 1:
        time.sleep(1)
    assert _inspect(name)["RestartCount"] >= 1, "crashed container was not restarted"

    assert docker.run(["stop", "-t", "1", name]).returncode == 0
    count_after_stop = _inspect(name)["RestartCount"]
    time.sleep(5)
    state = _inspect(name)
    assert state["State"]["Running"] is False
    assert state["RestartCount"] == count_after_stop, "manually stopped container came back"
