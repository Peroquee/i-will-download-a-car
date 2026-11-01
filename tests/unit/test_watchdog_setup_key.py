"""scripts/watchdog/setup_key.py: one key, in .env and in gluetun's auth file, read-only role."""

from __future__ import annotations

import importlib.util
import stat
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "watchdog" / "setup_key.py"
spec = importlib.util.spec_from_file_location("setup_key", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ENV = "TZ=Europe/Berlin\nGLUETUN_CONTROL_APIKEY=changeme\nWATCHDOG_INTERVAL=1\n"


@pytest.fixture
def files(tmp_path) -> tuple[Path, Path]:
    env = tmp_path / ".env"
    env.write_text(ENV)
    env.chmod(0o640)
    return env, tmp_path / "config" / "gluetun" / "auth" / "config.toml"


def run(files, *args: str) -> int:
    env, auth = files
    return mod.main(["--env-file", str(env), "--auth-file", str(auth), *args])


def key_in_env(env: Path) -> str:
    return mod.env_value(env.read_text(), "GLUETUN_CONTROL_APIKEY")


def test_creates_a_key_in_both_places_and_they_match(files):
    env, auth = files
    assert run(files) == 0
    key = key_in_env(env)
    assert key != "changeme"
    assert len(key) >= 20
    role = tomllib.loads(auth.read_text())["roles"][0]
    assert role["apikey"] == key
    assert role["auth"] == "apikey"


@pytest.mark.security
def test_the_role_may_read_the_vpn_status_and_nothing_else(files):
    run(files)
    (role,) = tomllib.loads(files[1].read_text())["roles"]
    assert role["routes"] == ["GET /v1/vpn/status"]


def test_modes_the_env_keeps_and_the_auth_file_is_private(files):
    env, auth = files
    run(files)
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
    assert not auth.with_name("config.toml.tmp").exists()


def test_other_env_lines_survive(files):
    env, _ = files
    run(files)
    lines = env.read_text().splitlines()
    assert lines[0] == "TZ=Europe/Berlin"
    assert lines[-1] == "WATCHDOG_INTERVAL=1"


def test_running_again_keeps_the_key_and_repairs_the_auth_file(files):
    env, auth = files
    run(files)
    key = key_in_env(env)
    auth.unlink()
    run(files)
    assert key_in_env(env) == key
    assert tomllib.loads(auth.read_text())["roles"][0]["apikey"] == key


def test_rotate_makes_a_new_key_everywhere(files):
    env, auth = files
    run(files)
    first = key_in_env(env)
    run(files, "--rotate")
    second = key_in_env(env)
    assert second != first
    assert tomllib.loads(auth.read_text())["roles"][0]["apikey"] == second


def test_a_missing_key_line_is_appended(files):
    env, _ = files
    env.write_text("TZ=UTC\n")
    run(files)
    assert env.read_text().count("GLUETUN_CONTROL_APIKEY=") == 1


def test_a_hand_typed_short_key_is_replaced(files):
    env, _ = files
    env.write_text("GLUETUN_CONTROL_APIKEY=abc\n")
    run(files)
    assert len(key_in_env(env)) >= 20


@pytest.mark.security
def test_the_key_is_never_printed(files, capsys):
    run(files)
    out = capsys.readouterr()
    assert key_in_env(files[0]) not in out.out + out.err


def test_dry_run_writes_nothing(files, capsys):
    env, auth = files
    assert run(files, "--dry-run") == 0
    assert env.read_text() == ENV
    assert not auth.exists()
    assert "dry run" in capsys.readouterr().out


def test_a_missing_env_file_is_explained(tmp_path):
    with pytest.raises(SystemExit) as error:
        mod.main(["--env-file", str(tmp_path / "nope"), "--auth-file", str(tmp_path / "a.toml")])
    assert "not found" in str(error.value)
