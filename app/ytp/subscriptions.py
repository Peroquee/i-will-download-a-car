"""Reading and writing rows of the `subscriptions` table (issues #9, #19).

A subscription is a YouTube channel the app watches for new videos (the poller is #20). The
channel id is what the RSS feed is keyed by, so it is resolved when someone subscribes and
stored here (the unique index on `channel_id` makes subscribing twice a no-op). `channel_name`
is the library folder the channel's videos go into: it has been through `safe_path_component`.
Every value goes in as a query parameter.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import Database

MAX_SUBSCRIPTIONS = 200
MAX_BACKFILL = 15  # the size of a channel's RSS feed
MAX_ERROR_LENGTH = 500


class TooManySubscriptions(Exception):
    pass


@dataclass(frozen=True)
class Subscription:
    id: int
    channel_id: str
    channel_name: str
    enabled: bool
    backfill: int
    created_at: str
    last_checked: str | None
    last_error: str | None
    target: str = "video"  # "video" or "podcast" (issue #35)
    audio_only: bool = False
    max_height: int | None = None  # new videos of this channel (#65); None = server default
    poll_minutes: int | None = None  # check interval (#79): 0 = paused, None = server default
    poll_at: str | None = None  # start time "HH:MM" (#79): "" = none, None = server default
    poll_mode: str | None = None  # "smart" | "fixed" (#80); None = the server default
    poll_errors: int = 0  # failed checks in a row (#80): the retry waits longer each time
    history_checked_at: str | None = None  # when the publication history was last filled (#80)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Subscription:
        return cls(
            id=row["id"],
            channel_id=row["channel_id"],
            channel_name=row["channel_name"],
            enabled=bool(row["enabled"]),
            backfill=row["backfill"],
            created_at=row["created_at"],
            last_checked=row["last_checked"],
            last_error=row["last_error"],
            target=row["target"],
            audio_only=bool(row["audio_only"]),
            max_height=row["max_height"],
            poll_minutes=row["poll_minutes"],
            poll_at=row["poll_at"],
            poll_mode=row["poll_mode"],
            poll_errors=row["poll_errors"],
            history_checked_at=row["history_checked_at"],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "channel_url": f"https://www.youtube.com/channel/{self.channel_id}",
            "enabled": self.enabled,
            "backfill": self.backfill,
            "created_at": self.created_at,
            "last_checked": self.last_checked,
            "last_error": self.last_error,
            "target": self.target,
            "audio_only": self.audio_only,
            "max_height": self.max_height,
            "poll_minutes": self.poll_minutes,
            "poll_at": self.poll_at,
            "poll_mode": self.poll_mode,
        }


def add(
    db: Database,
    channel_id: str,
    channel_name: str,
    backfill: int = 0,
    target: str = "video",
    audio_only: bool = False,
    max_height: int | None = None,
) -> tuple[Subscription, bool]:
    """Subscribe to a channel. Returns (subscription, created); an existing one is unchanged."""
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM subscriptions WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if existing is not None:
            return Subscription.from_row(existing), False
        count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        if count >= MAX_SUBSCRIPTIONS:
            raise TooManySubscriptions
        cursor = conn.execute(
            "INSERT INTO subscriptions "
            "(channel_id, channel_name, backfill, target, audio_only, max_height) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, channel_name, backfill, target, int(audio_only), max_height),
        )
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return Subscription.from_row(row), True


def get(db: Database, subscription_id: int) -> Subscription | None:
    rows = db.query("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,))
    return Subscription.from_row(rows[0]) if rows else None


def set_max_height(
    db: Database, subscription_id: int, max_height: int | None
) -> Subscription | None:
    """Change the resolution cap for new videos of this channel; None = server default (#65)."""
    with db.transaction() as conn:
        updated = conn.execute(
            "UPDATE subscriptions SET max_height = ? WHERE id = ?",
            (max_height, subscription_id),
        ).rowcount
        if updated != 1:
            return None
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
    return Subscription.from_row(row)


SETTINGS_COLUMNS = ("max_height", "poll_minutes", "poll_at", "poll_mode")


def update_settings(
    db: Database, subscription_id: int, changes: dict[str, object]
) -> Subscription | None:
    """Change the given settings (`SETTINGS_COLUMNS`) of a channel in one step; None for a value
    means "follow the server default" (#79). None is returned if there is no such channel."""
    assert changes
    assert set(changes) <= set(SETTINGS_COLUMNS)
    assignments = ", ".join(f"{column} = ?" for column in changes)
    with db.transaction() as conn:
        updated = conn.execute(
            f"UPDATE subscriptions SET {assignments} WHERE id = ?",
            (*changes.values(), subscription_id),
        ).rowcount
        if updated != 1:
            return None
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
    return Subscription.from_row(row)


def list_all(db: Database) -> list[Subscription]:
    """All subscriptions, by name (case-insensitive), then id."""
    rows = db.query("SELECT * FROM subscriptions ORDER BY channel_name COLLATE NOCASE, id")
    return [Subscription.from_row(row) for row in rows]


def delete(db: Database, subscription_id: int) -> bool:
    """Unsubscribe. Videos already downloaded stay where they are. False if there was none."""
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM publish_history WHERE kind = 'channel' AND subscription_id = ?",
            (subscription_id,),
        )  # ids can be reused: a new channel must not inherit the old one's history (#80)
        return (
            conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,)).rowcount == 1
        )


def record_poll(
    db: Database, subscription_id: int, error: str | None = None, when: datetime | None = None
) -> None:
    """Note that the channel was just polled: `last_checked` = now (or `when`), `last_error` =
    the reason (None clears it). Used by the poller (#20)."""
    message = None if error is None else error[:MAX_ERROR_LENGTH]
    with db.transaction() as conn:
        if when is None:
            stamp = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            conn.execute(
                f"UPDATE subscriptions SET last_checked = {stamp}, last_error = ?, "
                "poll_errors = CASE WHEN ? IS NULL THEN 0 ELSE poll_errors + 1 END WHERE id = ?",
                (message, message, subscription_id),
            )
        else:
            text = (
                when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{when.microsecond // 1000:03d}Z"
            )
            conn.execute(
                "UPDATE subscriptions SET last_checked = ?, last_error = ?, "
                "poll_errors = CASE WHEN ? IS NULL THEN 0 ELSE poll_errors + 1 END WHERE id = ?",
                (text, message, message, subscription_id),
            )


def mark_history_checked(db: Database, subscription_id: int, when: datetime) -> None:
    """Note that the publication history was just filled from the channel's RSS feed (#80)."""
    text = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET history_checked_at = ? WHERE id = ?",
            (text, subscription_id),
        )
