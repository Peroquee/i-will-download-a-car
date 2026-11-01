"""scripts/ops/backup.py: backups of the database, config/ and .env, and their restore (#30)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "backup.py"
spec = importlib.util.spec_from_file_location("ytp_backup", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["ytp_backup"] = mod
spec.loader.exec_module(mod)

sys.path.insert(0, str(ROOT / "app"))
from ytp.db import Database  # noqa: E402

SECRET = "OPENVPN_PASSWORD=very-secret-test-value\n"


@pytest.fixture
def repo(tmp_path) -> Path:
    """A stack folder: .env, config/ with the usual subfolders and a real app database."""
    root = tmp_path / "stack"
    (root / "config" / "app").mkdir(parents=True)
    (root / "config" / "gluetun" / "auth").mkdir(parents=True)
    (root / "config" / "qbittorrent" / "qBittorrent" / "logs").mkdir(parents=True)
    (root / "config" / "qbittorrent" / "qBittorrent" / "cache").mkdir(parents=True)
    (root / ".env").write_text(SECRET)
    (root / "config" / "gluetun" / "auth" / "config.toml").write_text("apikey = 'x'\n")
    (root / "config" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf").write_text("[x]\n")
    (root / "config" / "qbittorrent" / "qBittorrent" / "logs" / "qbittorrent.log").write_text("l")
    (root / "config" / "qbittorrent" / "qBittorrent" / "cache" / "blob").write_text("c")
    db = Database.open(root / "config" / "app" / "app.db")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC"
            + "a" * 22
            + "', 'A')"
        )
    db.close()
    return root


def run(repo: Path, *args: str) -> int:
    return mod.main(["--repo-root", str(repo), *args])


def archives(repo: Path) -> list[Path]:
    return sorted((repo / "backups").glob("ytp-backup-*.tar.gz"))


def members(archive: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(archive) as tar:
        return {m.name: m for m in tar.getmembers()}


# --- creating --------------------------------------------------------------------------------


def test_a_backup_has_the_database_config_and_env(repo):
    assert run(repo) == 0
    (archive,) = archives(repo)
    names = members(archive)
    assert {"MANIFEST.json", "db/app.db", "env/dotenv", "config/gluetun/auth/config.toml"} <= set(
        names
    )
    assert "config/qbittorrent/qBittorrent/qBittorrent.conf" in names
    with tarfile.open(archive) as tar:
        assert tar.extractfile("env/dotenv").read().decode() == SECRET


def test_logs_caches_and_the_live_database_files_are_left_out(repo):
    (repo / "config" / "app" / "app.db-wal").write_text("w")
    (repo / "config" / "app" / "app.db-shm").write_text("s")
    run(repo)
    names = set(members(archives(repo)[0]))
    assert not any("logs" in n or "cache" in n for n in names)
    assert not any(n.startswith("config/app/app.db") for n in names), "the online copy is db/app.db"


def test_the_archive_and_the_folder_are_private(repo):
    run(repo)
    (archive,) = archives(repo)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
    for info in members(archive).values():
        assert info.uid == 0
        assert info.uname == ""
        if info.isreg():
            assert info.mode == 0o600, info.name


def test_the_database_copy_is_consistent_while_the_app_is_writing(repo):
    """The online backup, not a file copy: a writer in WAL mode does not break the copy."""
    db_path = repo / "config" / "app" / "app.db"
    stop = threading.Event()
    failures: list[Exception] = []

    def writer():
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        i = 0
        try:
            while not stop.is_set():
                conn.execute(
                    "INSERT INTO jobs (type, external_id, metadata) VALUES ('magnet', ?, '{}')",
                    (f"hash{i}",),
                )
                i += 1
        except Exception as exc:  # pragma: no cover - would fail the test below
            failures.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(3):
            assert run(repo) == 0
    finally:
        stop.set()
        thread.join(10)
    assert not failures
    for archive in archives(repo):
        assert mod.verify_archive(archive) == []


def test_the_manifest_describes_the_backup(repo):
    run(repo)
    with tarfile.open(archives(repo)[0]) as tar:
        manifest = json.load(tar.extractfile("MANIFEST.json"))
    assert manifest["format"] == 1
    assert manifest["database"]["subscriptions"] == 1
    assert manifest["database"]["migrations"][0] == "0001_initial.sql"
    assert len(manifest["database"]["sha256"]) == 64
    assert manifest["config_files"] == 2
    assert manifest["created"].endswith("Z")
    assert SECRET.strip() not in json.dumps(manifest)


def test_every_run_makes_a_new_archive_even_in_the_same_second(repo):
    run(repo)
    run(repo)
    run(repo)
    assert len(archives(repo)) == 3
    assert len({a.name for a in archives(repo)}) == 3


def test_old_archives_beyond_keep_are_removed_and_other_files_are_not(repo):
    target = repo / "backups"
    target.mkdir(mode=0o700)
    for stamp in ("20250101T000000Z", "20250102T000000Z", "20250103T000000Z"):
        (target / f"ytp-backup-{stamp}.tar.gz").write_bytes(b"old")
    (target / "notes.txt").write_text("mine")
    (target / "ytp-backup-latest.tar.gz").write_text("not ours")
    assert run(repo, "--keep", "2") == 0
    names = sorted(p.name for p in target.iterdir())
    assert "notes.txt" in names
    assert "ytp-backup-latest.tar.gz" in names
    ours = [n for n in names if mod.NAME_RE.fullmatch(n)]
    assert len(ours) == 2
    assert "ytp-backup-20250101T000000Z.tar.gz" not in ours
    assert "ytp-backup-20250102T000000Z.tar.gz" not in ours
    assert "ytp-backup-20250103T000000Z.tar.gz" in ours, "the second newest stays"


def test_a_dry_run_writes_nothing_and_says_what_it_would_do(repo, capsys):
    target = repo / "backups"
    target.mkdir(mode=0o700)
    (target / "ytp-backup-20250101T000000Z.tar.gz").write_bytes(b"old")
    assert run(repo, "--dry-run", "--keep", "1") == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "would remove the oldest archive ytp-backup-20250101T000000Z.tar.gz" in out
    assert [p.name for p in target.iterdir()] == ["ytp-backup-20250101T000000Z.tar.gz"]
    assert SECRET.strip() not in out


def test_a_dry_run_does_not_even_create_the_target(repo):
    run(repo, "--dry-run")
    assert not (repo / "backups").exists()


def test_a_custom_target_and_the_environment_variable(repo, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    assert run(repo, "--target", str(other)) == 0
    assert len(list(other.glob("ytp-backup-*"))) == 1
    via_env = tmp_path / "from-env"
    monkeypatch.setenv("YTP_BACKUP_DIR", str(via_env))
    assert run(repo) == 0
    assert len(list(via_env.glob("ytp-backup-*"))) == 1
    assert not (repo / "backups").exists()


def test_symbolic_links_are_neither_followed_nor_archived(repo, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    (repo / "config" / "link-dir").symlink_to(outside)
    (repo / "config" / "link-file").symlink_to(outside / "secret.txt")
    run(repo)
    names = set(members(archives(repo)[0]))
    assert not any("link" in n or "secret.txt" in n for n in names)
    assert "skipped" in capsys.readouterr().out


def test_config_itself_may_be_a_symlink(repo, tmp_path):
    real = tmp_path / "real-config"
    (repo / "config").rename(real)
    (repo / "config").symlink_to(real)
    assert run(repo) == 0
    assert "config/gluetun/auth/config.toml" in members(archives(repo)[0])


# --- refusing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remove", "message"),
    [(".env", ".env does not exist"), ("config/app/app.db", "has not started yet")],
)
def test_missing_pieces_fail_loudly_and_leave_no_archive(repo, capsys, remove, message):
    (repo / remove).unlink()
    assert run(repo) == 2
    assert message in capsys.readouterr().err
    assert archives(repo) == []


def test_a_target_inside_config_is_refused(repo, capsys):
    assert run(repo, "--target", str(repo / "config" / "backups")) == 2
    assert "inside config/" in capsys.readouterr().err


def test_keep_must_be_positive(repo):
    assert run(repo, "--keep", "0") == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
def test_an_unreadable_file_fails_the_backup_and_leaves_no_partial_file(repo, capsys):
    secret = repo / "config" / "gluetun" / "auth" / "config.toml"
    secret.chmod(0)
    try:
        assert run(repo) == 2
    finally:
        secret.chmod(0o600)
    assert "cannot read" in capsys.readouterr().err
    assert archives(repo) == []
    assert list((repo / "backups").glob(".partial-*")) == []
    assert list((repo / "backups").glob(".work-*")) == []


# --- verifying and restoring --------------------------------------------------------------------


def make_archive(repo: Path) -> Path:
    run(repo)
    return archives(repo)[0]


def test_verify_accepts_a_good_archive(repo, capsys):
    archive = make_archive(repo)
    assert run(repo, "verify", str(archive)) == 0
    assert "complete" in capsys.readouterr().out


def test_verify_rejects_a_truncated_archive(repo, capsys):
    archive = make_archive(repo)
    archive.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])
    assert run(repo, "verify", str(archive)) == 1
    assert "PROBLEM" in capsys.readouterr().err


def test_verify_rejects_a_damaged_database(repo, tmp_path):
    archive = make_archive(repo)
    broken = tmp_path / "broken.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(broken, "w:gz") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read() if member.isreg() else None
            if member.name == "db/app.db":
                data = b"this is not a database" * 100
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    assert mod.verify_archive(broken)


def test_restore_unpacks_into_an_empty_folder_with_private_modes(repo, tmp_path):
    archive = make_archive(repo)
    into = tmp_path / "restored"
    assert run(repo, "restore", str(archive), "--into", str(into)) == 0
    assert (into / "env" / "dotenv").read_text() == SECRET
    assert (into / "config" / "gluetun" / "auth" / "config.toml").exists()
    conn = sqlite3.connect(into / "db" / "app.db")
    assert conn.execute("SELECT channel_name FROM subscriptions").fetchall() == [("A",)]
    conn.close()
    for path in into.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(into.stat().st_mode) == 0o700


def test_restore_refuses_a_folder_that_is_not_empty(repo, tmp_path, capsys):
    archive = make_archive(repo)
    into = tmp_path / "busy"
    into.mkdir()
    (into / "keep.txt").write_text("mine")
    assert run(repo, "restore", str(archive), "--into", str(into)) == 2
    assert "empty" in capsys.readouterr().err
    assert [p.name for p in into.iterdir()] == ["keep.txt"]


def test_restore_dry_run_writes_nothing(repo, tmp_path):
    archive = make_archive(repo)
    into = tmp_path / "restored"
    assert run(repo, "restore", str(archive), "--into", str(into), "--dry-run") == 0
    assert not into.exists()


@pytest.mark.security
@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/etc/escape", "config/../../escape", "other/file", "db/../../x"],
)
def test_restore_refuses_members_that_would_leave_the_folder(repo, tmp_path, name):
    archive = make_archive(repo)
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(evil, "w:gz") as dst:
        for member in src.getmembers():
            dst.addfile(member, src.extractfile(member) if member.isreg() else None)
        info = tarfile.TarInfo(name)
        info.size = 1
        dst.addfile(info, io.BytesIO(b"x"))
    into = tmp_path / "restored"
    assert run(repo, "restore", str(evil), "--into", str(into)) == 2
    assert not into.exists()
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.security
def test_restore_refuses_links_and_special_members(repo, tmp_path):
    archive = make_archive(repo)
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(evil, "w:gz") as dst:
        for member in src.getmembers():
            dst.addfile(member, src.extractfile(member) if member.isreg() else None)
        link = tarfile.TarInfo("config/evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        dst.addfile(link)
    into = tmp_path / "restored"
    assert run(repo, "restore", str(evil), "--into", str(into)) == 2
    assert not into.exists()


def test_list_shows_the_archives(repo, capsys):
    make_archive(repo)
    capsys.readouterr()
    assert run(repo, "list") == 0
    assert "ytp-backup-" in capsys.readouterr().out


def test_list_with_no_folder_is_fine(repo, capsys):
    assert run(repo, "list") == 0
    assert "no archives" in capsys.readouterr().out


def test_the_script_is_executable_and_prints_help():
    assert os.access(SCRIPT, os.X_OK)
    with pytest.raises(SystemExit) as exit_info:
        mod.main(["--help"])
    assert exit_info.value.code == 0
