""".env.example must stay in sync with docker-compose.yml and contain no real secrets."""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.helpers.compose import all_compose_variables, parse_env_file

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = parse_env_file(REPO_ROOT / ".env.example")
REFERENCED = all_compose_variables(REPO_ROOT / "docker-compose.yml")


def test_env_example_parses_cleanly():
    assert ENV_EXAMPLE, ".env.example is empty"


@pytest.mark.parametrize("name", sorted(REFERENCED))
def test_every_compose_variable_is_documented(name):
    assert name in ENV_EXAMPLE, (
        f"{name} is used in docker-compose.yml but missing from .env.example"
    )


@pytest.mark.parametrize("name", sorted(ENV_EXAMPLE))
def test_no_stale_variables_in_env_example(name):
    assert name in REFERENCED, f"{name} is in .env.example but docker-compose.yml never uses it"


@pytest.mark.parametrize("name", sorted(n for n, op in REFERENCED.items() if op and "?" in op))
def test_required_variables_have_example_values(name):
    assert ENV_EXAMPLE.get(name), f"{name} is required (:?) so the example needs a value"


@pytest.mark.security
@pytest.mark.parametrize("name", ["OPENVPN_USER", "OPENVPN_PASSWORD", "WIREGUARD_PRIVATE_KEY"])
def test_secrets_are_placeholders(name):
    assert ENV_EXAMPLE[name] == "changeme"


def test_vpn_defaults_match_expressvpn():
    # ExpressVPN: OpenVPN only, with its own .ovpn because gluetun's built-in key is stale.
    assert ENV_EXAMPLE["VPN_SERVICE_PROVIDER"] == "custom"
    assert ENV_EXAMPLE["VPN_TYPE"] == "openvpn"
    assert ENV_EXAMPLE["OPENVPN_CUSTOM_CONFIG"] == "/gluetun/custom/expressvpn.ovpn"


def test_custom_provider_leaves_the_server_filters_empty():
    assert ENV_EXAMPLE["VPN_SERVER_COUNTRIES"] == ""
    assert ENV_EXAMPLE["VPN_SERVER_CITIES"] == ""


def test_gluetun_tag_is_pinned_to_an_explicit_version():
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ENV_EXAMPLE["GLUETUN_TAG"])


@pytest.mark.security
def test_no_value_looks_like_a_real_wireguard_key():
    # WireGuard keys are 32 bytes base64 = 44 chars ending in '='.
    for name, value in ENV_EXAMPLE.items():
        assert not re.fullmatch(r"[A-Za-z0-9+/]{43}=", value), f"{name} looks like a real key"


@pytest.mark.security
def test_qbittorrent_ui_defaults_to_localhost():
    assert ENV_EXAMPLE["QBIT_WEBUI_BIND"] == "127.0.0.1"


def test_lan_subnet_is_the_owners_lan():
    assert ENV_EXAMPLE["LAN_SUBNET"] == "192.168.178.0/24"


def test_lan_subnet_is_a_private_network():
    network = ipaddress.ip_network(ENV_EXAMPLE["LAN_SUBNET"], strict=True)
    assert network.is_private


@pytest.mark.parametrize("name", ["PUID", "PGID"])
def test_owner_ids_are_numeric_and_not_root(name):
    assert ENV_EXAMPLE[name].isdigit()
    assert int(ENV_EXAMPLE[name]) > 0


def test_umask_is_group_writable_octal():
    umask = ENV_EXAMPLE["UMASK"]
    assert re.fullmatch(r"0?[0-7]{3}", umask)
    assert int(umask, 8) & 0o020 == 0, "UMASK must keep group write so services can share files"


MEDIA_DIRS = (
    "MEDIA_DIR_MOVIES",
    "MEDIA_DIR_SERIES",
    "MEDIA_DIR_AUDIOBOOKS",
    "MEDIA_DIR_MUSIC",
    "MEDIA_DIR_YOUTUBE",
)


@pytest.mark.parametrize("name", MEDIA_DIRS)
def test_media_dirs_are_single_folder_names_relative_to_data_root(name):
    value = ENV_EXAMPLE[name]
    assert value
    assert "/" not in value
    assert value not in {".", ".."}
    assert not value.startswith("-")
    assert value.isprintable()


def test_media_dirs_are_distinct():
    values = [ENV_EXAMPLE[name] for name in MEDIA_DIRS]
    assert len(set(values)) == len(values)


def test_jellyfin_url_is_an_http_address_on_the_lan():
    url = urlparse(ENV_EXAMPLE["JELLYFIN_URL"])
    assert url.scheme == "http"
    assert ipaddress.ip_address(url.hostname).is_private
    assert not url.path.strip("/"), "the base address only, no path"


@pytest.mark.security
def test_jellyfin_api_key_is_never_in_the_example():
    # The owner creates it in Jellyfin and puts it only in .env on the VM.
    assert ENV_EXAMPLE["JELLYFIN_API_KEY"] == ""


def test_data_root_is_absolute():
    assert ENV_EXAMPLE["DATA_ROOT"].startswith("/")


@pytest.mark.security
def test_env_file_is_gitignored():
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env" in lines


@pytest.mark.security
def test_git_really_ignores_env_but_tracks_example():
    if not shutil.which("git") or not (REPO_ROOT / ".git").exists():
        pytest.skip("not inside a git checkout")

    def ignored(path: str) -> bool:
        result = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT, check=False)
        return result.returncode == 0

    assert ignored(".env")
    assert not ignored(".env.example")
