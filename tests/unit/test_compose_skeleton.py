"""Static checks on docker-compose.yml: structure and the ground rules it must encode."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers.compose import (
    blank_interpolations,
    default_services,
    environment_as_dict,
    gluetun_environment,
    load_compose,
    services,
    vpn_variant_files,
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


REQUIRED = re.compile(r"\$\{[A-Z_]+:\?[^}]+\}")
# Which secrets belong to which VPN type, and what gluetun's own variables are called.
VPN_SECRETS = {
    "openvpn": ("OPENVPN_USER", "OPENVPN_PASSWORD"),
    "wireguard": ("WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES"),
}


def test_both_vpn_types_have_a_variant_file():
    assert sorted(vpn_variant_files(COMPOSE_PATH)) == ["openvpn", "wireguard"]


def test_gluetun_extends_the_variant_chosen_by_vpn_type_defaulting_to_openvpn():
    # ExpressVPN (our provider) only supports OpenVPN, so that is the default.
    extends = services(COMPOSE)["gluetun"]["extends"]
    assert extends == {"file": "compose/vpn-${VPN_TYPE:-openvpn}.yml", "service": "gluetun"}


@pytest.mark.security
@pytest.mark.parametrize("vpn_type", sorted(VPN_SECRETS))
def test_each_variant_sets_its_type_and_requires_only_its_own_secrets(vpn_type):
    env = environment_as_dict(
        services(load_compose(vpn_variant_files(COMPOSE_PATH)[vpn_type]))["gluetun"]
    )
    assert env["VPN_TYPE"] == vpn_type
    assert set(env) == {"VPN_TYPE", *VPN_SECRETS[vpn_type]}
    for key in VPN_SECRETS[vpn_type]:
        assert REQUIRED.fullmatch(env[key]), f"{key} must be ${{VAR:?msg}}"


@pytest.mark.security
def test_the_base_file_holds_no_vpn_type_secrets():
    # If these were in the base file they would be required for BOTH types.
    env = environment_as_dict(services(COMPOSE)["gluetun"])
    for keys in VPN_SECRETS.values():
        assert not set(keys) & set(env)
    assert "VPN_TYPE" not in env, "VPN_TYPE comes from the variant file"


@pytest.mark.security
def test_gluetun_lets_only_the_lan_bypass_the_tunnel():
    env = gluetun_environment(COMPOSE_PATH, "openvpn")
    assert env["FIREWALL_OUTBOUND_SUBNETS"].startswith("${LAN_SUBNET:?")


@pytest.mark.security
def test_provider_is_required_and_servers_are_optional():
    env = gluetun_environment(COMPOSE_PATH, "openvpn")
    assert REQUIRED.fullmatch(env["VPN_SERVICE_PROVIDER"])
    assert env["SERVER_COUNTRIES"] == "${VPN_SERVER_COUNTRIES:-}"
    assert env["SERVER_CITIES"] == "${VPN_SERVER_CITIES:-}"


@pytest.mark.security
def test_no_port_forwarding_is_configured():
    # ExpressVPN has none. Asking gluetun for it would only produce errors.
    for vpn_type in VPN_SECRETS:
        assert not any(
            k.startswith("VPN_PORT_FORWARDING") for k in gluetun_environment(COMPOSE_PATH, vpn_type)
        )


def test_gluetun_has_its_own_health_check_that_qbittorrent_waits_for():
    gluetun = services(COMPOSE)["gluetun"]
    assert gluetun["healthcheck"]["test"] == ["CMD", "/gluetun-entrypoint", "healthcheck"]
    assert int(gluetun["healthcheck"]["retries"]) >= 1
    qbit = services(COMPOSE)["qbittorrent"]
    assert qbit["depends_on"]["gluetun"]["condition"] == "service_healthy"


@pytest.mark.security
def test_only_gluetun_publishes_ports():
    # qbittorrent shares gluetun's namespace, so its ports are gluetun's.
    published = {n for n, svc in services(COMPOSE).items() if svc.get("ports")}
    assert published == {"gluetun"}


@pytest.mark.security
def test_qbittorrent_web_ui_port_matches_the_published_container_port():
    env = environment_as_dict(services(COMPOSE)["qbittorrent"])
    (port,) = services(COMPOSE)["gluetun"]["ports"]
    assert port.endswith(f":{env['WEBUI_PORT']}")


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
