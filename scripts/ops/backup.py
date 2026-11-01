#!/usr/bin/env python3
"""backup.py: back up what cannot be rebuilt from Git, and restore it (issue #30).

What is lost if the media VM dies, and cannot be re-created from the repository:

* the app's database (`config/app/app.db`: jobs, subscriptions), copied with SQLite's ONLINE
  backup, so it is consistent while the app runs (a plain file copy of a WAL database is not);
* `config/`: gluetun's auth file and custom .ovpn, qBittorrent's configuration and resume data,
  the proxy and watchdog state (logs and caches left out);
* `.env`: the VPN login, the proxy password hash, the API keys.

NOT included: the downloads and the media libraries (`/data`). They are large and live on the
share, which has its own protection; this backup is the small, secret-bearing part.

The archive therefore CONTAINS SECRETS. It is created with mode 600 in a folder with mode 700,
the default folder `backups/` is ignored by Git, and it must never be committed or put on a
share other people can read. Encrypting it (for example with `age` or `gpg`) before it leaves
this machine is the owner's call; see docs/operations.md.

Usage:
    scripts/ops/backup.py [create] [--target DIR] [--keep N] [--dry-run]
    scripts/ops/backup.py list [--target DIR]
    scripts/ops/backup.py verify ARCHIVE
    scripts/ops/backup.py restore ARCHIVE --into DIR

`create` is the default. It is safe to re-run: every run makes a new, complete, time-stamped
archive (written under a temporary name and renamed when finished and verified), then removes the
oldest archives beyond `--keep` (default 14); it never touches other files in the folder.
`--dry-run` shows what it would do and writes nothing. Run it as the user who owns `config/`
(PUID:PGID of the stack), or with sudo. If a file cannot be read the backup FAILS instead of
producing an incomplete archive.

`restore` unpacks an archive into an EMPTY folder (it never writes into the live installation)
after checking every member's name and type, and checks the database. Putting the files back is
a deliberate step, described in docs/operations.md.

Test hook: `--repo-root` points the script at another tree.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEEP = 14
NAME_RE = re.compile(r"ytp-backup-\d{8}T\d{6}Z(?:-\d+)?\.tar\.gz")
MANIFEST = "MANIFEST.json"
TOP_LEVEL = ("db", "config", "env")  # the only folders an archive may contain, besides MANIFEST
# Relative to config/: things that are noise or rebuilt by the programs.
EXCLUDED_PARTS = (
    ("app", "app.db"),
    ("app", "app.db-wal"),
    ("app", "app.db-shm"),
    ("app", "app.db-journal"),
)
EXCLUDED_DIR_NAMES = {"logs", "cache", "__pycache__"}


class BackupError(Exception):
    """Something the user has to fix; the message says what. Exit code 2."""


# ------------------------------------------------------------------------------------ helpers


def fmt_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def is_excluded(relative: Path) -> bool:
    """Left out of the backup: the live database files (the online copy replaces them) and the
    log and cache folders."""
    return relative.parts in EXCLUDED_PARTS or any(
        part in EXCLUDED_DIR_NAMES for part in relative.parts
    )


def collect_config(config: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(directories, files, skipped) below config/, relative to it.

    Symbolic links, sockets and the like are skipped (never followed: a link must not make the
    backup read, or a restore write, something outside the folder). `skipped` lists them."""
    dirs: list[Path] = []
    files: list[Path] = []
    skipped: list[Path] = []
    for current, dirnames, filenames in os.walk(config, followlinks=False):
        base = Path(current)
        relative_base = base.relative_to(config)
        keep_dirs = []
        for name in sorted(dirnames):
            relative = relative_base / name
            if is_excluded(relative):
                continue
            if (base / name).is_symlink():
                skipped.append(relative)
                continue
            dirs.append(relative)
            keep_dirs.append(name)
        dirnames[:] = keep_dirs
        for name in sorted(filenames):
            relative = relative_base / name
            if is_excluded(relative):
                continue
            mode = (base / name).lstat().st_mode
            if stat.S_ISREG(mode):
                files.append(relative)
            else:
                skipped.append(relative)
    return dirs, files, skipped


