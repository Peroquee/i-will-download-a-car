"""The reverse proxy (#29): compose service, Caddyfile and .env.example, checked statically.

CI has no Docker daemon, so nothing here starts Caddy. These tests pin the decisions that make
the proxy safe (LAN check first, then login, nothing trusted from headers, nothing published
besides the proxy) so a refactor cannot loosen them unnoticed. tests/system/test_c10_proxy.py
runs the real Caddyfile in a real Caddy on a host with Docker.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest

from tests.helpers.compose import (
    environment_as_dict,
    load_compose,
    parse_env_file,
    services,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = load_compose(REPO_ROOT / "docker-compose.yml")
PROXY = services(COMPOSE)["proxy"]
ENV_EXAMPLE = parse_env_file(REPO_ROOT / ".env.example")
CADDYFILE = (REPO_ROOT / "proxy" / "Caddyfile").read_text(encoding="utf-8")


def caddy_directives(text: str) -> str:
    """The Caddyfile without comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


CODE = caddy_directives(CADDYFILE)


def site_block(address: str) -> str:
    """The text of one top-level site block, by its address line."""
    match = re.search(rf"^{re.escape(address)} \{{\n(.*?)^\}}", CODE, re.S | re.M)
    assert match, f"no site block {address}"
    return match.group(1)


# --- compose ---------------------------------------------------------------------------


def test_the_proxy_starts_with_the_default_stack():
    assert not PROXY.get("profiles")


def test_the_proxy_image_is_pinned_through_caddy_tag():
    assert PROXY["image"] == "caddy:${CADDY_TAG:?CADDY_TAG must be set in .env}"
    assert re.fullmatch(r"\d+\.\d+\.\d+", ENV_EXAMPLE["CADDY_TAG"]), "pin an exact Caddy version"


@pytest.mark.security
def test_the_proxy_does_not_run_as_root_and_is_locked_down():
    assert PROXY["user"] == "${PUID:?PUID must be set in .env}:${PGID:?PGID must be set in .env}"
    assert PROXY["read_only"] is True
    assert PROXY["cap_drop"] == ["ALL"]
    # The official image's binary carries this file capability; without it in the bounding
    # set exec fails. It listens on ports above 1024, so it is not used to bind anything.
    assert PROXY["cap_add"] == ["NET_BIND_SERVICE"]
    assert "no-new-privileges:true" in PROXY["security_opt"]
    assert not PROXY.get("privileged")


@pytest.mark.security
def test_the_proxy_keeps_nothing_on_disk_and_mounts_only_its_config_read_only():
    assert PROXY["volumes"] == ["./proxy:/etc/caddy:ro"]
    assert sorted(PROXY["tmpfs"]) == ["/config", "/data", "/tmp"]


@pytest.mark.security
def test_the_proxy_never_gets_the_docker_socket_or_the_media():
    text = "\n".join(map(str, PROXY.get("volumes", [])))
    assert "docker.sock" not in text
    assert "/data" not in text.replace("/etc/caddy", "")


@pytest.mark.security
def test_the_login_is_required_not_optional():
    env = environment_as_dict(PROXY)
    for name in ("PROXY_USER", "PROXY_PASSWORD_HASH", "LAN_SUBNET"):
        assert env[name].startswith(f"${{{name}:?"), f"{name} must stop compose when missing"


@pytest.mark.security
def test_published_ports_use_the_configured_address_and_the_two_container_ports():
    ports = PROXY["ports"]
    assert len(ports) == 2
    assert ports[0].startswith("${PROXY_BIND:?")
    assert ports[0].endswith(":8080")
    assert ports[1].startswith("${PROXY_BIND}:${PROXY_QBT_PORT:?")
    assert ports[1].endswith(":8081")


@pytest.mark.security
def test_only_the_proxy_and_gluetun_publish_ports():
    published = {name for name, svc in services(COMPOSE).items() if svc.get("ports")}
    assert published == {"gluetun", "proxy"}
    assert not services(COMPOSE)["app"].get("ports")
    assert not services(COMPOSE)["qbittorrent"].get("ports")
    assert not services(COMPOSE)["app"].get("expose")


def test_the_proxy_waits_for_a_healthy_app_and_has_a_health_check():
    assert PROXY["depends_on"]["app"]["condition"] == "service_healthy"
    assert PROXY["healthcheck"]["test"][0] == "CMD"
    assert PROXY["healthcheck"]["test"][-1] == "http://127.0.0.1:8082/health"


def test_the_proxy_is_not_in_gluetuns_network_and_does_not_share_it():
    assert "network_mode" not in PROXY, "it reaches gluetun/app over the bridge"


# --- .env.example ---------------------------------------------------------------------------


def test_proxy_address_is_a_private_address_inside_the_lan():
    address = ipaddress.ip_address(ENV_EXAMPLE["PROXY_BIND"])
    assert address.version == 4
    assert address in ipaddress.ip_network(ENV_EXAMPLE["LAN_SUBNET"])
    assert not address.is_unspecified, "0.0.0.0 would listen on every interface"


def test_proxy_ports_are_valid_and_distinct_from_the_backends():
    ports = [int(ENV_EXAMPLE[name]) for name in ("PROXY_PORT", "PROXY_QBT_PORT")]
    assert all(0 < port < 65536 for port in ports)
    assert len(set(ports)) == 2
    assert not {8000, int(ENV_EXAMPLE["QBIT_WEBUI_PORT"])} & set(ports)


@pytest.mark.security
def test_the_example_holds_no_real_password_hash():
    value = ENV_EXAMPLE["PROXY_PASSWORD_HASH"]
    assert value == "changeme"
    assert not value.startswith("$2"), "never commit a real bcrypt hash"


def test_the_example_user_is_a_safe_login_name():
    assert re.fullmatch(r"[A-Za-z0-9._-]{3,64}", ENV_EXAMPLE["PROXY_USER"])


@pytest.mark.security
def test_qbittorrent_still_binds_localhost_behind_the_proxy():
    assert ENV_EXAMPLE["QBIT_WEBUI_BIND"] == "127.0.0.1"


# --- Caddyfile ---------------------------------------------------------------------------


@pytest.mark.security
def test_the_lan_check_uses_the_connections_own_address_not_a_header():
    assert "remote_ip {$LAN_SUBNET}" in CODE
    # client_ip honours X-Forwarded-For when trusted_proxies is set; trusted_proxies must stay
    # unset so the header is never believed, and nothing here may switch it on.
    for word in ("client_ip", "trusted_proxies", "X-Forwarded-For", "X-Real-IP"):
        assert word not in CODE, word
    # the only header rewriting is the removal of the proxy's own login (see the next test)
    assert set(re.findall(r"header_up.*", CODE)) == {"header_up -Authorization"}


@pytest.mark.security
def test_the_lan_check_comes_first_and_answers_403():
    lan = re.search(r"^\(lan_only\) \{\n(.*?)^\}", CODE, re.S | re.M)
    assert lan
    body = lan.group(1)
    assert body.index("@outside not remote_ip") < body.index("respond @outside")
    assert "403" in body
    assert "basic_auth" not in body


@pytest.mark.security
def test_the_qbittorrent_guard_checks_the_lan_before_the_login():
    guard = re.search(r"^\(guard\) \{\n(.*?)^\}", CODE, re.S | re.M)
    assert guard
    body = guard.group(1)
    assert body.index("import lan_only") < body.index("basic_auth")
    assert "{$PROXY_USER} {$PROXY_PASSWORD_HASH}" in body


@pytest.mark.security
@pytest.mark.parametrize(
    ("address", "guard", "upstream"),
    [
        # #44: the app has its own accounts, so Caddy only checks the LAN there;
        # the qBittorrent port keeps the proxy's basic auth (admin only)
        (":8080", "lan_only", "app:8000"),
        (":8081", "guard", "gluetun:8080"),
    ],
)
def test_every_public_listener_applies_its_guard_before_proxying(address, guard, upstream):
    block = site_block(address)
    route = re.search(r"route \{\n(.*)\n\t\}", block, re.S)
    assert route, "the guard and the upstream must sit in one `route` so their order is kept"
    inner = route.group(1)
    assert inner.index(f"import {guard}") < inner.index(f"reverse_proxy {upstream}")
    assert inner.count("reverse_proxy") == 1
    assert inner.count("import ") == 1


@pytest.mark.security
def test_only_the_qbittorrent_port_asks_for_basic_auth():
    assert "basic_auth" not in site_block(":8080")
    assert "basic_auth" not in site_block("http://127.0.0.1:8082")
    assert "basic_auth" in CODE


def test_the_caddyfile_listens_on_exactly_the_container_ports_compose_publishes():
    published = {int(p.rsplit(":", 1)[1]) for p in PROXY["ports"]}
    listeners = {int(m) for m in re.findall(r"^:(\d+) \{", CODE, re.M)}
    assert listeners == published == {8080, 8081}


@pytest.mark.security
def test_the_only_other_listener_is_the_loopback_health_check():
    addresses = re.findall(r"^(\S+) \{$", CODE, re.M)
    others = [
        a for a in addresses if not re.fullmatch(r":\d+", a) and a not in ("(guard)", "(lan_only)")
    ]
    assert others == ["http://127.0.0.1:8082"]
    block = site_block("http://127.0.0.1:8082")
    assert "bind 127.0.0.1" in block
    assert "reverse_proxy" not in block


@pytest.mark.security
def test_no_admin_api_and_no_certificates():
    global_block = re.search(r"^\{\n(.*?)^\}", CODE, re.S | re.M)
    assert global_block
    assert "admin off" in global_block.group(1)
    assert "auto_https off" in global_block.group(1)


@pytest.mark.security
def test_the_caddyfile_holds_no_credentials_only_placeholders():
    assert not re.search(r"\$2[abxy]\$", CADDYFILE), "a bcrypt hash must not be in Git"
    assert "changeme" not in CADDYFILE


def test_the_qbittorrent_upstream_is_the_service_that_publishes_its_web_ui():
    # qbittorrent has no network of its own; the Web UI answers on gluetun's name and port 8080
    # (WEBUI_PORT=8080 is fixed in docker-compose.yml).
    assert environment_as_dict(services(COMPOSE)["qbittorrent"])["WEBUI_PORT"] == "8080"
    assert services(COMPOSE)["qbittorrent"]["network_mode"] == "service:gluetun"


def test_the_app_upstream_is_the_apps_port():
    dockerfile = (REPO_ROOT / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert "8000" in dockerfile


@pytest.mark.security
@pytest.mark.parametrize(
    ("address", "upstream"), [(":8080", "app:8000"), (":8081", "gluetun:8080")]
)
def test_the_proxy_login_is_never_forwarded_to_the_upstream(address, upstream):
    """The browser's basic-auth header is the proxy's login. qBittorrent 5.3 even answers 401 to
    any request with an Authorization header, and no backend has a use for the proxy password."""
    block = site_block(address)
    proxy = re.search(rf"reverse_proxy {re.escape(upstream)} \{{\n(.*?)\n\t\t\}}", block, re.S)
    assert proxy, "the reverse_proxy needs a block"
    assert re.findall(r"header_up.*", proxy.group(1)) == ["header_up -Authorization"]
