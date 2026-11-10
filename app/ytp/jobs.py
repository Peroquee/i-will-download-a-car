"""Reading and writing rows of the `jobs` table (issues #9, #13, #18).

Plain functions over `ytp.db.Database`, no ORM. Every value goes in as a query parameter,
never into the SQL text. The unique index on (type, external_id) is what decides "is this
torrent already in the system": the insert itself is the check, so two submissions of the same
magnet at the same moment cannot both create a job.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from .db import Database

MAX_ERROR_LENGTH = 500
MAX_NOTE_LENGTH = 500

Outcome = Literal["created", "requeued", "duplicate"]


@dataclass(frozen=True)
class Job:
    id: int
    type: str
    external_id: str
    metadata: dict[str, Any]
    status: str
    error: str | None
    created_at: str
    updated_at: str
    progress_bytes: int | None = None  # magnet jobs: what the reconciler last saw (#40)
    progress_at: str | None = None  # ... and when that last changed
    priority: int = 0  # 1 = high priority (#51); 0 = a normal download
    note: str | None = None  # a short line about a finished job (#69), e.g. skipped files

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            type=row["type"],
            external_id=row["external_id"],
            metadata=json.loads(row["metadata"]),
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            progress_bytes=row["progress_bytes"],
            progress_at=row["progress_at"],
            priority=row["priority"],
            note=row["note"],
        )


def get_job(db: Database, job_id: int) -> Job | None:
    rows = db.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return Job.from_row(rows[0]) if rows else None


def find_job(db: Database, job_type: str, external_id: str) -> Job | None:
    """The job for a thing, if the system has one (any status)."""
    rows = db.query(
        "SELECT * FROM jobs WHERE type = ? AND external_id = ?", (job_type, external_id)
    )
    return Job.from_row(rows[0]) if rows else None


def submit_job(
    db: Database,
    job_type: str,
    external_id: str,
    metadata_json: str,
    source: str | None = None,
    *,
    retry_failed: bool = True,
    priority: int = 0,
) -> tuple[Job, Outcome]:
    """Create the job for a thing (`job_type`: 'magnet' or 'youtube'), or find the existing one.

    One transaction; the unique index on (type, external_id) decides, so two simultaneous
    submissions create exactly one job.

    `priority` (1 = high, #51) is stored for a NEW job only; changing the priority of an
    existing job is deliberately not a feature. A retried failed job keeps its priority.

    * "created": a new row with status `queued`.
    * "requeued": it was there with status `failed`; it is queued again with the new metadata and
      the old error cleared (submitting a failed download again is a retry).
    * "duplicate": it is already in the system and not failed; nothing changes and the existing
      job is returned. (With `retry_failed=False`, also when it failed: the channel poller must
      not re-queue a video that failed, over and over; retrying one is the user's decision.)
    """
    stored_priority = 1 if priority else 0
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO jobs (type, external_id, metadata, source, priority) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (type, external_id) DO NOTHING",
            (job_type, external_id, metadata_json, source, stored_priority),
        )
        outcome: Outcome = "created" if cursor.rowcount == 1 else "duplicate"
        row = conn.execute(
            "SELECT * FROM jobs WHERE type = ? AND external_id = ?", (job_type, external_id)
        ).fetchone()
        if outcome == "duplicate" and row["status"] == "failed" and retry_failed:
            conn.execute(
                "UPDATE jobs SET status = 'queued', error = NULL, note = NULL, metadata = ?, "
                "source = COALESCE(?, source), progress_bytes = NULL, progress_at = NULL "
                "WHERE id = ?",
                (metadata_json, source, row["id"]),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            outcome = "requeued"
        return Job.from_row(row), outcome


def submit_magnet_job(
    db: Database,
    infohash: str,
    metadata_json: str,
    magnet: str | None = None,
    *,
    priority: int = 0,
) -> tuple[Job, Outcome]:
    """`submit_job` for a magnet link, keyed by its infohash (the link is kept for a retry)."""
    return submit_job(db, "magnet", infohash, metadata_json, magnet, priority=priority)


def claim_next_queued(db: Database, job_type: str) -> Job | None:
    """Take the highest-priority `queued` job of a type and mark it `downloading`.

    High priority wins first, then the oldest (#51). While a high-priority job of ANY type is
    `downloading`, a normal YouTube/podcast job is not claimed at all: it waits in `queued`
    until the high-priority download is done. A high-priority job is always claimable.

    The claim is one transaction, so two workers can never take the same job.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE type = ? AND status = 'queued' "
            "AND (priority > 0 OR NOT EXISTS ("
            " SELECT 1 FROM jobs hi WHERE hi.status = 'downloading' AND hi.priority > 0)) "
            "ORDER BY priority DESC, id LIMIT 1",
            (job_type,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET status = 'downloading' WHERE id = ?", (row["id"],))
        return Job.from_row(
            conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        )


def requeue_interrupted(db: Database, job_type: str) -> int:
    """After a restart: jobs of a type that were `downloading` or `organizing` start again.

    Only for work the app does itself (YouTube); a torrent is qBittorrent's and is handled by
    the reconciler. Returns how many jobs were put back.
    """
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'queued' "
            "WHERE type = ? AND status IN ('downloading', 'organizing')",
            (job_type,),
        )
        return cursor.rowcount


def mark_failed(db: Database, job_id: int, error: str) -> None:
    """Record why a job failed (shown to the user); the text is cut to a sane length."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
            (error[:MAX_ERROR_LENGTH], job_id),
        )


ACTIVE_STATUSES = ("queued", "downloading", "organizing")


def active_magnet_jobs(db: Database) -> list[Job]:
    """Magnet jobs that are not finished (the reconciler's work list), oldest first."""
    rows = db.query(
        "SELECT * FROM jobs WHERE type = 'magnet' "
        "AND status IN ('queued', 'downloading', 'organizing') ORDER BY id"
    )
    return [Job.from_row(row) for row in rows]


def transition(
    db: Database,
    job_id: int,
    to: str,
    *,
    expected: tuple[str, ...],
    error: str | None = None,
    note: str | None = None,
) -> bool:
    """Move a job to status `to` only if it is still in one of the `expected` statuses.

    Returns False if someone else changed it meanwhile (so two actors never both win).
    `error` is stored (cut to length) for `failed`, and cleared for every other status.
    `note` is the short line a finished job may carry (#69); it is stored on `done` and
    cleared on every other transition (a job that runs again starts clean).
    """
    marks = ", ".join("?" for _ in expected)
    with db.transaction() as conn:
        cursor = conn.execute(
            f"UPDATE jobs SET status = ?, error = ?, note = ? WHERE id = ? AND status IN ({marks})",
            (
                to,
                error[:MAX_ERROR_LENGTH] if error else None,
                note[:MAX_NOTE_LENGTH] if note else None,
                job_id,
                *expected,
            ),
        )
        return cursor.rowcount == 1


def record_progress(db: Database, job_id: int, done_bytes: int, at: str) -> None:
    """Remember how much of a torrent exists and when that was last seen to change (#40)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET progress_bytes = ?, progress_at = ? WHERE id = ?",
            (done_bytes, at, job_id),
        )


def _visible(owner_id: int | None) -> tuple[str, tuple[int, ...]]:
    """SQL that limits `jobs` to the ones an account owns (None = no limit: an admin)."""
    if owner_id is None:
        return "", ()
    return " JOIN job_owners o ON o.job_id = jobs.id AND o.account_id = ?", (owner_id,)


def active_jobs(db: Database, owner_id: int | None = None) -> list[Job]:
    """Everything running or waiting, of every type, oldest first (the dashboard)."""
    join, params = _visible(owner_id)
    rows = db.query(
        f"SELECT jobs.* FROM jobs{join} "
        "WHERE jobs.status IN ('queued', 'downloading', 'organizing') ORDER BY jobs.id",
        params,
    )
    return [Job.from_row(row) for row in rows]


def recent_finished(
    db: Database,
    limit: int = 20,
    owner_id: int | None = None,
    viewer_id: int | None = None,
    include_dismissed: bool = True,
) -> list[Job]:
    """The newest done and failed jobs, by when they last changed.

    `viewer_id` + `include_dismissed=False`: leave out the failed jobs that account dismissed.
    """
    join, params = _visible(owner_id)
    hide, hide_params = _not_dismissed(viewer_id, include_dismissed)
    rows = db.query(
        f"SELECT jobs.* FROM jobs{join} WHERE jobs.status IN ('done', 'failed'){hide} "
        "ORDER BY jobs.updated_at DESC, jobs.id DESC LIMIT ?",
        (*params, *hide_params, limit),
    )
    return [Job.from_row(row) for row in rows]


def list_jobs(
    db: Database, *, limit: int = 50, status: str | None = None, owner_id: int | None = None
) -> list[Job]:
    """Jobs, newest first (the job list view). `owner_id`: only the jobs this account owns."""
    join, params = _visible(owner_id)
    where, extra = ("", ()) if status is None else (" WHERE jobs.status = ?", (status,))
    rows = db.query(
        f"SELECT jobs.* FROM jobs{join}{where} ORDER BY jobs.id DESC LIMIT ?",
        (*params, *extra, limit),
    )
    return [Job.from_row(row) for row in rows]


def status_counts(
    db: Database, owner_id: int | None = None, viewer_id: int | None = None
) -> dict[str, int]:
    """How many jobs are in each status (zero for the empty ones).

    With `viewer_id`, `failed` leaves out the failed jobs that account dismissed (the input page's
    red "failed: N" line); `dismissed_count` says how many those are.
    """
    join, params = _visible(owner_id)
    hide, hide_params = _not_dismissed(viewer_id, False)
    counts = dict.fromkeys(("queued", "downloading", "organizing", "done", "failed"), 0)
    for row in db.query(
        f"SELECT jobs.status AS status, COUNT(*) AS n FROM jobs{join} "
        f"WHERE 1 = 1{hide} GROUP BY jobs.status",
        (*params, *hide_params),
    ):
        counts[row["status"]] = row["n"]
    return counts


def _not_dismissed(viewer_id: int | None, include_dismissed: bool) -> tuple[str, tuple[int, ...]]:
    """SQL (to append to a WHERE) hiding the failed jobs `viewer_id` dismissed."""
    if viewer_id is None or include_dismissed:
        return "", ()
    return (
        " AND NOT (jobs.status = 'failed' AND EXISTS (SELECT 1 FROM job_dismissals d "
        "WHERE d.job_id = jobs.id AND d.account_id = ?))",
        (viewer_id,),
    )


def dismissed_ids(db: Database, viewer_id: int, job_ids: list[int]) -> set[int]:
    """Which of these jobs the account has dismissed."""
    if not job_ids:
        return set()
    marks = ", ".join("?" for _ in job_ids)
    rows = db.query(
        f"SELECT job_id FROM job_dismissals WHERE account_id = ? AND job_id IN ({marks})",
        (viewer_id, *job_ids),
    )
    return {row["job_id"] for row in rows}


def dismissed_count(db: Database, owner_id: int | None, viewer_id: int) -> int:
    """How many failed jobs the account may see and has dismissed."""
    join, params = _visible(owner_id)
    rows = db.query(
        f"SELECT COUNT(*) AS n FROM jobs{join} JOIN job_dismissals d "
        "ON d.job_id = jobs.id AND d.account_id = ? WHERE jobs.status = 'failed'",
        (*params, viewer_id),
    )
    return rows[0]["n"]


def dismiss(db: Database, job_id: int, account_id: int) -> bool:
    """Hide a failed job for this account. False if the job is not `failed` (nothing stored)."""
    with db.transaction() as conn:
        failed = conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND status = 'failed'", (job_id,)
        ).fetchone()
        if failed is None:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO job_dismissals (job_id, account_id) VALUES (?, ?)",
            (job_id, account_id),
        )
        return True


def restore(db: Database, job_id: int, account_id: int) -> None:
    """Show a dismissed job again (a no-op when it was not dismissed)."""
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM job_dismissals WHERE job_id = ? AND account_id = ?", (job_id, account_id)
        )


def dismiss_all_failed(db: Database, owner_id: int | None, account_id: int) -> int:
    """Dismiss every failed job the account may see; returns how many were newly dismissed."""
    join, params = _visible(owner_id)
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO job_dismissals (job_id, account_id) "
            f"SELECT jobs.id, ? FROM jobs{join} WHERE jobs.status = 'failed'",
            (account_id, *params),
        )
        return cursor.rowcount


def get_source(db: Database, job_id: int) -> str | None:
    """The stored source of a job (the magnet link). Only the retry reads it."""
    rows = db.query("SELECT source FROM jobs WHERE id = ?", (job_id,))
    return rows[0]["source"] if rows else None


# --- high priority (#51) -------------------------------------------------------------------


def high_priority_downloading(db: Database) -> bool:
    """Is at least one high-priority job downloading right now?"""
    return bool(
        db.query("SELECT 1 FROM jobs WHERE priority > 0 AND status = 'downloading' LIMIT 1")
    )


def normal_magnet_hashes(db: Database) -> list[str]:
    """Infohashes of normal magnet jobs that are queued or downloading (the torrents to pause).

    `queued` is included because qBittorrent starts a torrent as soon as the add call returns,
    while the reconciler only later moves the job to `downloading`; holding those back too is
    what keeps a normal torrent from running beside a high-priority download.
    """
    rows = db.query(
        "SELECT external_id FROM jobs WHERE type = 'magnet' AND priority = 0 "
        "AND status IN ('queued', 'downloading')"
    )
    return [row["external_id"] for row in rows]


def app_paused_hashes(db: Database) -> list[str]:
    """The torrents the app itself paused while a high-priority job was downloading."""
    return [row["infohash"] for row in db.query("SELECT infohash FROM priority_paused")]


def oldest_app_pause_at(db: Database) -> str | None:
    """The stored time of the app's oldest pause (for the watchdog comparison, #51)."""
    rows = db.query("SELECT MIN(paused_at) AS at FROM priority_paused")
    return rows[0]["at"] if rows and rows[0]["at"] else None


def remember_app_paused(db: Database, hashes: list[str]) -> None:
    """Remember torrents the app paused, so only those are ever resumed again."""
    if not hashes:
        return
    with db.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO priority_paused (infohash) VALUES (?)",
            [(value,) for value in hashes],
        )


def forget_app_paused(db: Database, hashes: list[str]) -> None:
    """Drop the record after the app resumed these torrents (or they are gone for good)."""
    if not hashes:
        return
    marks = ", ".join("?" for _ in hashes)
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM priority_paused WHERE infohash IN ({marks})", tuple(hashes))
