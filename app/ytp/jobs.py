"""Reading and writing rows of the `jobs` table (issues #9, #13).

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
        )


def get_job(db: Database, job_id: int) -> Job | None:
    rows = db.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return Job.from_row(rows[0]) if rows else None


def submit_magnet_job(db: Database, infohash: str, metadata_json: str) -> tuple[Job, Outcome]:
    """Create the job for a magnet, or find the one that already exists. One transaction.

    * "created": a new row with status `queued`.
    * "requeued": the infohash was there with status `failed`; it is queued again with the new
      metadata and the old error cleared (submitting a failed download again is a retry).
    * "duplicate": the infohash is already in the system and not failed; nothing changes and the
      existing job is returned.
    """
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO jobs (type, external_id, metadata) VALUES ('magnet', ?, ?) "
            "ON CONFLICT (type, external_id) DO NOTHING",
            (infohash, metadata_json),
        )
        outcome: Outcome = "created" if cursor.rowcount == 1 else "duplicate"
        row = conn.execute(
            "SELECT * FROM jobs WHERE type = 'magnet' AND external_id = ?", (infohash,)
        ).fetchone()
        if outcome == "duplicate" and row["status"] == "failed":
            conn.execute(
                "UPDATE jobs SET status = 'queued', error = NULL, metadata = ? WHERE id = ?",
                (metadata_json, row["id"]),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            outcome = "requeued"
        return Job.from_row(row), outcome


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
