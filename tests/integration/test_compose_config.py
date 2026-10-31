"""Our compose file + .env, as interpreted by the real `docker compose` CLI.

Unit tests read the YAML with PyYAML; these confirm Compose itself agrees:
interpolation, merge keys, profiles and required-variable errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.compose_cli import compose, rendered_config, write_env

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
        "WIREGUARD_PRIVATE_KEY",
        "WIREGUARD_ADDRESSES",
        "VPN_SERVICE_PROVIDER",
        "LAN_SUBNET",
        "PUID",
        "PGID",
        "UMASK",
        "DATA_ROOT",
        "TZ",
        "QBIT_WEBUI_BIND",
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
    env_file = write_env(tmp_path / ".env", WIREGUARD_PRIVATE_KEY="")
    result = compose("config", "--quiet", env_file=env_file)
    assert result.returncode != 0
    assert "WIREGUARD_PRIVATE_KEY must be set" in result.stderr
