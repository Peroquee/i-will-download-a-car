"""The killswitch holds and qbittorrent recovers on the real stack (issue #4).

Runs scripts/vpn/killswitch-test.sh against a throwaway copy of the stack, so the
production containers are never stopped. Needs real ExpressVPN credentials and the
downloaded config/expressvpn.ovpn, so it runs only with YTP_VPN_ENV_FILE set (see
test_c1_tunnel.py). The script is also the manual procedure: `make killswitch-test`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers.compose_cli import REPO_ROOT
from tests.helpers.shell import run_script
from tests.helpers.stack import Stack, running_stack

ENV_FILE = os.environ.get("YTP_VPN_ENV_FILE")
SCRIPT = REPO_ROOT / "scripts" / "vpn" / "killswitch-test.sh"

pytestmark = [
    pytest.mark.docker,
    pytest.mark.tun,
    pytest.mark.network,
    pytest.mark.slow,
    pytest.mark.security,
    pytest.mark.skipif(not ENV_FILE, reason="set YTP_VPN_ENV_FILE to a real .env to run this"),
]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    with running_stack(Path(ENV_FILE or ""), tmp_path_factory.mktemp("ytp-killswitch")) as running:
        yield running


@pytest.fixture(scope="module")
def killswitch_run(stack):
    """One run of the script (it takes minutes); the tests below look at its result."""
    return run_script(SCRIPT, env={"KS_COMPOSE": shlex.join(stack.compose_prefix())}, timeout=900)


def test_the_killswitch_holds_and_qbittorrent_recovers_by_itself(killswitch_run):
    assert killswitch_run.returncode == 0, killswitch_run.stdout + killswitch_run.stderr
    assert "HOME IP LEAK" not in killswitch_run.stdout
    assert "OK: the killswitch holds and qbittorrent recovers" in killswitch_run.stdout


def test_the_script_output_never_contains_the_home_address(killswitch_run):
    home = subprocess.run(
        ["curl", "-s", "--max-time", "10", "https://api.ipify.org"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert home, "the host has no internet"
    assert home not in killswitch_run.stdout + killswitch_run.stderr
