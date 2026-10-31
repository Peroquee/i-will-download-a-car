"""Regression guards for issues #2 and #3 (C1): VPN and qBittorrent decisions.

Same rule as for C0: a failing test here means a deliberate decision is being
changed, which needs its own issue and a conscious update, not a quick fix.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.helpers.compose import gluetun_environment, parse_env_file
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT

COMPOSE = REPO_ROOT / "docker-compose.yml"
SEED_SCRIPT = REPO_ROOT / "scripts" / "qbittorrent" / "seed_config.py"


@pytest.mark.security
def test_c1_expressvpn_means_openvpn():
    """Decision: the provider is ExpressVPN, which gluetun supports over OpenVPN only
    (no WireGuard, no Lightway). The example env must therefore default to OpenVPN."""
    env = parse_env_file(ENV_EXAMPLE)
    assert env["VPN_SERVICE_PROVIDER"] == "expressvpn"
    assert env["VPN_TYPE"] == "openvpn"


def test_c1_no_port_forwarding():
    """Decision: ExpressVPN has no port forwarding, so none is configured (fewer incoming
    peers is accepted). Gluetun would only log errors for VPN_PORT_FORWARDING."""
    for vpn_type in ("openvpn", "wireguard"):
        env = gluetun_environment(COMPOSE, vpn_type)
        assert not [k for k in env if "PORT_FORWARDING" in k]


@pytest.mark.security
def test_c1_the_lan_is_the_owners_192_168_178_network():
    """Decision: FIREWALL_OUTBOUND_SUBNETS defaults to the owner's LAN, not 192.168.1.0/24."""
    assert parse_env_file(ENV_EXAMPLE)["LAN_SUBNET"] == "192.168.178.0/24"


@pytest.mark.security
def test_c1_qbittorrent_is_bound_to_the_tunnel_interface():
    """Decision: besides sharing gluetun's namespace, qBittorrent binds to tun0, so with
    the tunnel down it has nothing to connect through."""
    spec = importlib.util.spec_from_file_location("seed_config", SEED_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    bit_torrent = seed.wanted_settings("admin", "x" * 12)["BitTorrent"]
    assert bit_torrent == {"Session\\Interface": "tun0", "Session\\InterfaceName": "tun0"}


@pytest.mark.security
def test_c1_no_qbittorrent_password_in_git():
    """Decision: the Web UI password never lives in a tracked file or in .env; only its
    PBKDF2 hash is written, on the host, by scripts/qbittorrent/seed_config.py."""
    assert not [k for k in parse_env_file(ENV_EXAMPLE) if "QBIT" in k and "PASS" in k]
    tracked = [p for p in Path(REPO_ROOT).rglob("qBittorrent.conf") if ".git" not in p.parts]
    assert tracked == [], "qBittorrent.conf holds the hash and belongs in config/ (gitignored)"
