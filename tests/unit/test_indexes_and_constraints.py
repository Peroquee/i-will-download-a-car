"""Migration 0018 (#72): the index clean-up and the CHECK constraints the schema was missing.

The CHECK constraints need table rebuilds, so every test here also watches that the rebuild
keeps the rows (ids included) and re-creates the indexes and triggers the old tables had.
"""

from __future__ import annotations

import sqlite3

import pytest

from ytp import db as dbmod

BEFORE = 17  # 0001 .. 0017 are the state before this migration


def old_database(tmp_path):
    """A database at the version before 0018, with one row in every table it rebuilds."""
    conn = dbmod.connect(tmp_path / "app.db")
    migrations = dbmod.load_migrations()
    assert dbmod.migrate(conn, migrations[:BEFORE])[-1] == "0017_poll_settings.sql"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO jobs (id, type, external_id, metadata, status, error, source, "
        "progress_bytes, progress_at, priority, note) VALUES "
        "(1, 'magnet', 'aaaa', '{\"type\":\"movies\"}', 'done', NULL, "
        "'magnet:?xt=urn:btih:aaaa', 100, '2026-01-01T00:00:00.000Z', 1, 'one was skipped'), "
        "(2, 'youtube', 'bbbbbbbbbbb', '{}', 'failed', 'boom', NULL, NULL, NULL, 0, NULL), "
        "(5, 'podcast', 'cccc', '{}', 'queued', NULL, NULL, NULL, NULL, 0, NULL)"
    )
    conn.execute(
        "INSERT INTO accounts (id, username, password_hash, role) "
        "VALUES (7, 'old', 'scrypt:x', 'user')"
    )
    conn.execute("INSERT INTO job_owners (job_id, account_id) VALUES (2, 7)")
    conn.execute(
        "INSERT INTO job_dismissals (job_id, account_id, dismissed_at) "
        "VALUES (2, 7, '2026-02-02T00:00:00.000Z')"
    )
    conn.execute(
        "INSERT INTO alerts (id, kind, severity, title, params, dedupe_key) "
        "VALUES (3, 'vpn_down', 'error', 'VPN down', '{\"reason\":\"x\"}', 'vpn-1')"
    )
    conn.execute("INSERT INTO vpn_events (condition, paused, reason) VALUES ('blind', 1, 'x')")
    conn.execute(
        "INSERT INTO podcast_backlogs (id, feed_url, folder, total) "
        "VALUES (11, 'https://example.test/f.xml', 'Pod', 1)"
    )
    conn.execute(
        "INSERT INTO podcast_backlog_items (backlog_id, position, key, metadata, size_bytes) "
        "VALUES (11, 0, 'cccc', '{\"published\":\"2026-01-01\"}', 10)"
    )
    conn.execute("INSERT INTO podcast_backlog_owners (backlog_id, account_id) VALUES (11, 7)")
    conn.execute("COMMIT")
    return conn, migrations


def migrated(tmp_path):
    conn, migrations = old_database(tmp_path)
    assert dbmod.migrate(conn, migrations) == ["0018_indexes_and_constraints.sql"]
    return conn


def index_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


# --- the index findings -------------------------------------------------------------------


def test_the_two_unused_indexes_are_gone_and_the_three_new_ones_exist(tmp_path):
    conn = migrated(tmp_path)
    jobs = index_names(conn, "jobs")
    assert {"jobs_type_external_id", "jobs_status_priority", "jobs_status_updated_at"} <= jobs
    assert "jobs_status" not in jobs, "redundant: jobs_status_priority starts with status"
    assert "jobs_created_at" not in jobs, "no query sorts or filters on created_at"
    assert "podcast_backlog_items_key" in index_names(conn, "podcast_backlog_items")
    assert "podcast_backlog_owners_account" in index_names(conn, "podcast_backlog_owners")
    conn.close()


def test_the_monitor_and_finished_list_can_use_the_new_status_updated_index(tmp_path):
    conn = migrated(tmp_path)
    plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM jobs "
            "WHERE status = 'failed' AND updated_at > ? ORDER BY updated_at, id",
            ("2026-01-01T00:00:00.000Z",),
        )
    )
    assert "jobs_status_updated_at" in plan
    conn.close()


def test_the_backlog_join_can_look_items_up_by_key_and_runs_by_account(tmp_path):
    conn = migrated(tmp_path)
    by_key = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT key, metadata FROM podcast_backlog_items WHERE key = ?",
            ("cccc",),
        )
    )
    assert "podcast_backlog_items_key" in by_key
    by_account = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT backlog_id FROM podcast_backlog_owners WHERE account_id = ?",
            (7,),
        )
    )
    assert "podcast_backlog_owners_account" in by_account
    conn.close()


# --- the CHECK constraint findings --------------------------------------------------------


def test_jobs_priority_is_now_0_or_1(tmp_path):
    conn = migrated(tmp_path)
    conn.execute("UPDATE jobs SET priority = 1 WHERE id = 2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET priority = 2 WHERE id = 2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (type, external_id, priority) VALUES ('youtube', 'd', 5)")
    default = conn.execute(
        "INSERT INTO jobs (type, external_id) VALUES ('youtube', 'eeeeeeeeeee')"
    ).lastrowid
    assert conn.execute("SELECT priority FROM jobs WHERE id = ?", (default,)).fetchone()[0] == 0
    conn.close()


