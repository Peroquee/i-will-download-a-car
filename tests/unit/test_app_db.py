"""app/ytp/db.py and migrations/0001_initial.sql: WAL, the schema, and the migration runner."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from ytp import db as dbmod
from ytp.config import Settings
from ytp.db import (
    JOB_STATUSES,
    JOB_TYPES,
    Database,
    DatabaseError,
    Migration,
    MigrationError,
    connect,
    load_migrations,
    migrate,
)

SHIPPED = load_migrations()


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def insert_job(conn, **values) -> None:
    row = {"type": "magnet", "external_id": "abc", **values}
    conn.execute(
        f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )


# --- connection settings -------------------------------------------------------------


def test_the_database_uses_wal_mode(tmp_path):
    conn = connect(tmp_path / "app.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_wal_is_stored_in_the_file_so_it_survives_a_reopen(tmp_path):
    connect(tmp_path / "app.db").close()
    other = sqlite3.connect(tmp_path / "app.db")
    assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    other.close()


def test_connection_pragmas(tmp_path):
    conn = connect(tmp_path / "app.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    conn.close()


def test_a_missing_folder_is_not_created(tmp_path):
    with pytest.raises(DatabaseError, match="does not exist") as error:
        connect(tmp_path / "config" / "app.db")
    assert not (tmp_path / "config").exists()
    assert "make layout" in error.value.hint


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_folder_the_user_cannot_write_is_explained(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir(mode=0o555)
    try:
        with pytest.raises(DatabaseError, match="not writable") as error:
            connect(folder / "app.db")
    finally:
        folder.chmod(0o755)
    assert "PUID:PGID" in error.value.hint


def test_sqlite_errors_become_database_errors(tmp_path, monkeypatch):
    # Also covers a root-run CI, where the permission test above is skipped.
    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(dbmod.sqlite3, "connect", refuse)
    with pytest.raises(DatabaseError, match="unable to open database file") as error:
        connect(tmp_path / "app.db")
    assert "make layout" in error.value.hint


def test_a_filesystem_without_wal_is_refused(tmp_path, monkeypatch):
    class NoWal(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA journal_mode"):
                return super().execute("PRAGMA journal_mode = DELETE")
            return super().execute(sql, *args)

    real = sqlite3.connect
    monkeypatch.setattr(dbmod.sqlite3, "connect", lambda *a, **k: real(*a, factory=NoWal, **k))
    with pytest.raises(DatabaseError, match="WAL") as error:
        connect(tmp_path / "app.db")
    assert "local filesystem" in error.value.hint


def test_not_a_database_file_is_reported(tmp_path):
    (tmp_path / "app.db").write_bytes(b"definitely not sqlite" * 50)
    with pytest.raises(DatabaseError, match="cannot open the database"):
        Database.open(tmp_path / "app.db")


def test_the_database_lives_in_the_config_folder():
    settings = Settings.from_env({"CONFIG_DIR": "/somewhere"} | _media_env())
    assert settings.database_path == Path("/somewhere/app.db")
    assert Settings().database_path == Path("/config/app.db")


def _media_env() -> dict[str, str]:
    return {
        "MEDIA_DIR_MOVIES": "a",
        "MEDIA_DIR_SERIES": "b",
        "MEDIA_DIR_AUDIOBOOKS": "c",
        "MEDIA_DIR_MUSIC": "d",
        "MEDIA_DIR_PODCASTS": "f",
        "MEDIA_DIR_YOUTUBE": "e",
    }


# --- the schema ----------------------------------------------------------------------


def test_a_new_database_gets_the_initial_schema(database):
    conn = database._conn
    assert {"jobs", "subscriptions", "schema_migrations"} <= tables(conn)
    assert list(columns(conn, "jobs")) == [
        "id",
        "type",
        "external_id",
        "metadata",
        "status",
        "error",
        "created_at",
        "updated_at",
        "source",
        "progress_bytes",
        "progress_at",
        "priority",
        "note",
    ]
    assert list(columns(conn, "subscriptions")) == [
        "id",
        "channel_id",
        "channel_name",
        "last_checked",
        "last_error",
        "enabled",
        "created_at",
        "backfill",
        "target",
        "audio_only",
        "max_height",  # #65
        "poll_minutes",  # #79
        "poll_at",
    ]


def test_not_null_columns(database):
    jobs, subs = columns(database._conn, "jobs"), columns(database._conn, "subscriptions")
    for name in ("type", "external_id", "metadata", "status", "created_at", "updated_at"):
        assert jobs[name]["notnull"] == 1, name
    assert jobs["error"]["notnull"] == 0
    for name in ("channel_id", "channel_name", "enabled"):
        assert subs[name]["notnull"] == 1, name
    for name in ("last_checked", "last_error"):
        assert subs[name]["notnull"] == 0, name


def test_a_job_gets_sensible_defaults(database):
    with database.transaction() as conn:
        insert_job(conn, external_id="0123abcd")
    (row,) = database.query("SELECT * FROM jobs")
    assert row["id"] == 1
    assert row["status"] == "queued"
    assert row["priority"] == 0, "a job is a normal download unless asked otherwise (#51)"
    assert row["metadata"] == "{}"
    assert row["error"] is None
    assert row["created_at"].endswith("Z")
    assert row["updated_at"] == row["created_at"]


def test_timestamps_are_utc_iso_8601_with_milliseconds(database):
    with database.transaction() as conn:
        insert_job(conn)
    (row,) = database.query("SELECT created_at FROM jobs")
    assert len(row["created_at"]) == len("2026-11-01T12:34:56.789Z")
    assert row["created_at"][10] == "T"


def test_the_check_constraints_match_the_python_constants(database):
    # 0005 rebuilt the jobs table, so its CHECK constraints live there now.
    sql = (dbmod.MIGRATIONS_DIR / "0005_podcasts.sql").read_text(encoding="utf-8")
    for value in (*JOB_TYPES, *JOB_STATUSES):
        assert f"'{value}'" in sql
    with database.transaction() as conn:
        for job_type in JOB_TYPES:
            insert_job(conn, type=job_type)
        for status in JOB_STATUSES:
            insert_job(conn, external_id=f"id-{status}", status=status)


@pytest.mark.parametrize(
    "values",
    [
        {"type": "ftp"},
        {"status": "paused"},
        {"external_id": ""},
        {"metadata": "not json"},
        {"metadata": "[1, 2]"},
        {"metadata": '"text"'},
        {"type": None},
        {"external_id": None},
        {"status": None},
    ],
)
def test_bad_jobs_are_rejected(database, values):
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        insert_job(conn, **values)
    assert database.query("SELECT COUNT(*) FROM jobs")[0][0] == 0


def test_json_metadata_round_trips_with_umlauts(database):
    meta = {"title": "Größte Hörbücher", "year": 2026, "tags": ["a", "b"]}
    with database.transaction() as conn:
        insert_job(conn, metadata=json.dumps(meta, ensure_ascii=False))
    (row,) = database.query("SELECT json_extract(metadata, '$.title') AS title, metadata FROM jobs")
    assert row["title"] == "Größte Hörbücher"
    assert json.loads(row["metadata"]) == meta


def test_the_same_thing_twice_is_one_job_but_types_do_not_collide(database):
    with database.transaction() as conn:
        insert_job(conn, type="magnet", external_id="same")
        insert_job(conn, type="youtube", external_id="same")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), database.transaction() as conn:
        insert_job(conn, type="magnet", external_id="same")


def test_updating_a_job_moves_updated_at_but_not_created_at(database):
    with database.transaction() as conn:
        insert_job(
            conn, created_at="2026-01-01T00:00:00.000Z", updated_at="2026-01-01T00:00:00.000Z"
        )
    with database.transaction() as conn:
        conn.execute("UPDATE jobs SET status = 'downloading'")
    (row,) = database.query("SELECT * FROM jobs")
    assert row["created_at"] == "2026-01-01T00:00:00.000Z"
    assert row["updated_at"] > "2026-01-01T00:00:00.000Z"


def test_an_explicit_updated_at_is_respected(database):
    with database.transaction() as conn:
        insert_job(conn)
        conn.execute("UPDATE jobs SET status = 'done', updated_at = '2030-01-01T00:00:00.000Z'")
    assert database.query("SELECT updated_at FROM jobs")[0][0] == "2030-01-01T00:00:00.000Z"


def test_subscriptions_defaults_and_constraints(database):
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC123', 'Kanal Ä')"
        )
    (row,) = database.query("SELECT * FROM subscriptions")
    assert (row["enabled"], row["last_checked"], row["last_error"]) == (1, None, None)
    assert row["channel_name"] == "Kanal Ä"
    for sql in (
        "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC123', 'dup')",
        "INSERT INTO subscriptions (channel_id, channel_name, enabled) VALUES ('UC9', 'x', 2)",
        "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('', 'x')",
        "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC8', NULL)",
    ):
        with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
            conn.execute(sql)


def test_the_expected_indexes_exist(database):
    names = {r[0] for r in database.query("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {
        "jobs_type_external_id",
        "jobs_status_priority",
        "jobs_status_updated_at",
        "subscriptions_enabled_last_checked",
    } <= names
    # 0018 dropped the two that no query uses (#72).
    assert not {"jobs_status", "jobs_created_at"} & names


# --- the migration runner ------------------------------------------------------------


def write(folder: Path, name: str, sql: str) -> None:
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(sql, encoding="utf-8")


@pytest.fixture
def folder(tmp_path) -> Path:
    return tmp_path / "migrations"


def fresh(tmp_path) -> sqlite3.Connection:
    return connect(tmp_path / "m.db")


def test_the_shipped_migrations_are_valid():
    assert SHIPPED[0].name == "0001_initial.sql"
    assert [m.version for m in SHIPPED] == list(range(1, len(SHIPPED) + 1))


def test_pending_migrations_run_in_order_and_are_recorded(tmp_path, folder):
    write(folder, "0001_a.sql", "CREATE TABLE a (x);")
    write(folder, "0002_b.sql", "CREATE TABLE b (y); INSERT INTO a VALUES (1);")
    conn = fresh(tmp_path)
    assert migrate(conn, load_migrations(folder)) == ["0001_a.sql", "0002_b.sql"]
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY 1"
    ).fetchall()
    assert [(r["version"], r["name"]) for r in rows] == [(1, "0001_a.sql"), (2, "0002_b.sql")]
    assert all(len(r["checksum"]) == 64 for r in rows)


def test_running_twice_applies_nothing_the_second_time(tmp_path):
    conn = fresh(tmp_path)
    assert migrate(conn) == [m.name for m in SHIPPED]
    assert migrate(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(SHIPPED)


def test_only_new_migrations_run_after_an_update(tmp_path, folder):
    write(folder, "0001_a.sql", "CREATE TABLE a (x);")
    conn = fresh(tmp_path)
    migrate(conn, load_migrations(folder))
    conn.execute("INSERT INTO a VALUES (42)")
    write(folder, "0002_b.sql", "ALTER TABLE a ADD COLUMN y DEFAULT 'new';")
    assert migrate(conn, load_migrations(folder)) == ["0002_b.sql"]
    assert conn.execute("SELECT x, y FROM a").fetchone()[:] == (42, "new")


def test_a_failing_migration_changes_nothing_and_is_not_recorded(tmp_path, folder):
    write(folder, "0001_a.sql", "CREATE TABLE a (x);")
    write(folder, "0002_bad.sql", "CREATE TABLE b (y); INSERT INTO a (nope) VALUES (1);")
    conn = fresh(tmp_path)
    with pytest.raises(MigrationError, match=r"0002_bad\.sql failed and was rolled back"):
        migrate(conn, load_migrations(folder))
    assert "a" in tables(conn)
    assert "b" not in tables(conn), "half of a failed migration must not stay"
    assert not conn.in_transaction
    assert [r[0] for r in conn.execute("SELECT name FROM schema_migrations")] == ["0001_a.sql"]
    # Fixed file, same version number: it runs now (the failed one was never recorded).
    write(folder, "0002_bad.sql", "CREATE TABLE b (y);")
    assert migrate(conn, load_migrations(folder)) == ["0002_bad.sql"]


def test_a_migration_may_rebuild_a_parent_table_without_cascading_into_children(tmp_path, folder):
    """#68: with foreign keys on, DROP TABLE of a rebuilt parent would wipe its children."""
    write(
        folder,
        "0001_a.sql",
        "CREATE TABLE parents (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
        "CREATE TABLE children ("
        " id INTEGER PRIMARY KEY,"
        " parent_id INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE);\n"
        "INSERT INTO parents (id, name) VALUES (1, 'one'), (2, 'two');\n"
        "INSERT INTO children (id, parent_id) VALUES (10, 1), (11, 1), (12, 2);\n",
    )
    conn = fresh(tmp_path)
    migrate(conn, load_migrations(folder))
    write(
        folder,
        "0002_rebuild.sql",
        "CREATE TABLE parents_new (id INTEGER PRIMARY KEY, name TEXT NOT NULL, extra TEXT);\n"
        "INSERT INTO parents_new (id, name) SELECT id, name FROM parents;\n"
        "DROP TABLE parents;\n"
        "ALTER TABLE parents_new RENAME TO parents;\n",
    )
    assert migrate(conn, load_migrations(folder)) == ["0002_rebuild.sql"]
    assert [tuple(r) for r in conn.execute("SELECT id, parent_id FROM children ORDER BY id")] == [
        (10, 1),
        (11, 1),
        (12, 2),
    ]
    assert conn.execute("SELECT name, extra FROM parents WHERE id = 2").fetchone()[:] == (
        "two",
        None,
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "the pragma must be back on"


def test_a_migration_that_would_leave_broken_foreign_keys_is_rolled_back(tmp_path, folder):
    write(
        folder,
        "0001_a.sql",
        "CREATE TABLE parents (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE children ("
        " id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parents(id));\n"
        "INSERT INTO parents (id) VALUES (1);\n"
        "INSERT INTO children (id, parent_id) VALUES (10, 1);\n",
    )
    conn = fresh(tmp_path)
    migrate(conn, load_migrations(folder))
    write(folder, "0002_orphans.sql", "DELETE FROM parents;\n")
    with pytest.raises(MigrationError, match="broken foreign keys"):
        migrate(conn, load_migrations(folder))
    assert conn.execute("SELECT COUNT(*) FROM parents").fetchone()[0] == 1, "rolled back"
    assert not conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "the pragma must be back on"
    assert [r[0] for r in conn.execute("SELECT name FROM schema_migrations")] == ["0001_a.sql"]


def test_an_edited_migration_is_refused(tmp_path, folder):
    write(folder, "0001_a.sql", "CREATE TABLE a (x);")
    conn = fresh(tmp_path)
    migrate(conn, load_migrations(folder))
    write(folder, "0001_a.sql", "CREATE TABLE a (x, extra);")
    with pytest.raises(MigrationError, match="changed after it was applied") as error:
        migrate(conn, load_migrations(folder))
    assert "add a new one" in error.value.hint


def test_a_database_from_a_newer_app_is_refused(tmp_path, folder):
    write(folder, "0001_a.sql", "CREATE TABLE a (x);")
    write(folder, "0002_b.sql", "CREATE TABLE b (y);")
    conn = fresh(tmp_path)
    migrate(conn, load_migrations(folder))
    (folder / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="newer version"):
        migrate(conn, load_migrations(folder))
    assert "b" in tables(conn), "an older image must not touch a newer database"


def test_a_renamed_migration_is_refused(tmp_path, folder):
    write(folder, "0001_a.sql", "CREATE TABLE a (x);")
    conn = fresh(tmp_path)
    migrate(conn, load_migrations(folder))
    (folder / "0001_a.sql").rename(folder / "0001_other.sql")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(conn, load_migrations(folder))


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["001_short.sql"], "NNNN_name.sql"),
        (["0001_Upper.sql"], "NNNN_name.sql"),
        (["0001_a.sql", "0003_c.sql"], "without gaps"),
        (["0002_b.sql"], "without gaps"),
        (["0001_a.sql", "0001_b.sql"], "without gaps"),
    ],
)
def test_bad_migration_names_and_numbering(folder, names, message):
    for name in names:
        write(folder, name, "SELECT 1;")
    with pytest.raises(MigrationError, match=message):
        load_migrations(folder)


