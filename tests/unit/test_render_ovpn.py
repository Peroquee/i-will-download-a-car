"""scripts/vpn/render_ovpn.py: ExpressVPN's .ovpn made acceptable for gluetun's custom provider."""

from __future__ import annotations

import importlib.util
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vpn" / "render_ovpn.py"
spec = importlib.util.spec_from_file_location("render_ovpn", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# A fake ExpressVPN file; the "key" is a test marker that must never show up in output.
SECRET = "TESTKEYMATERIAL-DO-NOT-PRINT"  # a test marker, not a real key
ORIGINAL = f"""client
dev tun
proto udp
remote germany-frankfurt-1-ca-version-2.expressnetw.com 1195
remote-random
resolv-retry infinite
auth-user-pass
<cert>
{SECRET}
</cert>
"""


def fake_resolver(mapping: dict[str, str]):
    def resolve(host: str) -> str:
        if host not in mapping:
            raise mod.RenderError(f"cannot resolve {host}")
        return mapping[host]

    return resolve


def test_the_host_name_becomes_an_ip_and_remote_random_goes():
    text, report = mod.render(
        ORIGINAL, fake_resolver({"germany-frankfurt-1-ca-version-2.expressnetw.com": "203.0.113.7"})
    )
    assert "remote 203.0.113.7 1195\n" in text
    assert "expressnetw.com" not in text
    assert "remote-random" not in text
    assert report == "remote germany-frankfurt-1-ca-version-2.expressnetw.com -> 203.0.113.7"


def test_everything_else_is_copied_unchanged():
    text, _ = mod.render(
        ORIGINAL, fake_resolver({"germany-frankfurt-1-ca-version-2.expressnetw.com": "203.0.113.7"})
    )
    for kept in ("client", "dev tun", "proto udp", "resolv-retry infinite", "auth-user-pass"):
        assert kept in text.splitlines()
    assert f"<cert>\n{SECRET}\n</cert>" in text


def test_the_protocol_word_after_the_port_is_kept():
    text, _ = mod.render(
        "remote vpn.example 443 tcp\n", fake_resolver({"vpn.example": "198.51.100.1"})
    )
    assert text == "remote 198.51.100.1 443 tcp\n"


def test_only_the_first_remote_is_kept():
    original = "remote a.example 1195\nremote b.example 1195\nremote-random\n"
    text, _ = mod.render(
        original, fake_resolver({"a.example": "192.0.2.1", "b.example": "192.0.2.2"})
    )
    assert text == "remote 192.0.2.1 1195\n"


def test_a_remote_that_is_already_an_ip_is_kept_without_dns():
    def no_dns(host: str) -> str:
        raise AssertionError("DNS must not be asked")

    text, report = mod.render("remote 192.0.2.9 1195\n", no_dns)
    assert text == "remote 192.0.2.9 1195\n"
    assert "already an IP" in report


@pytest.mark.parametrize("original", ["client\ndev tun\n", "remote\n"])
def test_a_file_without_a_usable_remote_is_refused(original):
    with pytest.raises(mod.RenderError):
        mod.render(original, fake_resolver({}))


def test_an_unresolvable_host_is_refused():
    with pytest.raises(mod.RenderError, match="cannot resolve"):
        mod.render("remote nope.example 1195\n", fake_resolver({}))


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "expressvpn.ovpn"
    # localhost resolves without network access, so the real DNS path is exercised too.
    path.write_text(
        ORIGINAL.replace("germany-frankfurt-1-ca-version-2.expressnetw.com", "localhost")
    )
    return path


@pytest.mark.security
def test_cli_writes_a_private_file_and_never_prints_its_contents(source: Path, tmp_path: Path):
    output = tmp_path / "gluetun" / "custom" / "expressvpn.ovpn"
    result = run("--source", str(source), "--output", str(output))
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "remote 127.0.0.1 1195" in output.read_text()
    assert SECRET in output.read_text()
    assert SECRET not in result.stdout + result.stderr
    assert "localhost -> 127.0.0.1" in result.stdout


def test_cli_dry_run_writes_nothing(source: Path, tmp_path: Path):
    output = tmp_path / "out" / "expressvpn.ovpn"
    result = run("--source", str(source), "--output", str(output), "--dry-run")
    assert result.returncode == 0
    assert not output.exists()
    assert "dry run" in result.stdout


def test_cli_is_safe_to_rerun(source: Path, tmp_path: Path):
    output = tmp_path / "expressvpn.ovpn"
    run("--source", str(source), "--output", str(output))
    first = output.read_text()
    assert run("--source", str(source), "--output", str(output)).returncode == 0
    assert output.read_text() == first
    assert not (tmp_path / "expressvpn.ovpn.tmp").exists()


def test_cli_explains_a_missing_source(tmp_path: Path):
    result = run("--source", str(tmp_path / "missing.ovpn"), "--output", str(tmp_path / "o.ovpn"))
    assert result.returncode == 1
    assert "Manual configuration" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_reports_an_unresolvable_host_without_a_traceback(tmp_path: Path):
    source = tmp_path / "in.ovpn"
    source.write_text("remote does-not-exist.invalid 1195\n")
    result = run("--source", str(source), "--output", str(tmp_path / "o.ovpn"))
    assert result.returncode == 1
    assert "cannot resolve" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_default_paths_match_the_compose_mount_and_the_docs():
    assert mod.DEFAULT_SOURCE.relative_to(mod.REPO_ROOT).as_posix() == "config/expressvpn.ovpn"
    relative = mod.DEFAULT_OUTPUT.relative_to(mod.REPO_ROOT).as_posix()
    assert relative == "config/gluetun/custom/expressvpn.ovpn"


def test_resolve_ipv4_returns_an_ipv4_address():
    assert socket.inet_aton(mod.resolve_ipv4("localhost"))
