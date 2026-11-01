"""scripts/proxy/set_password.py: a bcrypt hash from Caddy goes into .env, the password nowhere."""

from __future__ import annotations

import importlib.util
import io
import os
import stat
from pathlib import Path

import pytest

from tests.helpers.shell import StubBin

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "proxy" / "set_password.py"
spec = importlib.util.spec_from_file_location("set_password", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PASSWORD = "correct horse battery"  # a test value, not a secret
FAKE_HASH = "$2a$14$" + "abcdefghijklmnopqrstuv" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ01234"
assert mod.BCRYPT_PATTERN.fullmatch(FAKE_HASH)

ENV_TEXT = """# comment
TZ=Europe/Berlin
PROXY_USER=admin
PROXY_PASSWORD_HASH=changeme
CADDY_TAG=2.10.2
"""


@pytest.fixture
def docker(tmp_path, monkeypatch) -> StubBin:
    """A fake `docker` that records its arguments and stdin and prints a bcrypt-shaped hash."""
    stub = StubBin(tmp_path / "bin")
    stub.path.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    script = [
        f"printf '%s\\n' \"$@\" > {record}/args",
        f"cat > {record}/stdin",
        f"echo '{FAKE_HASH}'",
    ]
    stub.add("docker", "\n".join(script))
    monkeypatch.setenv("PATH", f"{stub.path}{os.pathsep}{os.environ['PATH']}")
    return stub


def record(tmp_path: Path, name: str) -> str:
    return (tmp_path / "record" / name).read_text()


@pytest.fixture
def env_file(tmp_path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    path.chmod(0o640)
    return path


def run(env_file: Path, monkeypatch, *args: str, password: str = PASSWORD) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    return mod.main(["--env-file", str(env_file), "--password-stdin", *args])


def test_the_hash_replaces_the_placeholder_and_other_lines_stay(
    docker, env_file, monkeypatch, tmp_path, capsys
):
    assert run(env_file, monkeypatch) == 0
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "# comment",
        "TZ=Europe/Berlin",
        "PROXY_USER=admin",
        f"PROXY_PASSWORD_HASH='{FAKE_HASH}'",
        "CADDY_TAG=2.10.2",
    ]
    assert FAKE_HASH not in capsys.readouterr().out


@pytest.mark.security
def test_the_password_goes_through_stdin_never_the_command_line(
    docker, env_file, monkeypatch, tmp_path, capsys
):
    run(env_file, monkeypatch)
    assert record(tmp_path, "stdin").strip() == PASSWORD
    assert PASSWORD not in record(tmp_path, "args")
    assert PASSWORD not in env_file.read_text(encoding="utf-8")
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err


def test_the_hash_is_made_in_the_pinned_caddy_image(docker, env_file, monkeypatch, tmp_path):
    run(env_file, monkeypatch)
    args = record(tmp_path, "args").split()
    assert args == ["run", "--rm", "-i", "caddy:2.10.2", "caddy", "hash-password"]


def test_the_hash_is_quoted_because_it_contains_dollar_signs(docker, env_file, monkeypatch):
    run(env_file, monkeypatch)
    assert f"PROXY_PASSWORD_HASH='{FAKE_HASH}'" in env_file.read_text(encoding="utf-8")


def test_the_file_mode_is_kept(docker, env_file, monkeypatch):
    run(env_file, monkeypatch)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640
    assert not env_file.with_name(".env.tmp").exists()


def test_user_option_sets_the_user(docker, env_file, monkeypatch):
    run(env_file, monkeypatch, "--user", "mario")
    assert "PROXY_USER=mario" in env_file.read_text(encoding="utf-8").splitlines()


def test_the_current_user_is_kept_when_none_is_given(docker, env_file, monkeypatch):
    env_file.write_text(ENV_TEXT.replace("PROXY_USER=admin", "PROXY_USER=owner"))
    run(env_file, monkeypatch)
    assert "PROXY_USER=owner" in env_file.read_text(encoding="utf-8").splitlines()


def test_missing_keys_are_appended(docker, tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("CADDY_TAG=2.10.2\n")
    run(path, monkeypatch)
    assert path.read_text().splitlines() == [
        "CADDY_TAG=2.10.2",
        "PROXY_USER=admin",
        f"PROXY_PASSWORD_HASH='{FAKE_HASH}'",
    ]


def test_running_it_twice_does_not_duplicate_lines(docker, env_file, monkeypatch):
    run(env_file, monkeypatch)
    once = env_file.read_text(encoding="utf-8")
    run(env_file, monkeypatch)
    assert env_file.read_text(encoding="utf-8").count("PROXY_PASSWORD_HASH=") == 1
    assert env_file.read_text(encoding="utf-8").splitlines()[:3] == once.splitlines()[:3]


def test_duplicate_lines_are_collapsed_to_one(docker, env_file, monkeypatch):
    env_file.write_text(ENV_TEXT + "PROXY_PASSWORD_HASH=old\n")
    run(env_file, monkeypatch)
    assert env_file.read_text(encoding="utf-8").count("PROXY_PASSWORD_HASH=") == 1


def test_dry_run_changes_nothing_and_asks_for_nothing(docker, env_file, tmp_path, capsys):
    assert mod.main(["--env-file", str(env_file), "--dry-run"]) == 0
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT
    assert not (tmp_path / "record" / "args").exists()
    assert "dry run" in capsys.readouterr().out


@pytest.mark.parametrize("password", ["short", "x" * (mod.MIN_PASSWORD_LENGTH - 1), ""])
def test_a_short_password_is_refused(docker, env_file, monkeypatch, password):
    with pytest.raises(SystemExit) as error:
        run(env_file, monkeypatch, password=password)
    assert "at least" in str(error.value)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT


@pytest.mark.parametrize("user", ["ab", "has space", "semi;colon", "{brace}", "x" * 65, "ü" * 4])
def test_an_unsafe_user_name_is_refused(docker, env_file, monkeypatch, user):
    with pytest.raises(SystemExit) as error:
        run(env_file, monkeypatch, "--user", user)
    assert "user name" in str(error.value)


def test_a_missing_env_file_is_explained(docker, tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as error:
        run(tmp_path / "nope", monkeypatch)
    assert "not found" in str(error.value)


def test_a_failing_caddy_leaves_the_file_alone(tmp_path, monkeypatch, env_file):
    stub = StubBin(tmp_path / "bin")
    stub.path.mkdir()
    stub.add("docker", 'cat >/dev/null; echo "pull access denied" >&2; exit 1')
    monkeypatch.setenv("PATH", f"{stub.path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(SystemExit) as error:
        run(env_file, monkeypatch)
    assert "pull access denied" in str(error.value)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT


def test_something_that_is_not_a_bcrypt_hash_is_refused(tmp_path, monkeypatch, env_file):
    stub = StubBin(tmp_path / "bin")
    stub.path.mkdir()
    stub.add("docker", "cat >/dev/null; echo not-a-hash")
    monkeypatch.setenv("PATH", f"{stub.path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(SystemExit) as error:
        run(env_file, monkeypatch)
    assert "bcrypt" in str(error.value)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT


def test_a_missing_docker_is_explained(tmp_path, monkeypatch, env_file):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(SystemExit) as error:
        run(env_file, monkeypatch)
    assert "docker" in str(error.value)


def test_a_missing_caddy_tag_is_explained(docker, tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("TZ=UTC\n")
    with pytest.raises(SystemExit) as error:
        run(path, monkeypatch)
    assert "CADDY_TAG" in str(error.value)
