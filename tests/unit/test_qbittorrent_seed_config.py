"""scripts/qbittorrent/seed_config.py: the Web UI login and VPN binding it writes."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qbittorrent" / "seed_config.py"
PASSWORD = "correct horse battery"  # a test value, not a secret

spec = importlib.util.spec_from_file_location("seed_config", SCRIPT)
assert spec is not None
assert spec.loader is not None
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


def run(config: Path, *args: str, stdin: str = PASSWORD + "\n") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--config", str(config), "--password-stdin", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def stored_hash(text: str) -> str:
    match = re.search(r'^WebUI\\Password_PBKDF2="@ByteArray\((.+)\)"$', text, re.MULTILINE)
    assert match, text
    return match.group(1)


def verifies(stored: str, password: str) -> bool:
    """Check a stored value the way qBittorrent does: PBKDF2-HMAC-SHA512, 100000 rounds."""
    salt_b64, key_b64 = stored.split(":")
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), base64.b64decode(salt_b64), 100_000, 64)
    return key == base64.b64decode(key_b64)


def test_hash_format_matches_what_qbittorrent_stores():
    value = seed.pbkdf2_value(PASSWORD, salt=bytes(range(16)))
    assert value.startswith("@ByteArray(")
    assert value.endswith(")")
    salt_b64, key_b64 = value.removeprefix("@ByteArray(").removesuffix(")").split(":")
    assert base64.b64decode(salt_b64) == bytes(range(16))
    assert len(base64.b64decode(key_b64)) == 64


def test_every_hash_has_a_fresh_salt():
    assert seed.pbkdf2_value(PASSWORD) != seed.pbkdf2_value(PASSWORD)


@pytest.mark.security
def test_a_new_config_gets_login_port_and_vpn_binding(tmp_path: Path):
    config = tmp_path / "qBittorrent" / "qBittorrent.conf"
    result = run(config, "--user", "owner")
    assert result.returncode == 0, result.stderr
    text = config.read_text()
    assert "[LegalNotice]\nAccepted=true" in text
    assert "WebUI\\Username=owner" in text
    assert verifies(stored_hash(text), PASSWORD)
    assert not verifies(stored_hash(text), "wrong password")
    assert "WebUI\\LocalHostAuth=true" in text
    assert "WebUI\\AuthSubnetWhitelistEnabled=false" in text
    assert "WebUI\\Port=8080" in text
    assert "Session\\Interface=tun0" in text
    assert "Session\\InterfaceName=tun0" in text


@pytest.mark.security
def test_the_password_never_reaches_the_file_or_the_output(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    result = run(config)
    assert PASSWORD not in config.read_text()
    assert PASSWORD not in result.stdout + result.stderr


@pytest.mark.security
def test_the_config_is_private(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    run(config)
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_existing_settings_are_kept_and_ours_are_replaced(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    config.write_text(
        "[BitTorrent]\n"
        "Session\\DefaultSavePath=/data/downloads\n"
        "Session\\Interface=eth0\n"
        "Session\\Port=6881\n"
        "\n"
        "[Preferences]\n"
        "Downloads\\SavePath=/data/downloads/\n"
        "WebUI\\Username=old\n"
        'WebUI\\Password_PBKDF2="@ByteArray(old:old)"\n'
        "\n"
        "[RSS]\n"
        "AutoDownloader\\Enabled=false\n"
    )
    assert run(config).returncode == 0
    text = config.read_text()
    for kept in (
        "Session\\DefaultSavePath=/data/downloads",
        "Session\\Port=6881",
        "Downloads\\SavePath=/data/downloads/",
        "[RSS]\nAutoDownloader\\Enabled=false",
    ):
        assert kept in text
    assert "eth0" not in text
    assert "WebUI\\Username=old" not in text
    assert "old:old" not in text
    assert text.count("Session\\Interface=") == 1
    assert text.count("WebUI\\Username=") == 1


def test_running_twice_is_stable(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    run(config)
    first = config.read_text()
    run(config, "--password-stdin")
    second = config.read_text()

    def without_hash(text: str) -> str:
        return re.sub(r"Password_PBKDF2=.*", "", text)

    assert without_hash(first) == without_hash(second)
    assert stored_hash(first) != stored_hash(second)  # a new salt each time
    assert second.count("[Preferences]") == 1


def test_dry_run_writes_nothing_and_shows_no_hash(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    result = run(config, "--dry-run")
    assert result.returncode == 0
    assert not config.exists()
    assert "<hash>" in result.stdout
    assert "ByteArray" not in result.stdout


@pytest.mark.parametrize("password", ["", "short", "x" * 9])
def test_a_short_password_is_refused(tmp_path: Path, password):
    config = tmp_path / "qBittorrent.conf"
    result = run(config, stdin=password + "\n")
    assert result.returncode != 0
    assert "at least" in result.stderr
    assert not config.exists()


@pytest.mark.parametrize("user", ["", "ab", "has space", "semi;colon", "new\nline", "x" * 65])
def test_an_unsafe_user_name_is_refused(tmp_path: Path, user):
    config = tmp_path / "qBittorrent.conf"
    result = run(config, "--user", user)
    assert result.returncode != 0
    assert not config.exists()


def test_the_default_config_path_is_where_the_container_reads_it():
    # docker-compose.yml mounts ./config/qbittorrent at /config; the linuxserver image
    # keeps qBittorrent's profile in /config/qBittorrent/.
    relative = seed.DEFAULT_CONFIG.relative_to(seed.REPO_ROOT)
    assert relative.as_posix() == "config/qbittorrent/qBittorrent/qBittorrent.conf"


def test_the_web_ui_port_defaults_to_8080_and_can_be_chosen_for_tests(tmp_path):
    """Only the system tests choose one (a free port, published under the same number)."""
    default, chosen = tmp_path / "a.conf", tmp_path / "b.conf"
    assert run(default).returncode == 0
    assert run(chosen, "--port", "18123").returncode == 0
    assert "WebUI\\Port=8080" in default.read_text()
    assert "WebUI\\Port=18123" in chosen.read_text()


@pytest.mark.parametrize("port", ["0", "65536", "abc", "-1"])
def test_a_bad_port_is_refused(tmp_path, port):
    result = run(tmp_path / "c.conf", "--port", port)
    assert result.returncode == 2
    assert not (tmp_path / "c.conf").exists()


# --- the proxy on :8081 (qBittorrent answered 401 to a Host header with another port) -----------


def test_a_new_config_turns_host_header_validation_off_and_keeps_csrf_protection_on(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    assert run(config).returncode == 0
    text = config.read_text()
    assert "WebUI\\HostHeaderValidation=false" in text
    assert "WebUI\\CSRFProtection=true" in text
    assert "WebUI\\CSRFProtection=false" not in text
    assert text.count("WebUI\\HostHeaderValidation=") == 1


def test_an_old_csrf_off_or_validation_on_line_is_overwritten(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    config.write_text(
        "[Preferences]\nWebUI\\CSRFProtection=false\nWebUI\\HostHeaderValidation=true\n"
    )
    assert run(config).returncode == 0
    text = config.read_text()
    assert text.count("WebUI\\CSRFProtection=") == 1
    assert "WebUI\\CSRFProtection=true" in text
    assert "WebUI\\HostHeaderValidation=false" in text
    assert "HostHeaderValidation=true" not in text


OLD_CONFIG = (
    "[BitTorrent]\nSession\\Interface=tun0\nSession\\Port=6881\n\n"
    "[Preferences]\nWebUI\\Username=owner\n"
    'WebUI\\Password_PBKDF2="@ByteArray(salt:key)"\n'
    "WebUI\\LocalHostAuth=true\n\n[RSS]\nAutoDownloader\\Enabled=false\n"
)


def fix(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--config", str(config), "--fix-host-header", *args],
        capture_output=True,
        text=True,
        check=False,
        input="",
    )


def test_fix_host_header_patches_an_existing_config_and_touches_nothing_else(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    config.write_text(OLD_CONFIG)
    result = fix(config)
    assert result.returncode == 0, result.stderr
    text = config.read_text()
    assert "WebUI\\HostHeaderValidation=false" in text
    assert "WebUI\\CSRFProtection=true" in text
    for kept in (
        'WebUI\\Password_PBKDF2="@ByteArray(salt:key)"',
        "WebUI\\Username=owner",
        "Session\\Port=6881",
        "[RSS]\nAutoDownloader\\Enabled=false",
    ):
        assert kept in text
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_fix_host_header_asks_for_no_password_and_prints_none(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    config.write_text(OLD_CONFIG)
    result = fix(config)  # stdin is empty: a password prompt would fail
    assert result.returncode == 0
    assert "password" not in (result.stdout + result.stderr).lower()


def test_fix_host_header_twice_changes_nothing_the_second_time(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    config.write_text(OLD_CONFIG)
    fix(config)
    first = config.read_text()
    mtime = config.stat().st_mtime_ns
    again = fix(config)
    assert again.returncode == 0
    assert "nothing to do" in again.stdout
    assert config.read_text() == first
    assert config.stat().st_mtime_ns == mtime, "not even rewritten"
    assert first.count("WebUI\\HostHeaderValidation=") == 1


def test_fix_host_header_dry_run_writes_nothing(tmp_path: Path):
    config = tmp_path / "qBittorrent.conf"
    config.write_text(OLD_CONFIG)
    result = fix(config, "--dry-run")
    assert result.returncode == 0
    assert "would set" in result.stdout
    assert config.read_text() == OLD_CONFIG


def test_fix_host_header_without_a_config_says_so_and_creates_nothing(tmp_path: Path):
    config = tmp_path / "missing" / "qBittorrent.conf"
    result = fix(config)
    assert result.returncode == 1
    assert "does not exist" in result.stdout
    assert not config.parent.exists()


def test_the_check_for_an_already_patched_config_is_exact():
    assert seed.host_header_fixed(OLD_CONFIG) is False
    patched = seed.apply_settings(OLD_CONFIG, {"Preferences": seed.PROXY_SETTINGS})
    assert seed.host_header_fixed(patched) is True
    assert (
        seed.host_header_fixed(
            patched.replace("HostHeaderValidation=false", "HostHeaderValidation=true")
        )
        is False
    )
    assert (
        seed.host_header_fixed(patched.replace("CSRFProtection=true", "CSRFProtection=false"))
        is False
    )