@pytest.mark.parametrize("statement", ["BEGIN;", "begin transaction;", "COMMIT;", "ROLLBACK;"])
def test_a_migration_may_not_control_the_transaction(folder, statement):
    write(folder, "0001_a.sql", f"CREATE TABLE a (x);\n{statement}\n")
    with pytest.raises(MigrationError, match="BEGIN/COMMIT"):
        load_migrations(folder)


def test_a_trigger_with_its_own_begin_end_is_allowed(folder):
    sql = "CREATE TABLE a (x);\nCREATE TRIGGER t AFTER INSERT ON a BEGIN\n  SELECT 1;\nEND;\n"
    write(folder, "0001_a.sql", sql)
    assert [m.name for m in load_migrations(folder)] == ["0001_a.sql"]


def test_an_empty_migrations_folder_is_fine(tmp_path, folder):
    folder.mkdir()
    assert migrate(fresh(tmp_path), load_migrations(folder)) == []


def test_open_closes_the_connection_when_a_migration_fails(tmp_path):
    bad = Migration(1, "0001_bad.sql", "NOT SQL AT ALL;", "0" * 64)
    with pytest.raises(MigrationError):
        Database.open(tmp_path / "app.db", [bad])
    # The file is usable again straight away (no leaked lock).
    assert Database.open(tmp_path / "app.db").check() is None


