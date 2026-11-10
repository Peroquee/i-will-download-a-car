"""Rows of the `podcast_subscriptions` table (issue #34): the feeds that are polled.

Same shape as `subscriptions` (YouTube channels): `title` is the library folder name (it has been
through `safe_path_component`), `feed_url` the normalised address (unique, so subscribing twice is
a no-op). Every value goes in as a query parameter.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import Database

MAX_PODCASTS = 100
MAX_BACKFILL = 20
MAX_ERROR_LENGTH = 500


class TooManyPodcasts(Exception):
    pass


@dataclass(frozen=True)
class PodcastSubscription:
    id: int
    feed_url: str
    title: str
    enabled: bool
    backfill: int
    created_at: str
    last_checked: str | None
    last_error: str | None
    poll_minutes: int | None = None  # check interval (#79): 0 = paused, None = server default
    poll_at: str | None = None  # start time "HH:MM" (#79): "" = none, None = server default

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PodcastSubscription:
        return cls(
            id=row["id"],
            feed_url=row["feed_url"],
            title=row["title"],
            enabled=bool(row["enabled"]),
            backfill=row["backfill"],
            created_at=row["created_at"],
            last_checked=row["last_checked"],
            last_error=row["last_error"],
            poll_minutes=row["poll_minutes"],
            poll_at=row["poll_at"],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": "podcast",
            "feed_url": self.feed_url,
            "title": self.title,
            "enabled": self.enabled,
            "backfill": self.backfill,
            "created_at": self.created_at,
            "last_checked": self.last_checked,
            "last_error": self.last_error,
            "poll_minutes": self.poll_minutes,
            "poll_at": self.poll_at,
        }


def add(
    db: Database, feed_url: str, title: str, backfill: int = 0
) -> tuple[PodcastSubscription, bool]:
    """Subscribe. Returns (subscription, created); an existing one is unchanged."""
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM podcast_subscriptions WHERE feed_url = ?", (feed_url,)
        ).fetchone()
        if existing is not None:
            return PodcastSubscription.from_row(existing), False
        count = conn.execute("SELECT COUNT(*) FROM podcast_subscriptions").fetchone()[0]
        if count >= MAX_PODCASTS:
            raise TooManyPodcasts
        cursor = conn.execute(
            "INSERT INTO podcast_subscriptions (feed_url, title, backfill) VALUES (?, ?, ?)",
            (feed_url, title, backfill),
        )
        row = conn.execute(
            "SELECT * FROM podcast_subscriptions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return PodcastSubscription.from_row(row), True


def get(db: Database, subscription_id: int) -> PodcastSubscription | None:
    rows = db.query("SELECT * FROM podcast_subscriptions WHERE id = ?", (subscription_id,))
    return PodcastSubscription.from_row(rows[0]) if rows else None


SETTINGS_COLUMNS = ("poll_minutes", "poll_at")


def update_settings(
    db: Database, subscription_id: int, changes: dict[str, object]
) -> PodcastSubscription | None:
    """Change the given settings (`SETTINGS_COLUMNS`) of a feed in one step; None for a value
    means "follow the server default" (#79). None is returned if there is no such feed."""
    assert changes
    assert set(changes) <= set(SETTINGS_COLUMNS)
    assignments = ", ".join(f"{column} = ?" for column in changes)
    with db.transaction() as conn:
        updated = conn.execute(
            f"UPDATE podcast_subscriptions SET {assignments} WHERE id = ?",
            (*changes.values(), subscription_id),
        ).rowcount
        if updated != 1:
            return None
        row = conn.execute(
            "SELECT * FROM podcast_subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
    return PodcastSubscription.from_row(row)


def list_all(db: Database) -> list[PodcastSubscription]:
    rows = db.query("SELECT * FROM podcast_subscriptions ORDER BY title COLLATE NOCASE, id")
    return [PodcastSubscription.from_row(row) for row in rows]


def delete(db: Database, subscription_id: int) -> bool:
    """Unsubscribe. Episodes already downloaded stay where they are. False if there was none."""
    with db.transaction() as conn:
        cursor = conn.execute("DELETE FROM podcast_subscriptions WHERE id = ?", (subscription_id,))
        return cursor.rowcount == 1


def record_poll(
    db: Database, subscription_id: int, error: str | None = None, when: datetime | None = None
) -> None:
    """Note that the feed was just polled: `last_checked` = now (or `when`), `last_error` = the
    reason (None clears it)."""
    message = None if error is None else error[:MAX_ERROR_LENGTH]
    moment = when or datetime.now(UTC)
    stamp = (
        moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE podcast_subscriptions SET last_checked = ?, last_error = ? WHERE id = ?",
            (stamp, message, subscription_id),
        )