def backup_database(db: Path, into: Path) -> dict[str, object]:
    """Copy the live database with SQLite's online backup API and check the copy."""
    try:
        source = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise BackupError(
            f"cannot open the database {db} ({exc}); run as the user who owns config/ "
            "(the stack's PUID:PGID), or with sudo"
        ) from exc
    try:
        target = sqlite3.connect(into)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise BackupError(f"the online backup of {db} failed: {exc}") from exc
    finally:
        source.close()
    return check_database(into)


def check_database(path: Path) -> dict[str, object]:
    """integrity_check plus the applied migrations; raises BackupError if the file is not sound."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise BackupError(f"the database copy is damaged: {result[:3]}")
            migrations = [
                row[0]
                for row in conn.execute("SELECT name FROM schema_migrations ORDER BY version")
            ]
            jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            subs = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"the database copy cannot be read: {exc}") from exc
    return {"migrations": migrations, "jobs": jobs, "subscriptions": subs}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def list_archives(target: Path) -> list[Path]:
    """The archives this script made in `target`, oldest first (the name sorts by time)."""
    if not target.is_dir():
        return []
    return sorted(p for p in target.iterdir() if p.is_file() and NAME_RE.fullmatch(p.name))


def archive_name(target: Path, now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    name = f"ytp-backup-{stamp}.tar.gz"
    counter = 1
    while (target / name).exists():
        name = f"ytp-backup-{stamp}-{counter}.tar.gz"
        counter += 1
    return name


# ------------------------------------------------------------------------------------ create


def create(args: argparse.Namespace) -> int:
    root: Path = args.repo_root.resolve()
    config = (root / "config").resolve()
    env_file = root / ".env"
    db = config / "app" / "app.db"
    target: Path = args.target.resolve()
    keep: int = args.keep

    if keep < 1:
        raise BackupError("--keep must be at least 1")
    if not config.is_dir():
        raise BackupError(f"{config} does not exist (is this the stack's folder?)")
    if not env_file.is_file():
        raise BackupError(f"{env_file} does not exist; without it a restore has no secrets")
    if not db.is_file():
        raise BackupError(
            f"{db} does not exist: the app has not started yet, so there is nothing to back up"
        )
    if target == config or config in target.parents:
        raise BackupError(f"the target {target} is inside config/, which would back itself up")

    dirs, files, skipped = collect_config(config)
    size = sum((config / f).stat().st_size for f in files) + db.stat().st_size
    size += env_file.stat().st_size
    now = datetime.now(UTC)
    name = archive_name(target, now)
    existing = list_archives(target)
    # After this run there are len(existing) + 1 archives; the oldest beyond `keep` go.
    doomed = existing[: max(0, len(existing) + 1 - keep)]

    print(f"backup of {root}")
    print(f"  database    config/app/app.db (online copy, {fmt_size(db.stat().st_size)})")
    print(f"  config      {len(files)} files in {len(dirs) + 1} folders")
    print(f"  environment .env ({fmt_size(env_file.stat().st_size)})")
    for item in skipped:
        print(f"  skipped (link or special file): config/{item}")
    print(f"  uncompressed size about {fmt_size(size)}")
    print(f"  archive     {target / name}")
    for old in doomed:
        print(f"  would remove the oldest archive {old.name} (keeping {keep})")
    if args.dry_run:
        print("dry run: nothing was written")
        return 0

    if not target.exists():
        target.mkdir(parents=True, mode=0o700)
    elif target.stat().st_mode & 0o077:
        print(f"note: {target} is readable by others; the archives themselves are mode 600")
    with tempfile.TemporaryDirectory(dir=target, prefix=".work-") as work_name:
        work = Path(work_name)
        work.chmod(0o700)
        db_copy = work / "app.db"
        facts = backup_database(db, db_copy)
        manifest = {
            "format": 1,
            "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "database": {**facts, "sha256": sha256(db_copy)},
            "config_files": len(files),
            "skipped": [str(s) for s in skipped],
        }
        partial = target / f".partial-{name}"
        try:
            write_archive(partial, config, dirs, files, db_copy, env_file, manifest)
            problems = verify_archive(partial)
            if problems:
                raise BackupError("the new archive failed its own check: " + "; ".join(problems))
            partial.chmod(0o600)
            os.replace(partial, target / name)
        finally:
            partial.unlink(missing_ok=True)
    print(f"written {target / name} ({fmt_size((target / name).stat().st_size)}, mode 600)")
    for old in doomed:
        old.unlink()
        print(f"removed {old.name}")
    return 0


def write_archive(
    path: Path,
    config: Path,
    dirs: list[Path],
    files: list[Path],
    db_copy: Path,
    env_file: Path,
    manifest: dict[str, object],
) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            blob = json.dumps(manifest, indent=2, sort_keys=True).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(blob)
            info.mode = 0o600
            info.mtime = int(datetime.now(UTC).timestamp())
            tar.addfile(info, io.BytesIO(blob))
            for directory in ("db", "config", "env"):
                add_dir(tar, directory, 0o700)
            for rel in dirs:
                add_dir(tar, f"config/{rel.as_posix()}", (config / rel).stat().st_mode & 0o777)
            for rel in files:
                tar.add(
                    config / rel,
                    arcname=f"config/{rel.as_posix()}",
                    recursive=False,
                    filter=private,
                )
            tar.add(db_copy, arcname="db/app.db", recursive=False, filter=private)
            tar.add(env_file, arcname="env/dotenv", recursive=False, filter=private)
    except PermissionError as exc:
        raise BackupError(
            f"cannot read {exc.filename}; run as the user who owns config/ or with sudo "
            "(no incomplete archive was kept)"
        ) from exc


def private(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """No owner names or ids in the archive, and files readable by their owner only."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600
    return info


def add_dir(tar: tarfile.TarFile, name: str, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode | 0o700
    info.mtime = int(datetime.now(UTC).timestamp())
    tar.addfile(info)


# ------------------------------------------------------------------------------------ verify


def safe_member_problem(member: tarfile.TarInfo) -> str | None:
    """Why a member must not be extracted, or None."""
    name = member.name
    if name == MANIFEST:
        return None if member.isreg() else "the manifest is not a regular file"
    parts = Path(name).parts
    if name.startswith("/") or ".." in parts or not parts or parts[0] not in TOP_LEVEL:
        return f"unexpected name {name!r}"
    if not (member.isreg() or member.isdir()):
        return f"{name!r} is a link or special file"
    if member.mode & (stat.S_ISUID | stat.S_ISGID):
        return f"{name!r} has setuid/setgid bits"
    return None


def verify_archive(path: Path) -> list[str]:
    """Read the whole archive and check it; returns a list of problems (empty = fine)."""
    problems: list[str] = []
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            names = {m.name for m in members}
            for member in members:
                problem = safe_member_problem(member)
                if problem:
                    problems.append(problem)
                elif member.isreg():
                    handle = tar.extractfile(member)
                    assert handle is not None
                    while handle.read(1 << 20):
                        pass
            for required in (MANIFEST, "db/app.db", "env/dotenv"):
                if required not in names:
                    problems.append(f"{required} is missing")
            if not problems:
                manifest = json.load(tar.extractfile(MANIFEST))  # type: ignore[arg-type]
                with tempfile.TemporaryDirectory() as work:
                    db_path = Path(work) / "app.db"
                    handle = tar.extractfile("db/app.db")
                    assert handle is not None
                    db_path.write_bytes(handle.read())
                    check_database(db_path)
                    expected = manifest.get("database", {}).get("sha256")
                    if expected and sha256(db_path) != expected:
                        problems.append("the database does not match the manifest")
    except (tarfile.TarError, OSError, EOFError, json.JSONDecodeError) as exc:
        problems.append(f"not a readable archive: {exc}")
    except BackupError as exc:
        problems.append(str(exc))
    return problems


def verify(args: argparse.Namespace) -> int:
    problems = verify_archive(args.archive)
    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}", file=sys.stderr)
        return 1
    print(f"{args.archive} is complete and its database is sound")
    return 0


# ------------------------------------------------------------------------------------ restore


def restore(args: argparse.Namespace) -> int:
    archive: Path = args.archive
    into: Path = args.into
    problems = verify_archive(archive)
    if problems:
        raise BackupError("refusing to restore: " + "; ".join(problems))
    if into.exists() and (not into.is_dir() or any(into.iterdir())):
        raise BackupError(f"{into} must not exist or must be an empty folder")
    if args.dry_run:
        print(f"dry run: would unpack {archive} into {into}")
        return 0
    into.mkdir(parents=True, exist_ok=True)
    into.chmod(0o700)
    # Not `extractall`: the tarfile extraction filters need Python 3.11.4, and Debian 12 has 3.11.2.
    # Every member was checked above (regular files and folders under db/, config/, env/ only,
    # no ".." and no absolute names), so each one is written by hand, readable by the owner only.
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            destination = into / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(0o700)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(member)
            assert handle is not None
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                while block := handle.read(1 << 20):
                    out.write(block)
    for folder in [into, *(p for p in into.rglob("*") if p.is_dir())]:
        folder.chmod(0o700)
    print(f"unpacked into {into}:")
    print(f"  {into}/config   -> the stack's config/ folder")
    print(f"  {into}/env/dotenv -> the stack's .env")
    print(f"  {into}/db/app.db  -> config/app/app.db (with the app stopped)")
    print('see docs/operations.md, "Restoring", for the exact steps and owners')
    return 0


def list_command(args: argparse.Namespace) -> int:
    archives = list_archives(args.target)
    if not archives:
        print(f"no archives in {args.target}")
        return 0
    for path in archives:
        print(f"{path.name}  {fmt_size(path.stat().st_size)}")
    return 0


# ------------------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up and restore the stack's state.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    def add_target(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--target",
            type=Path,
            default=None,
            help="folder for the archives (default: $YTP_BACKUP_DIR, else <repo>/backups)",
        )

    make = sub.add_parser("create", help="make a new backup (the default)")
    add_target(make)
    make.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="archives to keep (14)")
    make.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    make.set_defaults(func=create)
    show = sub.add_parser("list", help="list the archives")
    add_target(show)
    show.set_defaults(func=list_command)
    check = sub.add_parser("verify", help="check an archive is complete and sound")
    check.add_argument("archive", type=Path)
    check.set_defaults(func=verify)
    back = sub.add_parser("restore", help="unpack an archive into an empty folder")
    back.add_argument("archive", type=Path)
    back.add_argument("--into", type=Path, required=True)
    back.add_argument("--dry-run", action="store_true")
    back.set_defaults(func=restore)

    argv = sys.argv[1:] if argv is None else argv
    # `create` is the default command; the global option (--repo-root VALUE) may come first.
    position = 2 if argv[:1] == ["--repo-root"] else 0
    known = (["create"], ["list"], ["verify"], ["restore"], ["-h"], ["--help"])
    if argv[position : position + 1] not in known:
        argv = [*argv[:position], "create", *argv[position:]]
    args = parser.parse_args(argv)
    if hasattr(args, "target") and args.target is None:
        from_env = os.environ.get("YTP_BACKUP_DIR")
        args.target = Path(from_env) if from_env else args.repo_root / "backups"
    try:
        return args.func(args)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
