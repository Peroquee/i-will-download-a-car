"""Videos the channel poller skipped on purpose: lives, premieres, Shorts (issue #39).

Plain functions over `ytp.db.Database`. A row means "judged, not queued, do not look again".
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Database

REASONS = ("live", "premiere", "short")
RECENT = 5


@dataclass(frozen=True)
class SkippedVideo:
    video_id: str
    reason: str
    title: str
    published: str | None
    skipped_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "video_id": self.video_id,
            "reason": self.reason,
            "title": self.title,
            "published": self.published,
            "skipped_at": self.skipped_at,
        }


def record(
    db: Database,
    subscription_id: int,
    video_id: str,
    reason: str,
    title: str,
    published: str | None,
) -> None:
    """Remember a skipped video (once: asking again changes nothing)."""
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO skipped_videos (subscription_id, video_id, reason, title, published) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (subscription_id, video_id) DO NOTHING",
            (subscription_id, video_id, reason, title[:300], published),
        )


def skipped_ids(db: Database, subscription_id: int) -> set[str]:
    rows = db.query(
        "SELECT video_id FROM skipped_videos WHERE subscription_id = ?", (subscription_id,)
    )
    return {row["video_id"] for row in rows}


def summary(db: Database, subscription_id: int) -> dict[str, object]:
    """`{"count", "by_reason": {...}, "recent": [newest few]}` for the subscription's details."""
    counts = {reason: 0 for reason in REASONS}
    for row in db.query(
        "SELECT reason, COUNT(*) AS n FROM skipped_videos "
        "WHERE subscription_id = ? GROUP BY reason",
        (subscription_id,),
    ):
        counts[row["reason"]] = row["n"]
    recent = db.query(
        "SELECT * FROM skipped_videos WHERE subscription_id = ? ORDER BY id DESC LIMIT ?",
        (subscription_id, RECENT),
    )
    return {
        "count": sum(counts.values()),
        "by_reason": counts,
        "recent": [
            SkippedVideo(
                r["video_id"], r["reason"], r["title"], r["published"], r["skipped_at"]
            ).as_dict()
            for r in recent
        ],
    }
