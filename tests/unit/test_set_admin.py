"""scripts/app/set_admin.py: a scrypt hash goes into .env, the password nowhere (#44)."""

from __future__ import annotations

import importlib.util
import io
import os
import stat
from pathlib import Path

import pytest

from ytp import passwords

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "app" / "set_admin.py"
spec = importlib.util.spec_from_file_location("set_admin", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PASSWORD = "correct horse battery"  # a test value, not a secret
ENV_TEXT = """# comment
TZ=Europe/Berlin
APP_ADMIN_USER=admin
APP_ADMIN_PASSWORD_HASH=changeme
PROXY_USER=james
"""


@pytest.fixture
def env_file(tmp_path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    path.chmod(0o640)
    return path


def run(env_file: Path, monkeypatch, *args: str, stdin: str = PASSWORD + "\n") -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return mod.main(["--env-file", str(env_file), "--password-stdin", *args])


def values(env_file: Path) -> dict[str, str]:
    lines = env_file.read_text(encoding="utf-8").splitlines()
    return dict(ln.split("=", 1) for ln in lines if "=" in ln and not ln.startswith("#"))


def test_it_writes_a_hash_that_verifies_and_keeps_the_rest(env_file, monkeypatch):
    assert run(env_file, monkeypatch) == 0
    written = values(env_file)
    assert passwords.is_hash(written["APP_ADMIN_PASSWORD_HASH"])
    assert passwords.verify_password(PASSWORD, written["APP_ADMIN_PASSWORD_HASH"])
    assert written["APP_ADMIN_USER"] == "admin"
    assert written["TZ"] == "Europe/Berlin"
    assert written["PROXY_USER"] == "james"
    assert "# comment" in env_file.read_text(encoding="utf-8")


def test_the_hash_has_no_dollar_sign_compose_would_expand(env_file, monkeypatch):
    run(env_file, monkeypatch)
    assert "$" not in values(env_file)["APP_ADMIN_PASSWORD_HASH"]


def test_the_password_is_never_written_or_printed(env_file, monkeypatch, capsys):
    run(env_file, monkeypatch)
    out = capsys.readouterr()
    assert PASSWORD not in env_file.read_text(encoding="utf-8")
    assert PASSWORD not in out.out + out.err


def test_the_user_can_be_chosen_and_is_kept_on_a_re_run(env_file, monkeypatch):
    run(env_file, monkeypatch, "--user", "hausherr")
    assert values(env_file)["APP_ADMIN_USER"] == "hausherr"
    first = values(env_file)["APP_ADMIN_PASSWORD_HASH"]
    run(env_file, monkeypatch)
    again = values(env_file)
    assert again["APP_ADMIN_USER"] == "hausherr"
    assert again["APP_ADMIN_PASSWORD_HASH"] != first, "a new salt every time"
    assert env_file.read_text(encoding="utf-8").count("APP_ADMIN_USER=") == 1


def test_the_file_mode_is_kept(env_file, monkeypatch):
    run(env_file, monkeypatch)
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o640


def test_a_dry_run_writes_nothing(env_file, monkeypatch, capsys):
    assert run(env_file, monkeypatch, "--dry-run") == 0
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT
    assert "dry run" in capsys.readouterr().out


@pytest.mark.parametrize("password", ["short", "", "x" * 201])
def test_a_weak_or_overlong_password_is_refused(env_file, monkeypatch, password):
    with pytest.raises(SystemExit) as error:
        run(env_file, monkeypatch, stdin=password + "\n")
    assert "password" in str(error.value)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT


@pytest.mark.parametrize("name", ["ab", "-bad", "has space", "a" * 65, "x;y"])
def test_a_bad_user_name_is_refused(env_file, monkeypatch, name):
    with pytest.raises(SystemExit):
        run(env_file, monkeypatch, "--user", name)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT


def test_a_missing_env_file_is_explained(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as error:
        run(tmp_path / "nope", monkeypatch)
    assert "not found" in str(error.value)


def test_the_two_terminal_entries_must_match(env_file, monkeypatch):
    answers = iter([PASSWORD, PASSWORD + "x"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(answers))
    with pytest.raises(SystemExit) as error:
        mod.main(["--env-file", str(env_file)])
    assert "differ" in str(error.value)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT
