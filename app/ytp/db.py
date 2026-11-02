"""SQLite access and migrations (issue #9).

Standard library only: `sqlite3`, no ORM, no migration framework. The schema is small and a
plain numbered SQL file per change can be read in full when something goes wrong.

* WAL mode: readers do not block the writer and the other way round, so the reconciler's
  polling (#15) and a request handler do not queue behind each other. WAL needs a local
  filesystem that supports shared memory: `/config` is on the VM's own disk, not on the
  virtiofs share.
* Migrations are `ytp/migrations/NNNN_name.sql`, applied in order at every start. Each file
  and its bookkeeping row (`schema_migrations`: version, name, SHA-256 of the file) are one
  transaction. The app refuses to start (instead of guessing) when an applied file was
  edited afterwards, or the database holds a version this code does not know (an older image
  started on a newer database).
* One connection, shared between threads, with a lock around every use. Every statement here
  is short, so one serialised connection is simpler and safer than a pool.

Nothing here creates the database folder: it is the `/config` bind mount and must exist and
belong to the user the app runs as (docs/app.md). A problem is reported as `DatabaseError`
with a hint on how to fix it, and the app keeps running "degraded" so /health says why.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_NAME = re.compile(r"(?P<version>\d{4})_(?P<slug>[a-z0-9_]+)\.sql")
TRANSACTION_CONTROL = re.compile(
    r"^\s*(BEGIN(\s+(DEFERRED|IMMEDIATE|EXCLUSIVE))?(\s+TRANSACTION)?|COMMIT|ROLLBACK)\s*;",
    re.I | re.M,
)

# The values the CHECK constraints in 0001_initial.sql allow. tests/unit/test_app_db.py proves
# they match, so code and schema cannot drift apart.
JOB_TYPES = ("magnet", "youtube", "podcast")
JOB_STATUSES = ("queued", "downloading", "organizing", "done", "failed")

LAYOUT_HINT = (
    "the folder must exist and be writable by the user the app runs as (PUID:PGID); "
    "on the host run: make layout   (sudo scripts/host/create-layout.sh)"
)


class DatabaseError(Exception):
    """The database cannot be opened or used. `hint` says what to do about it."""

    def __init__(self, message: str, hint: str = ""):
        self.hint = hint
        super().__init__(message)


class MigrationError(DatabaseError):
    """A migration failed, or the migrations and the database do not fit together."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read and validate the migration files: names, numbering without gaps, no BEGIN/COMMIT."""
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise MigrationError(f"migration file {path.name!r} must be named NNNN_name.sql")
        data = path.read_bytes()
        found.append(
            Migration(
                version=int(match["version"]),
                name=path.name,
                sql=data.decode("utf-8"),
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(
            f"migrations must be numbered 0001, 0002, ... without gaps: {versions}"
        )
    for migration in found:
        # The runner owns the transaction. (A trigger's own BEGIN ... END is not matched: its
        # BEGIN is not followed by a semicolon.)
        if TRANSACTION_CONTROL.search(migration.sql):
            raise MigrationError(f"{migration.name} must not contain BEGIN/COMMIT/ROLLBACK")
    return found


def connect(path: Path) -> sqlite3.Connection:
    """Open the database file with the settings the app relies on. Does not migrate."""
    folder = path.parent
    if not folder.is_dir():
        raise DatabaseError(f"database folder {folder} does not exist", LAYOUT_HINT)
    if not os.access(folder, os.W_OK | os.X_OK):
        raise DatabaseError(
            f"database folder {folder} is not writable for uid {os.getuid()}:{os.getgid()}",
            LAYOUT_HINT,
        )
    try:
        conn = sqlite3.connect(
            path,
            # Shared between the event loop and worker threads; Database serialises the use.
            check_same_thread=False,
            # Explicit transactions only (BEGIN IMMEDIATE ... COMMIT), see Database.transaction.
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            conn.close()
            raise DatabaseError(
                f"SQLite cannot use WAL mode here (journal mode is {mode!r})",
                "put the database on a local filesystem (the VM disk), not on a network share",
            )
        # NORMAL is the documented companion to WAL: safe against a crash of the process, and
        # at most the last commits are lost if the host loses power. Jobs can be re-submitted.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # A second writer waits a few seconds for the lock instead of failing at once.
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot open the database {path}: {exc}", LAYOUT_HINT) from exc
    return conn


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None) -> list[str]:
    """Apply the migrations that have not run yet, in order. Returns the names applied.

    Safe to run at every start: with nothing pending it only reads.
    """
    wanted = list(load_migrations() if migrations is None else migrations)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    by_version = {m.version: m for m in wanted}
    applied = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    for row in applied:
        known = by_version.get(row["version"])
        if known is None:
            raise MigrationError(
                f"the database is at migration {row['name']}, which this version of the app "
                "does not know: it was written by a newer version",
                "start the newer image again, or restore a backup of the database",
            )
        if known.checksum != row["checksum"] or known.name != row["name"]:
            raise MigrationError(
                f"migration {row['name']} was changed after it was applied",
                "never edit a shipped migration: restore the original file and add a new one",
            )
    done = {row["version"] for row in applied}
    ran: list[str] = []
    for migration in wanted:
        if migration.version in done:
            continue
        # The name is validated (a-z, 0-9, _ and .sql) and the checksum is hex, so inlining
        # them is safe; executescript() cannot take parameters.
        script = (
            f"BEGIN IMMEDIATE;\n{migration.sql}\n"
            "INSERT INTO schema_migrations (version, name, checksum) "
            f"VALUES ({migration.version}, '{migration.name}', '{migration.checksum}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(
                f"migration {migration.name} failed and was rolled back: {exc}"
            ) from exc
        logger.info("migration applied", extra={"migration": migration.name})
        ran.append(migration.name)
    return ran


class Database:
    """The app's one SQLite connection, opened, migrated and safe to share between threads."""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self._conn = conn
        self._lock = threading.RLock()
        self.path = path

    @classmethod
    def open(cls, path: Path, migrations: Sequence[Migration] | None = None) -> Database:
        conn = connect(path)
        try:
            migrate(conn, migrations)
        except BaseException:
            conn.close()
            raise
        return cls(conn, path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction: BEGIN IMMEDIATE, then COMMIT, or ROLLBACK on any exception.

        IMMEDIATE takes the write lock up front, so a conflict shows up at the start (and waits
        out busy_timeout) rather than as a failure half way through.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        """Run a read-only statement and return all rows."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def check(self) -> str | None:
        """None if the database is usable, else a short reason (for /health).

        Cheap on purpose (Docker calls it every 30 s): one SELECT plus a look at the
        permissions. WAL also needs the folder to be writable, to create its -wal/-shm files.
        """
        try:
            self.query("SELECT 1")
        except sqlite3.Error as exc:
            return f"query failed: {exc}"
        if not os.access(self.path.parent, os.W_OK) or not os.access(self.path, os.W_OK):
            return f"{self.path.parent} is not writable"
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
