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
) -> tuple[Job, Outcome]:
    """Create the job for a thing (`job_type`: 'magnet' or 'youtube'), or find the existing one.

    One transaction; the unique index on (type, external_id) decides, so two simultaneous
    submissions create exactly one job.

    * "created": a new row with status `queued`.
    * "requeued": it was there with status `failed`; it is queued again with the new metadata and
      the old error cleared (submitting a failed download again is a retry).
    * "duplicate": it is already in the system and not failed; nothing changes and the existing
      job is returned. (With `retry_failed=False`, also when it failed: the channel poller must
      not re-queue a video that failed, over and over; retrying one is the user's decision.)
    """
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO jobs (type, external_id, metadata, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (type, external_id) DO NOTHING",
            (job_type, external_id, metadata_json, source),
        )
        outcome: Outcome = "created" if cursor.rowcount == 1 else "duplicate"
        row = conn.execute(
            "SELECT * FROM jobs WHERE type = ? AND external_id = ?", (job_type, external_id)
        ).fetchone()
        if outcome == "duplicate" and row["status"] == "failed" and retry_failed:
            conn.execute(
                "UPDATE jobs SET status = 'queued', error = NULL, metadata = ?, "
                "source = COALESCE(?, source), progress_bytes = NULL, progress_at = NULL "
                "WHERE id = ?",
                (metadata_json, source, row["id"]),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            outcome = "requeued"
        return Job.from_row(row), outcome


def submit_magnet_job(
    db: Database, infohash: str, metadata_json: str, magnet: str | None = None
) -> tuple[Job, Outcome]:
    """`submit_job` for a magnet link, keyed by its infohash (the link is kept for a retry)."""
    return submit_job(db, "magnet", infohash, metadata_json, magnet)


def claim_next_queued(db: Database, job_type: str) -> Job | None:
    """Take the oldest `queued` job of a type and mark it `downloading`; None if there is none.

    The claim is one transaction, so two workers can never take the same job.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE type = ? AND status = 'queued' ORDER BY id LIMIT 1",
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
    db: Database, job_id: int, to: str, *, expected: tuple[str, ...], error: str | None = None
) -> bool:
    """Move a job to status `to` only if it is still in one of the `expected` statuses.

    Returns False if someone else changed it meanwhile (so two actors never both win).
    `error` is stored (cut to length) for `failed`, and cleared for every other status.
    """
    marks = ", ".join("?" for _ in expected)
    with db.transaction() as conn:
        cursor = conn.execute(
            f"UPDATE jobs SET status = ?, error = ? WHERE id = ? AND status IN ({marks})",
            (to, error[:MAX_ERROR_LENGTH] if error else None, job_id, *expected),
        )
        return cursor.rowcount == 1


def record_progress(db: Database, job_id: int, done_bytes: int, at: str) -> None:
    """Remember how much of a torrent exists and when that was last seen to change (#40)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET progress_bytes = ?, progress_at = ? WHERE id = ?",
            (done_bytes, at, job_id),
        )


def list_jobs(db: Database, *, limit: int = 50, status: str | None = None) -> list[Job]:
    """Jobs, newest first (the job list view)."""
    if status is None:
        rows = db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = db.query(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        )
    return [Job.from_row(row) for row in rows]


def status_counts(db: Database) -> dict[str, int]:
    """How many jobs are in each status (zero for the empty ones)."""
    counts = dict.fromkeys(("queued", "downloading", "organizing", "done", "failed"), 0)
    for row in db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        counts[row["status"]] = row["n"]
    return counts


def get_source(db: Database, job_id: int) -> str | None:
    """The stored source of a job (the magnet link). Only the retry reads it."""
    rows = db.query("SELECT source FROM jobs WHERE id = ?", (job_id,))
    return rows[0]["source"] if rows else None