# --- the Database wrapper ------------------------------------------------------------


def test_a_transaction_commits_on_success_and_rolls_back_on_error(database):
    with database.transaction() as conn:
        insert_job(conn, external_id="kept")

    def fail_inside_a_transaction():
        with database.transaction() as conn:
            insert_job(conn, external_id="dropped")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        fail_inside_a_transaction()
    assert [r["external_id"] for r in database.query("SELECT external_id FROM jobs")] == ["kept"]
    assert not database._conn.in_transaction


class CommitFails:
    """A connection proxy whose COMMIT raises, optionally after SQLite rolled back itself."""

    def __init__(self, conn: sqlite3.Connection, error: sqlite3.Error, *, rolled_back: bool):
        self._conn = conn
        self._error = error
        self._rolled_back = rolled_back

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def execute(self, sql, params=()):
        if sql.strip().upper() == "COMMIT":
            if self._rolled_back:
                self._conn.execute("ROLLBACK")
            raise self._error
        return self._conn.execute(sql, params)

    def close(self) -> None:
        self._conn.close()


@pytest.mark.parametrize("sqlite_rolled_back", [False, True])
def test_a_failed_commit_rolls_back_and_the_connection_stays_usable(database, sqlite_rolled_back):
    """#68: a COMMIT error must not leave the shared connection inside a transaction."""
    real = database._conn
    error = sqlite3.OperationalError("database or disk is full")
    database._conn = CommitFails(real, error, rolled_back=sqlite_rolled_back)
    with pytest.raises(sqlite3.OperationalError) as raised, database.transaction() as conn:
        insert_job(conn, external_id="not-committed")
    assert raised.value is error, "a ROLLBACK must not hide the error COMMIT raised"
    assert not real.in_transaction
    database._conn = real
    with database.transaction() as conn:
        insert_job(conn, external_id="after")
    assert [r["external_id"] for r in database.query("SELECT external_id FROM jobs")] == ["after"]