def test_alert_params_must_be_a_json_object(tmp_path):
    conn = migrated(tmp_path)
    conn.execute(
        "INSERT INTO alerts (kind, severity, title, params) VALUES ('x', 'info', 't', '{\"a\":1}')"
    )
    for bad in ("not json", "[]", '"text"', "null", "3"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO alerts (kind, severity, title, params) VALUES ('x', 'info', 't', ?)",
                (bad,),
            )
    conn.close()


def test_backlog_item_metadata_must_be_a_json_object(tmp_path):
    conn = migrated(tmp_path)
    conn.execute(
        "INSERT INTO podcast_backlog_items (backlog_id, position, key, metadata) "
        "VALUES (11, 1, 'dddd', '{\"a\":1}')"
    )
    for bad in ("not json", "[]", '"text"', "null", "3"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO podcast_backlog_items (backlog_id, position, key, metadata) "
                "VALUES (11, 2, ?, ?)",
                (f"k-{bad}", bad),
            )
    conn.close()


def test_vpn_event_condition_is_the_fixed_set(tmp_path):
    conn = migrated(tmp_path)
    for condition in ("ok", "down", "blind", "mismatch", "starting", "unknown"):
        conn.execute(
            "INSERT INTO vpn_events (condition, paused, reason) VALUES (?, 0, '')", (condition,)
        )
    for bad in ("", "up", "OK", "paused"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO vpn_events (condition) VALUES (?)", (bad,))
    conn.close()


# --- the rebuilds keep everything ---------------------------------------------------------


def test_the_rebuilds_keep_the_rows_ids_and_values(tmp_path):
    conn = migrated(tmp_path)
    jobs = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
    assert [row[0] for row in jobs] == [1, 2, 5]
    assert jobs[0][8] == "magnet:?xt=urn:btih:aaaa"  # source
    assert jobs[0][9:12] == (100, "2026-01-01T00:00:00.000Z", 1)  # progress, priority
    assert jobs[0][12] == "one was skipped"  # note
    assert tuple(conn.execute("SELECT params, dedupe_key FROM alerts").fetchone()) == (
        '{"reason":"x"}',
        "vpn-1",
    )
    assert tuple(
        conn.execute("SELECT key, metadata, size_bytes FROM podcast_backlog_items").fetchone()
    ) == (
        "cccc",
        '{"published":"2026-01-01"}',
        10,
    )
    assert tuple(conn.execute("SELECT condition, paused FROM vpn_events").fetchone()) == (
        "blind",
        1,
    )
    assert tuple(
        conn.execute("SELECT job_id, account_id, dismissed_at FROM job_dismissals").fetchone()
    ) == (
        2,
        7,
        "2026-02-02T00:00:00.000Z",
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_the_unique_job_key_and_the_id_sequence_survive_the_rebuild(tmp_path):
    conn = migrated(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (type, external_id) VALUES ('youtube', 'bbbbbbbbbbb')")
    new_id = conn.execute(
        "INSERT INTO jobs (type, external_id) VALUES ('youtube', 'fffffffffff')"
    ).lastrowid
    assert new_id == 6, "the new table continues after the highest id of the old one"
    conn.close()


def test_the_autoincrement_counter_survives_the_rebuild(tmp_path):
    """Jobs are AUTOINCREMENT, so a deleted top id is never handed out again.

    Production had seq 344 but MAX(id) 342 (the two newest jobs were deleted); a rebuild that
    only copies the rows resets the counter to 342 and reuses 343 and 344. Mirrored here: ids
    6..9 are used and gone before the migration, so the next id must be 10.
    """
    conn, migrations = old_database(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO jobs (id, type, external_id) VALUES (9, 'youtube', 'zzzzzzzzzzz')")
    conn.execute("DELETE FROM jobs WHERE id = 9")
    conn.execute("COMMIT")
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'").fetchone()[0]
    assert seq == 9, "the counter remembers the deleted id"

    assert dbmod.migrate(conn, migrations) == ["0018_indexes_and_constraints.sql"]
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'").fetchone()[0] == 9
    new_id = conn.execute(
        "INSERT INTO jobs (type, external_id) VALUES ('youtube', 'hhhhhhhhhhh')"
    ).lastrowid
    assert new_id == 10, "ids 6..9 were handed out before the rebuild and must not be reused"
    conn.close()


def test_the_counter_is_carried_when_the_jobs_table_is_empty(tmp_path):
    """The empty table creates no jobs_new counter row of its own; the old one must still move."""
    conn, migrations = old_database(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM jobs")
    conn.execute("COMMIT")
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'").fetchone()[0] == 5

    assert dbmod.migrate(conn, migrations) == ["0018_indexes_and_constraints.sql"]
    new_id = conn.execute(
        "INSERT INTO jobs (type, external_id) VALUES ('youtube', 'iiiiiiiiiii')"
    ).lastrowid
    assert new_id == 6, "not 1: the counter of the old table moved over"
    conn.close()


def test_the_triggers_still_work_after_the_rebuild(tmp_path):
    conn = migrated(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET status = 'downloading' WHERE id = 1")
    conn.execute("COMMIT")
    assert conn.execute("SELECT updated_at FROM jobs WHERE id = 1").fetchone()[0] > (
        "2026-01-01T00:00:00.000Z"
    )
    # the dismissal of the failed job (id 2) survives; a retried job loses it again
    assert conn.execute("SELECT COUNT(*) FROM job_dismissals WHERE job_id = 2").fetchone()[0] == 1
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET status = 'queued' WHERE id = 2")
    conn.execute("COMMIT")
    assert conn.execute("SELECT COUNT(*) FROM job_dismissals").fetchone()[0] == 0
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"jobs_touch_updated_at", "job_dismissals_cleared"} <= triggers
    conn.close()
