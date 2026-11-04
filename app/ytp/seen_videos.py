"""Videos of a subscribed channel that a poll has settled (see migration 0012).

A row means "this video was looked at completely: it was queued, skipped on purpose, or is older
than the subscription". The poller walks a channel's list from the newest video and stops at the
first video in this set; everything below it is settled too.
"""

from __future__ import annotations

from .db import Database

KEEP = 300  # per subscription; a channel list is 30 videos long


def ids(db: Database, subscription_id: int) -> set[str]:
    rows = db.query(
        "SELECT video_id FROM subscription_seen_videos WHERE subscription_id = ?",
        (subscription_id,),
    )
    return {row["video_id"] for row in rows}


def clear(db: Database, subscription_id: int) -> None:
    """Forget what was settled: the next poll walks the list again from the newest video. Used when
    somebody new follows the channel, so the videos that are already queued become theirs too."""
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM subscription_seen_videos WHERE subscription_id = ?", (subscription_id,)
        )


def add(db: Database, subscription_id: int, video_ids: list[str]) -> None:
    """Remember these videos (asking again changes nothing) and forget the oldest beyond KEEP."""
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO subscription_seen_videos (subscription_id, video_id) VALUES (?, ?) "
            "ON CONFLICT (subscription_id, video_id) DO NOTHING",
            [(subscription_id, video_id) for video_id in video_ids],
        )
        conn.execute(
            "DELETE FROM subscription_seen_videos WHERE subscription_id = ? AND id NOT IN "
            "(SELECT id FROM subscription_seen_videos WHERE subscription_id = ? "
            "ORDER BY id DESC LIMIT ?)",
            (subscription_id, subscription_id, KEEP),
        )