def test_an_error_after_sqlite_rolled_back_by_itself_reaches_the_caller(database):
    """SQLITE_FULL-style: SQLite ends the transaction, then the statement raises."""

    class RollsBackByItself:
        def __init__(self, conn):
            self._conn = conn

        @property
        def in_transaction(self):
            return self._conn.in_transaction

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO JOBS"):
                self._conn.execute("ROLLBACK")
                raise sqlite3.OperationalError("database or disk is full")
            return self._conn.execute(sql, params)

        def close(self):
            self._conn.close()

    real = database._conn
    database._conn = RollsBackByItself(real)
    with (
        pytest.raises(sqlite3.OperationalError, match="disk is full"),
        database.transaction() as conn,
    ):
        insert_job(conn, external_id="lost")
    assert not real.in_transaction
    database._conn = real
    with database.transaction() as conn:
        insert_job(conn, external_id="after")
    assert [r["external_id"] for r in database.query("SELECT external_id FROM jobs")] == ["after"]


def test_two_writers_wait_for_each_other_instead_of_failing(database):
    started = threading.Event()
    errors: list[BaseException] = []

    def slow_writer():
        try:
            with database.transaction() as conn:
                insert_job(conn, external_id="first")
                started.set()
                time.sleep(0.4)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread = threading.Thread(target=slow_writer)
    thread.start()
    started.wait(5)
    with database.transaction() as conn:
        insert_job(conn, external_id="second")
    thread.join()
    assert not errors
    assert database.query("SELECT COUNT(*) FROM jobs")[0][0] == 2


