"""The watchdog service in docker-compose.yml: separate, minimal, read-only, no way in."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.helpers.compose import environment_as_dict, load_compose, parse_env_file, services

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = load_compose(REPO_ROOT / "docker-compose.yml")
WATCHDOG = services(COMPOSE)["watchdog"]
ENV_EXAMPLE = parse_env_file(REPO_ROOT / ".env.example")


def test_it_is_part_of_the_default_stack_and_a_container_of_its_own():
    assert not WATCHDOG.get("profiles")
    assert "network_mode" not in WATCHDOG, "on the default bridge network, not gluetun's namespace"
    assert WATCHDOG["command"] == ["python", "-m", "ytp.watchdog"]


def test_it_uses_the_apps_image_so_it_needs_no_second_build():
    assert WATCHDOG["image"] == services(COMPOSE)["app"]["image"]
    assert WATCHDOG["build"] == services(COMPOSE)["app"]["build"]


@pytest.mark.security
def test_it_has_no_ports_no_volumes_no_capabilities_and_a_read_only_root():
    assert not WATCHDOG.get("ports")
    assert not WATCHDOG.get("volumes")
    assert WATCHDOG["read_only"] is True
    assert WATCHDOG["cap_drop"] == ["ALL"]
    assert not WATCHDOG.get("cap_add")
    assert "no-new-privileges:true" in WATCHDOG["security_opt"]
    assert not WATCHDOG.get("privileged")
    assert WATCHDOG["user"].startswith("${PUID:?")


@pytest.mark.security
def test_it_is_not_given_the_database_the_media_or_the_jellyfin_key():
    env = environment_as_dict(WATCHDOG)
    assert not [k for k in env if k.startswith(("MEDIA_DIR", "JELLYFIN", "DATA_ROOT"))]
    assert WATCHDOG.get("volumes") in (None, [])


def test_it_does_not_depend_on_the_app_or_on_gluetun_being_healthy():
    """If the app is wedged or gluetun unhealthy it must still run."""
    assert "depends_on" not in WATCHDOG


def test_it_talks_to_gluetun_over_the_bridge():
    env = environment_as_dict(WATCHDOG)
    assert env["GLUETUN_CONTROL_URL"] == "http://gluetun:8000"
    assert env["QBIT_URL"] == "http://gluetun:8080"


def test_the_key_is_required_the_password_is_checked_by_the_watchdog_itself():
    env = environment_as_dict(WATCHDOG)
    assert env["GLUETUN_CONTROL_APIKEY"].startswith("${GLUETUN_CONTROL_APIKEY:?")
    assert env["QBIT_PASSWORD"] == "${QBIT_PASSWORD:-}"  # empty: it exits 2 and says why


@pytest.mark.security
def test_the_example_holds_only_a_placeholder_key_and_safe_defaults():
    assert ENV_EXAMPLE["GLUETUN_CONTROL_APIKEY"] == "changeme"
    assert float(ENV_EXAMPLE["WATCHDOG_INTERVAL"]) <= 5
    assert 1 <= int(ENV_EXAMPLE["WATCHDOG_THRESHOLD"]) <= 10
    assert float(ENV_EXAMPLE["WATCHDOG_RESUME_AFTER"]) == 0, "no automatic resume by default"


def test_the_health_check_runs_the_modules_check():
    test = WATCHDOG["healthcheck"]["test"]
    assert test == ["CMD", "python", "-m", "ytp.watchdog", "--check"]


@pytest.mark.security
def test_gluetuns_auth_file_lives_where_the_existing_mount_exposes_it():
    """config/gluetun is mounted at /gluetun, and gluetun reads /gluetun/auth/config.toml."""
    assert "./config/gluetun:/gluetun" in services(COMPOSE)["gluetun"]["volumes"]
    script = (REPO_ROOT / "scripts/watchdog/setup_key.py").read_text()
    assert '"config" / "gluetun" / "auth" / "config.toml"' in script


@pytest.mark.security
def test_the_gluetun_control_server_is_not_published():
    ports = " ".join(map(str, services(COMPOSE)["gluetun"]["ports"]))
    assert "8000" not in ports


def test_the_watchdog_module_imports_only_what_it_needs():
    """It must not pull in the web framework or the database: it has to survive them."""
    tree = ast.parse((REPO_ROOT / "app/ytp/watchdog.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(("." * node.level + node.module).split(".")[0] or node.module)
    assert imported.isdisjoint({"fastapi", "uvicorn", "sqlite3", "pydantic", "apscheduler"})
    local = {
        m
        for m in re.findall(
            r"^from (\.[\w.]*) import", (REPO_ROOT / "app/ytp/watchdog.py").read_text(), re.M
        )
    }
    assert local == {
        ".alerts",
        ".clients.gluetun",
        ".clients.qbittorrent",
        ".clients.watchdog",
        ".egress",
        ".logging_setup",
    }


@pytest.mark.security
def test_alerts_are_off_by_default_and_no_public_service_is_preset():
    """Sending anything out of the LAN is the owner's decision (#22)."""
    assert ENV_EXAMPLE["ALERT_URL"] == ""
    assert ENV_EXAMPLE["ALERT_TOKEN"] == ""
    assert ENV_EXAMPLE["ALERT_FORMAT"] == "ntfy"
    for value in ENV_EXAMPLE.values():
        assert "ntfy.sh" not in value
    env = environment_as_dict(WATCHDOG)
    assert env["ALERT_URL"] == "${ALERT_URL:-}"
    assert env["ALERT_TOKEN"] == "${ALERT_TOKEN:-}"
    assert "ntfy.sh" not in (REPO_ROOT / "docker-compose.yml").read_text()


def test_the_status_page_is_for_the_bridge_only():
    assert environment_as_dict(WATCHDOG)["WATCHDOG_STATUS_PORT"] == "8090"
    assert not WATCHDOG.get("ports")
    app_env = environment_as_dict(services(COMPOSE)["app"])
    assert app_env["WATCHDOG_STATUS_URL"] == "http://watchdog:8090"


@pytest.mark.security
def test_only_the_watchdog_gets_the_alert_target():
    for name, svc in services(COMPOSE).items():
        if name != "watchdog":
            assert not [k for k in environment_as_dict(svc) if k.startswith("ALERT_")], name


@pytest.mark.security
def test_the_egress_check_settings_reach_only_the_watchdog_and_the_home_address_is_not_shipped():
    env = environment_as_dict(WATCHDOG)
    assert env["EGRESS_CHECK_INTERVAL"] == "${EGRESS_CHECK_INTERVAL:-300}"
    assert env["HOME_PUBLIC_IP"] == "${HOME_PUBLIC_IP:-}"
    assert ENV_EXAMPLE["HOME_PUBLIC_IP"] == "", "the owner's own line is never in Git"
    assert int(ENV_EXAMPLE["EGRESS_CHECK_INTERVAL"]) >= 30
    assert int(ENV_EXAMPLE["EGRESS_CONFIRM"]) >= 2
    for name, svc in services(COMPOSE).items():
        if name != "watchdog":
            assert "HOME_PUBLIC_IP" not in environment_as_dict(svc), name
