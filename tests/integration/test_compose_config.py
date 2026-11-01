"""Our compose file + .env, as interpreted by the real `docker compose` CLI.

Unit tests read the YAML with PyYAML; these confirm Compose itself agrees:
interpolation, merge keys, profiles and required-variable errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers.compose_cli import (
    ENV_EXAMPLE,
    REPO_ROOT,
    compose,
    normalized_config,
    rendered_config,
    write_env,
)

pytestmark = pytest.mark.compose


def test_config_is_valid_with_the_example_env():
    result = compose("config", "--quiet")
    assert result.returncode == 0, result.stderr


def test_config_is_valid_with_every_profile():
    result = compose("config", "--quiet", profiles=("*",))
    assert result.returncode == 0, result.stderr


def test_default_services_are_only_the_built_ones():
    result = compose("config", "--services")
    assert sorted(result.stdout.split()) == ["app", "gluetun", "proxy", "qbittorrent"]


def test_planned_profile_adds_the_placeholders():
    result = compose("config", "--services", profiles=("planned",))
    assert "watchdog" in set(result.stdout.split())


def test_every_rendered_service_restarts_unless_stopped():
    config = rendered_config(profiles=("*",))
    policies = {name: svc.get("restart") for name, svc in config["services"].items()}
    assert set(policies.values()) == {"unless-stopped"}, policies


def test_qbittorrent_renders_into_gluetun_namespace():
    qbit = rendered_config()["services"]["qbittorrent"]
    assert qbit["network_mode"] == "service:gluetun"
    assert "networks" not in qbit
    assert "ports" not in qbit
    assert qbit["depends_on"]["gluetun"]["condition"] == "service_healthy"


def test_webui_port_is_published_on_localhost_by_default():
    ports = rendered_config()["services"]["gluetun"]["ports"]
    assert ports == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8080,
            "published": "8080",
            "protocol": "tcp",
        },
    ]


def test_env_values_flow_into_services(tmp_path: Path):
    env_file = write_env(
        tmp_path / ".env", PUID="1234", PGID="5678", UMASK="007", DATA_ROOT="/mnt/media", TZ="UTC"
    )
    qbit = rendered_config(env_file)["services"]["qbittorrent"]
    env = qbit["environment"]
    assert (env["PUID"], env["PGID"], env["UMASK"], env["TZ"]) == ("1234", "5678", "007", "UTC")
    data_mounts = [v for v in qbit["volumes"] if v["target"] == "/data"]
    assert data_mounts
    assert data_mounts[0]["source"] == "/mnt/media"


def test_shell_environment_does_not_leak_into_tests(tmp_path: Path, monkeypatch):
    # Guard for the helper itself: a PUID in CI's environment must not win over the env file.
    monkeypatch.setenv("PUID", "999")
    env_file = write_env(tmp_path / ".env", PUID="1234")
    assert rendered_config(env_file)["services"]["qbittorrent"]["environment"]["PUID"] == "1234"


@pytest.mark.security
@pytest.mark.parametrize(
    "missing",
    [
        "OPENVPN_USER",
        "OPENVPN_PASSWORD",
        "VPN_SERVICE_PROVIDER",
        "GLUETUN_TAG",
        "LAN_SUBNET",
        "PUID",
        "PGID",
        "UMASK",
        "DATA_ROOT",
        "TZ",
        "QBIT_WEBUI_BIND",
        "MEDIA_DIR_MOVIES",
        "MEDIA_DIR_YOUTUBE",
        "PROXY_BIND",
        "PROXY_PORT",
        "PROXY_QBT_PORT",
        "PROXY_USER",
        "PROXY_PASSWORD_HASH",
        "CADDY_TAG",
    ],
)
def test_missing_required_variable_stops_compose(tmp_path: Path, missing):
    env_file = write_env(tmp_path / ".env", **{missing: None})
    result = compose("config", "--quiet", env_file=env_file)
    assert result.returncode != 0
    assert f"{missing} must be set" in result.stderr


@pytest.mark.security
def test_empty_secret_stops_compose(tmp_path: Path):
    # ${VAR:?} also rejects an empty value, not only a missing one.
    env_file = write_env(tmp_path / ".env", OPENVPN_PASSWORD="")
    result = compose("config", "--quiet", env_file=env_file)
    assert result.returncode != 0
    assert "OPENVPN_PASSWORD must be set" in result.stderr


def test_the_actual_rendered_config_is_written_for_review():
    """Writes reports/c0-compose-config.actual.json (a CI artifact) from the dummy example env.

    Not a check on the config: it lets a reviewer, or whoever updates the regression
    snapshot, copy exactly what Compose rendered instead of writing it by hand.
    """
    actual = normalized_config(rendered_config(profiles=("*",)))
    out = REPO_ROOT / "reports" / "c0-compose-config.actual.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --- VPN type: only the active type's secrets are required (#2) ---

VPN_ENV = {
    "openvpn": {"OPENVPN_USER", "OPENVPN_PASSWORD"},
    "wireguard": {"WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES"},
}


def gluetun_env(env_file: Path) -> dict[str, str]:
    return rendered_config(env_file)["services"]["gluetun"]["environment"]


def test_openvpn_is_the_default_and_renders_only_openvpn_settings():
    env = gluetun_env(ENV_EXAMPLE)
    assert env["VPN_TYPE"] == "openvpn"
    assert env["VPN_SERVICE_PROVIDER"] == "custom"
    assert env["OPENVPN_CUSTOM_CONFIG"] == "/gluetun/custom/expressvpn.ovpn"
    assert VPN_ENV["openvpn"] <= set(env)
    assert not VPN_ENV["wireguard"] & set(env)


def test_vpn_type_unset_means_openvpn(tmp_path: Path):
    env_file = write_env(tmp_path / ".env", VPN_TYPE=None)
    assert gluetun_env(env_file)["VPN_TYPE"] == "openvpn"


def test_wireguard_renders_only_wireguard_settings(tmp_path: Path):
    env_file = write_env(tmp_path / ".env", VPN_TYPE="wireguard")
    env = gluetun_env(env_file)
    assert env["VPN_TYPE"] == "wireguard"
    assert VPN_ENV["wireguard"] <= set(env)
    assert not VPN_ENV["openvpn"] & set(env)


@pytest.mark.security
@pytest.mark.parametrize(
    ("vpn_type", "missing"),
    [
        ("openvpn", "OPENVPN_USER"),
        ("openvpn", "OPENVPN_PASSWORD"),
        ("wireguard", "WIREGUARD_PRIVATE_KEY"),
        ("wireguard", "WIREGUARD_ADDRESSES"),
    ],
)
def test_a_missing_secret_of_the_active_vpn_type_stops_compose(tmp_path: Path, vpn_type, missing):
    env_file = write_env(tmp_path / ".env", VPN_TYPE=vpn_type, **{missing: None})
    result = compose("config", "--quiet", env_file=env_file)
    assert result.returncode != 0
    assert f"{missing} must be set" in result.stderr


@pytest.mark.parametrize(
    ("vpn_type", "inactive"),
    [("openvpn", VPN_ENV["wireguard"]), ("wireguard", VPN_ENV["openvpn"])],
)
def test_the_inactive_vpn_types_variables_are_not_required(tmp_path: Path, vpn_type, inactive):
    env_file = write_env(tmp_path / ".env", VPN_TYPE=vpn_type, **dict.fromkeys(inactive))
    result = compose("config", "--quiet", env_file=env_file)
    assert result.returncode == 0, result.stderr


@pytest.mark.security
def test_an_unknown_vpn_type_stops_compose(tmp_path: Path):
    env_file = write_env(tmp_path / ".env", VPN_TYPE="lightway")
    result = compose("config", "--quiet", env_file=env_file)
    assert result.returncode != 0


def test_gluetun_renders_its_health_check_and_the_lan_bypass():
    gluetun = rendered_config()["services"]["gluetun"]
    assert gluetun["healthcheck"]["test"] == ["CMD", "/gluetun-entrypoint", "healthcheck"]
    assert gluetun["environment"]["FIREWALL_OUTBOUND_SUBNETS"] == "192.168.178.0/24"


def test_library_folder_names_reach_the_app_with_their_umlauts(tmp_path: Path):
    env_file = write_env(
        tmp_path / ".env", MEDIA_DIR_AUDIOBOOKS="Hörbücher", MEDIA_DIR_MOVIES="Filme"
    )
    app = rendered_config(env_file)["services"]["app"]["environment"]
    assert app["MEDIA_DIR_AUDIOBOOKS"] == "Hörbücher"
    assert app["MEDIA_DIR_MOVIES"] == "Filme"


def test_jellyfin_settings_reach_the_app(tmp_path: Path):
    env_file = write_env(
        tmp_path / ".env", JELLYFIN_URL="http://192.168.178.108:8096", JELLYFIN_API_KEY="k3y"
    )
    app = rendered_config(env_file)["services"]["app"]["environment"]
    assert app["JELLYFIN_URL"] == "http://192.168.178.108:8096"
    assert app["JELLYFIN_API_KEY"] == "k3y"


@pytest.mark.parametrize("missing", ["JELLYFIN_URL", "JELLYFIN_API_KEY"])
def test_the_jellyfin_settings_are_optional_in_compose(tmp_path: Path, missing):
    """The key is created by the owner later (docs/jellyfin.md): an .env
    without them must not stop `docker compose` (the app checks them when it runs)."""
    env_file = write_env(tmp_path / ".env", **{missing: None})
    result = compose("config", "--quiet", env_file=env_file, profiles=("*",))
    assert result.returncode == 0, result.stderr


def test_there_is_no_jellyfin_service():
    config = rendered_config(profiles=("*",))
    assert "jellyfin" not in config["services"]


# --- reverse proxy (#29) ---


def test_proxy_ports_are_published_on_the_configured_address(tmp_path: Path):
    env_file = write_env(
        tmp_path / ".env", PROXY_BIND="192.168.178.50", PROXY_PORT="8088", PROXY_QBT_PORT="8089"
    )
    ports = rendered_config(env_file)["services"]["proxy"]["ports"]
    shape = {(p["host_ip"], p["published"], p["target"]) for p in ports}
    assert shape == {("192.168.178.50", "8088", 8080), ("192.168.178.50", "8089", 8081)}


@pytest.mark.security
def test_the_app_publishes_no_port_and_only_the_proxy_and_gluetun_do():
    config = rendered_config(profiles=("*",))["services"]
    assert "ports" not in config["app"]
    assert {n for n, svc in config.items() if svc.get("ports")} == {"gluetun", "proxy"}


@pytest.mark.security
def test_the_proxy_gets_the_lan_subnet_and_the_login_from_env(tmp_path: Path):
    env_file = write_env(
        tmp_path / ".env", LAN_SUBNET="10.1.2.0/24", PROXY_USER="mario", PROXY_PASSWORD_HASH="x"
    )
    env = rendered_config(env_file)["services"]["proxy"]["environment"]
    assert (env["LAN_SUBNET"], env["PROXY_USER"], env["PROXY_PASSWORD_HASH"]) == (
        "10.1.2.0/24",
        "mario",
        "x",
    )


@pytest.mark.security
def test_the_proxy_renders_read_only_without_capabilities_except_the_one_it_needs():
    proxy = rendered_config()["services"]["proxy"]
    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]
    assert proxy["cap_add"] == ["NET_BIND_SERVICE"]
    assert proxy["depends_on"]["app"]["condition"] == "service_healthy"


@pytest.mark.security
@pytest.mark.parametrize("missing", ["PROXY_USER", "PROXY_PASSWORD_HASH"])
def test_an_empty_login_stops_compose(tmp_path: Path, missing):
    env_file = write_env(tmp_path / ".env", **{missing: ""})
    result = compose("config", "--quiet", env_file=env_file)
    assert result.returncode != 0
    assert f"{missing} must be set" in result.stderr


def test_a_single_quoted_hash_reaches_the_proxy_literally(tmp_path: Path):
    """The hash contains "$": make proxy-password writes it single-quoted so Compose keeps it."""
    hashed = "$2a$14$" + "a" * 22 + "B" * 31
    env_file = tmp_path / ".env"
    write_env(env_file, PROXY_PASSWORD_HASH="x")
    lines = [
        ln for ln in env_file.read_text().splitlines() if not ln.startswith("PROXY_PASSWORD_HASH")
    ]
    env_file.write_text("\n".join([*lines, f"PROXY_PASSWORD_HASH='{hashed}'"]) + "\n")
    env = rendered_config(env_file)["services"]["proxy"]["environment"]
    assert env["PROXY_PASSWORD_HASH"] == hashed