def test_a_second_process_waits_for_the_write_lock(tmp_path):
    """busy_timeout: another connection (like a backup tool) does not get 'database is locked'."""
    path = tmp_path / "app.db"
    first = Database.open(path)
    other = connect(path)
    release = threading.Timer(0.3, lambda: first._conn.execute("COMMIT"))
    first._conn.execute("BEGIN IMMEDIATE")
    release.start()
    started = time.monotonic()
    other.execute("BEGIN IMMEDIATE")  # waits for the first transaction
    waited = time.monotonic() - started
    other.execute("COMMIT")
    release.join()
    assert 0.2 < waited < 4
    first.close()
    other.close()


def test_readers_are_not_blocked_by_an_open_write_transaction(tmp_path):
    path = tmp_path / "app.db"
    writer = Database.open(path)
    reader = connect(path)
    with writer.transaction() as conn:
        insert_job(conn, external_id="uncommitted")
        assert reader.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert reader.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    writer.close()
    reader.close()


def test_data_survives_closing_and_reopening(tmp_path):
    first = Database.open(tmp_path / "app.db")
    with first.transaction() as conn:
        insert_job(conn, external_id="persisted")
    first.close()
    second = Database.open(tmp_path / "app.db")
    assert second.query("SELECT external_id FROM jobs")[0][0] == "persisted"
    assert second.check() is None
    second.close()


def test_check_reports_a_closed_connection(tmp_path):
    database = Database.open(tmp_path / "app.db")
    database.close()
    assert "query failed" in (database.check() or "")


def test_the_podcast_migration_keeps_every_job_and_its_indexes(tmp_path):
    """0005 rebuilds the jobs table to allow type 'podcast': nothing may be lost or renumbered."""
    conn = dbmod.connect(tmp_path / "app.db")
    migrations = dbmod.load_migrations()
    dbmod.migrate(conn, migrations[:4])
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO jobs (type, external_id, metadata, status, error, source) VALUES "
        "('magnet', 'aaaa', '{\"type\":\"movies\"}', 'done', NULL, 'magnet:?xt=urn:btih:aaaa'), "
        "('youtube', 'bbbbbbbbbbb', '{}', 'failed', 'boom', NULL), "
        "('youtube', 'ccccccccccc', '{}', 'queued', NULL, NULL)"
    )
    conn.execute("DELETE FROM jobs WHERE external_id = 'ccccccccccc'")  # a gap in the ids
    conn.execute("COMMIT")
    before = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]

    assert dbmod.migrate(conn, migrations[:5]) == ["0005_podcasts.sql"]

    after = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
    assert after == before
    columns = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
    assert columns == [
        *("id", "type", "external_id", "metadata", "status", "error"),
        *("created_at", "updated_at", "source"),
    ]
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(jobs)")}
    assert {"jobs_type_external_id", "jobs_status", "jobs_created_at"} <= indexes
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchone()[0] == (
        "jobs_touch_updated_at"
    )
    # the new type is allowed, the unique index still decides, ids go on after the old ones
    conn.execute("INSERT INTO jobs (type, external_id) VALUES ('podcast', 'k1')")
    assert conn.execute("SELECT MAX(id) FROM jobs").fetchone()[0] > max(r[0] for r in before)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (type, external_id) VALUES ('podcast', 'k1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (type, external_id) VALUES ('radio', 'k1')")
    conn.close()
