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
    assert sorted(result.stdout.split()) == ["gluetun", "qbittorrent"]


def test_planned_profile_adds_the_placeholders():
    result = compose("config", "--services", profiles=("planned",))
    assert {"app", "watchdog", "proxy"} <= set(result.stdout.split())


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
    app = rendered_config(env_file, profiles=("planned",))["services"]["app"]["environment"]
    assert app["MEDIA_DIR_AUDIOBOOKS"] == "Hörbücher"
    assert app["MEDIA_DIR_MOVIES"] == "Filme"


def test_jellyfin_settings_reach_the_app(tmp_path: Path):
    env_file = write_env(
        tmp_path / ".env", JELLYFIN_URL="http://192.168.178.108:8096", JELLYFIN_API_KEY="k3y"
    )
    app = rendered_config(env_file, profiles=("planned",))["services"]["app"]["environment"]
    assert app["JELLYFIN_URL"] == "http://192.168.178.108:8096"
    assert app["JELLYFIN_API_KEY"] == "k3y"


@pytest.mark.parametrize("missing", ["JELLYFIN_URL", "JELLYFIN_API_KEY"])
def test_the_jellyfin_settings_are_optional_in_compose(tmp_path: Path, missing):
    """The app is still a placeholder and the key is created by the owner later: an .env
    without them must not stop `docker compose` (the app checks them when it runs)."""
    env_file = write_env(tmp_path / ".env", **{missing: None})
    result = compose("config", "--quiet", env_file=env_file, profiles=("*",))
    assert result.returncode == 0, result.stderr


def test_there_is_no_jellyfin_service():
    config = rendered_config(profiles=("*",))
    assert "jellyfin" not in config["services"]
