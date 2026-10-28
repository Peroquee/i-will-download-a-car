"""Static checks on docker-compose.yml: structure and the ground rules it must encode."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers.compose import (
    blank_interpolations,
    default_services,
    environment_as_dict,
    load_compose,
    services,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
COMPOSE = load_compose(COMPOSE_PATH)
SERVICE_NAMES = sorted(services(COMPOSE))


def test_compose_file_has_project_name():
    # A fixed project name keeps container/volume names stable regardless of the folder name.
    assert COMPOSE.get("name") == "youtube-premium"


def test_compose_file_has_no_obsolete_version_key():
    assert "version" not in COMPOSE, "the top-level 'version' key is obsolete in Compose v2"


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_every_service_restarts_unless_stopped(name):
    assert services(COMPOSE)[name].get("restart") == "unless-stopped"


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_every_service_has_bounded_logs(name):
    logging = services(COMPOSE)[name].get("logging", {})
    assert logging.get("driver") == "json-file"
    assert logging.get("options", {}).get("max-size"), "logs must be size-capped"
    assert logging.get("options", {}).get("max-file"), "logs must be rotated"


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_no_service_is_privileged(name):
    assert not services(COMPOSE)[name].get("privileged"), f"{name} must not be privileged"


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_image_tag_is_explicit(name):
    image = services(COMPOSE)[name]["image"]
    assert ":" in image.split("/")[-1], f"{name}: image {image!r} has no tag"
    assert not image.endswith(":latest"), f"{name}: hard-coded :latest, use a ${{..._TAG}} variable"


def test_only_built_services_start_by_default():
    # Placeholders for later issues sit behind profiles so `up -d` works today.
    assert set(default_services(COMPOSE)) == {"gluetun", "qbittorrent"}


@pytest.mark.parametrize(
    ("name", "profile"),
    [
        ("app", "planned"),
        ("watchdog", "planned"),
        ("proxy", "planned"),
        ("jellyfin", "jellyfin"),
    ],
)
def test_placeholder_services_are_behind_profiles(name, profile):
    assert services(COMPOSE)[name].get("profiles") == [profile]


@pytest.mark.security
def test_gluetun_can_create_the_tunnel():
    gluetun = services(COMPOSE)["gluetun"]
    assert "NET_ADMIN" in gluetun.get("cap_add", [])
    assert "/dev/net/tun:/dev/net/tun" in gluetun.get("devices", [])


@pytest.mark.security
def test_gluetun_uses_wireguard():
    assert environment_as_dict(services(COMPOSE)["gluetun"])["VPN_TYPE"] == "wireguard"


@pytest.mark.security
def test_gluetun_lets_only_the_lan_bypass_the_tunnel():
    env = environment_as_dict(services(COMPOSE)["gluetun"])
    assert env["FIREWALL_OUTBOUND_SUBNETS"].startswith("${LAN_SUBNET:?")


@pytest.mark.security
def test_vpn_secrets_are_interpolated_never_inline():
    env = environment_as_dict(services(COMPOSE)["gluetun"])
    for key in ("WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES", "VPN_SERVICE_PROVIDER"):
        assert re.fullmatch(r"\$\{[A-Z_]+:\?[^}]+\}", env[key]), f"{key} must be ${{VAR:?msg}}"


@pytest.mark.security
def test_published_ports_bind_an_explicit_host_address():
    # "8080:8080" would publish on 0.0.0.0, i.e. every interface. Require HOST:PORT:PORT.
    for name, svc in services(COMPOSE).items():
        for port in svc.get("ports", []):
            shape = blank_interpolations(port) if isinstance(port, str) else ""
            assert shape.count(":") == 2, (
                f"{name}: port {port!r} must be 'HOST_IP:HOST_PORT:CONTAINER_PORT'"
            )


@pytest.mark.parametrize("name", ["qbittorrent", "app"])
def test_media_writers_get_owner_and_umask(name):
    env = environment_as_dict(services(COMPOSE)[name])
    for key in ("PUID", "PGID", "UMASK", "TZ"):
        assert key in env, f"{name} needs {key}"
        assert env[key].startswith(f"${{{key}:?"), f"{name}: {key} must be required from .env"


def test_media_writers_mount_the_same_data_root():
    # One mount for downloads and media, so hardlinks work (#5, #17).
    for name in ("qbittorrent", "app"):
        volumes = services(COMPOSE)[name]["volumes"]
        assert any(v.endswith(":/data") and v.startswith("${DATA_ROOT:?") for v in volumes), name


def test_jellyfin_reads_media_read_only():
    volumes = services(COMPOSE)["jellyfin"]["volumes"]
    assert any(v.endswith(":/data/media:ro") for v in volumes)
