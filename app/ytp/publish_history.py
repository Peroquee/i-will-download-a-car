"""When a subscription's items were published (issue #80): the data the pattern learner reads.

One row per item (a video, a podcast episode), unique per (kind, subscription, item), the newest
`KEEP` rows per subscription. Rows only ever come from times a feed or a metadata look gave
(never guessed). A time that is just a date (yt-dlp's `upload_date` stands in as 23:59:59 UTC, see
`video_filter._published`) carries no time of day and is not stored: it would teach a wrong hour.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from .db import Database
from .timeutil import now_text, parse_time

KEEP = 100  # per subscription
KINDS = ("channel", "podcast")


def _stamp(moment: datetime) -> str:
    return now_text(moment)


def has_time_of_day(moment: datetime) -> bool:
    """False for the date-only stand-in (23:59:59 UTC) and for midnight, which is how a feed that
    only knows the date writes it."""
    utc = moment.astimezone(UTC)
    return (utc.hour, utc.minute, utc.second) not in {(23, 59, 59), (0, 0, 0)}


def record(
    db: Database, kind: str, subscription_id: int, items: Iterable[tuple[str, datetime]]
) -> int:
    """Remember the publication times of these items (`(item_id, published)`). Known items are
    left as they are; returns how many were new. Keeps the newest `KEEP`."""
    assert kind in KINDS
    rows = [(item, _stamp(when)) for item, when in items if item and has_time_of_day(when)]
    if not rows:
        return 0
    added = 0
    with db.transaction() as conn:
        for item, stamp in rows:
            added += conn.execute(
                "INSERT OR IGNORE INTO publish_history (kind, subscription_id, item_id, "
                "published_at) VALUES (?, ?, ?, ?)",
                (kind, subscription_id, item, stamp),
            ).rowcount
        if added:
            conn.execute(
                "DELETE FROM publish_history WHERE kind = ? AND subscription_id = ? AND "
                "item_id NOT IN (SELECT item_id FROM publish_history WHERE kind = ? AND "
                "subscription_id = ? ORDER BY published_at DESC, item_id LIMIT ?)",
                (kind, subscription_id, kind, subscription_id, KEEP),
            )
    return added


def times(db: Database, kind: str, subscription_id: int) -> list[datetime]:
    """The publication times, newest first (aware UTC)."""
    rows = db.query(
        "SELECT published_at FROM publish_history WHERE kind = ? AND subscription_id = ? "
        "ORDER BY published_at DESC",
        (kind, subscription_id),
    )
    return [parse_time(row["published_at"]) for row in rows]


def count(db: Database, kind: str, subscription_id: int) -> int:
    rows = db.query(
        "SELECT COUNT(*) AS n FROM publish_history WHERE kind = ? AND subscription_id = ?",
        (kind, subscription_id),
    )
    return int(rows[0]["n"])


def forget(db: Database, kind: str, subscription_id: int) -> None:
    """Drop everything of a subscription that was removed."""
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM publish_history WHERE kind = ? AND subscription_id = ?",
            (kind, subscription_id),
        )
