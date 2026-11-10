"""Download the whole back catalogue of a podcast feed (issue #45).

A feed lists every episode ever published (hundreds are normal). Putting all of them into the
job queue at once would bury the job list, make the one podcast worker busy for days in front of
everything the family adds meanwhile, and could fill the drive. So a request becomes a **run**:

* `plan` reads the feed and leaves out what is already there: an episode with a job that is
  queued, running or done (a failed one is tried again), or a file of that name in the podcast's
  folder. What is left is stored as the run's items, **newest first**: if the run is stopped
  (full disk, cancelled) what you have is the most recent, which is what most listeners want, and
  Jellyfin sorts by date anyway.
* `BacklogFeeder.tick` (a scheduler job every 10 s) puts the next items into `jobs`, never more
  than `PARALLEL` of all runs together being queued or running, so a single episode someone picks
  by hand waits behind at most two. The episodes then go through the ordinary podcast worker
  (`podcast_jobs.py`), with its progress, retry and Jellyfin refresh.
* Before every episode the feeder checks the free space on the media drive: it must stay above
  `RESERVE_BYTES` (and above the 5 % of the drive that the disk monitor calls an error) after the
  episode. Otherwise the run is **paused** with an alert (`backlog_paused`), and continues by
  itself when space is free again. The same check refuses to start a run that cannot fit.
* The episodes belong to the account(s) that asked (they see the jobs); a subscription's other
  followers do not get them. A second request for the same feed while a run is going joins it.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import accounts, alertlog, jobs
from .config import Settings
from .db import Database
from .podcast_jobs import JOB_TYPE, PodcastWorker, episode_metadata
from .podcasts import Podcast, episode_filename

logger = logging.getLogger(__name__)

PARALLEL = 2  # backlog episodes waiting or downloading, all runs together
RESERVE_BYTES = 5 * 1024**3  # this much stays free after an episode
RESERVE_PERCENT = 5.0  # ... and at least this share of the drive (monitor.DISK_ERROR_PERCENT)
FEEDER_JOB_ID = "podcast-backlog"
FEEDER_SECONDS = 10
KEEP_FINISHED_DAYS = 14


class BacklogExists(Exception):
    """A run for this feed is going already; the asker has been added to it."""

    def __init__(self, backlog_id: int):
        super().__init__(f"backlog {backlog_id} exists")
        self.backlog_id = backlog_id


@dataclass(frozen=True)
class PlannedItem:
    key: str
    metadata_json: str
    size: int | None


@dataclass(frozen=True)
class Plan:
    items: list[PlannedItem]  # newest first
    skipped: int  # already queued, running, done or on disk
    episodes: int  # in the feed
    size_bytes: int  # the known sizes of the items added up
    sized: int  # how many items have a known size

    @property
    def total(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class Backlog:
    id: int
    feed_url: str
    folder: str
    state: str
    total: int
    skipped: int
    size_bytes: int | None
    paused_reason: str | None
    done: int
    failed: int
    waiting: int  # not yet put into the queue
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "folder": self.folder,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "size": self.size_bytes,
            "paused_reason": self.paused_reason,
            "created_at": self.created_at,
        }


# ------------------------------------------------------------------------------------ planning


def _stems(folder: Path) -> set[str]:
    try:
        return {p.stem for p in folder.iterdir() if p.is_file()}
    except OSError:
        return set()


_COPY = re.compile(r" \(\d+\)$")


def on_disk(stems: set[str], published: str, title: str) -> bool:
    """Is there a file for this episode in the podcast's folder? The name is the one the app gives
    (`<date> - <title>`, shortened, with ` (2)` for a clash); the extension does not matter."""
    wanted = episode_filename(datetime.fromisoformat(published).date(), title, "")
    return any(stem == wanted or _COPY.sub("", stem) == wanted for stem in stems)


def _known_jobs(db: Database, keys: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for start in range(0, len(keys), 500):
        chunk = keys[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in db.query(
            f"SELECT external_id, status FROM jobs WHERE type = ? AND external_id IN ({marks})",
            (JOB_TYPE, *chunk),
        ):
            found[row["external_id"]] = row["status"]
    return found


def plan(db: Database, podcast: Podcast, folder: str, library: Path) -> Plan:
    """What a run for this feed would download now (nothing is stored)."""
    known = _known_jobs(db, [e.key for e in podcast.episodes])
    stems = _stems(library / folder)
    items: list[PlannedItem] = []
    skipped = 0
    for episode in podcast.episodes:  # newest first
        metadata = episode_metadata(folder, episode, podcast.feed_url, podcast.image_url)
        status = known.get(episode.key)
        if status in ("queued", "downloading", "organizing", "done") or on_disk(
            stems, metadata.published, metadata.title
        ):
            skipped += 1
            continue
        items.append(PlannedItem(episode.key, metadata.model_dump_json(), episode.size))
    sizes = [i.size for i in items if i.size]
    return Plan(items, skipped, len(podcast.episodes), sum(sizes), len(sizes))


def floor_bytes(total: int) -> int:
    """Free space that must remain after an episode."""
    return max(RESERVE_BYTES, int(total * RESERVE_PERCENT / 100))


def fits(free: int, total: int, needed: int) -> bool:
    return free - needed >= floor_bytes(total)


# ------------------------------------------------------------------------------------ the runs


def _insert_run(db: Database, podcast: Podcast, folder: str, account_id: int, chosen: Plan) -> int:
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO podcast_backlogs (feed_url, folder, total, skipped, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            (podcast.feed_url, folder, chosen.total, chosen.skipped, chosen.size_bytes or None),
        )
        backlog_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "INSERT INTO podcast_backlog_items (backlog_id, position, key, metadata, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (backlog_id, position, item.key, item.metadata_json, item.size)
                for position, item in enumerate(chosen.items)
            ],
        )
        conn.execute(
            "INSERT INTO podcast_backlog_owners (backlog_id, account_id) VALUES (?, ?)",
            (backlog_id, account_id),
        )
    return backlog_id


def start(db: Database, podcast: Podcast, folder: str, account_id: int, chosen: Plan) -> int:
    """Store the run and make `account_id` its owner. Raises BacklogExists (after adding the
    account to the run that is going) if the feed has an unfinished run: the database allows only
    one, so two requests at the same moment cannot both start."""
    for _ in range(3):
        try:
            return _insert_run(db, podcast, folder, account_id, chosen)
        except sqlite3.IntegrityError:
            pass  # an unfinished run for this feed exists (unique index): join it
        joined: int | None = None
        with db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM podcast_backlogs "
                "WHERE feed_url = ? AND state IN ('running', 'paused')",
                (podcast.feed_url,),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO podcast_backlog_owners (backlog_id, account_id) "
                    "VALUES (?, ?)",
                    (existing["id"], account_id),
                )
                joined = int(existing["id"])
        if joined is not None:  # raised outside the transaction: it would roll the join back
            raise BacklogExists(joined)
    raise RuntimeError("could not start the back catalogue")  # the run ended meanwhile, 3 times


_VIEW = """
SELECT b.*,
  (SELECT COUNT(*) FROM podcast_backlog_items i JOIN jobs j
     ON j.type = 'podcast' AND j.external_id = i.key
   WHERE i.backlog_id = b.id AND i.submitted = 1 AND j.status = 'done') AS done,
  (SELECT COUNT(*) FROM podcast_backlog_items i JOIN jobs j
     ON j.type = 'podcast' AND j.external_id = i.key
   WHERE i.backlog_id = b.id AND i.submitted = 1 AND j.status = 'failed') AS failed,
  (SELECT COUNT(*) FROM podcast_backlog_items i
   WHERE i.backlog_id = b.id AND i.submitted = 0) AS waiting
FROM podcast_backlogs b
"""


def _backlog(row: sqlite3.Row) -> Backlog:
    return Backlog(
        id=row["id"],
        feed_url=row["feed_url"],
        folder=row["folder"],
        state=row["state"],
        total=row["total"],
        skipped=row["skipped"],
        size_bytes=row["size_bytes"],
        paused_reason=row["paused_reason"],
        done=row["done"],
        failed=row["failed"],
        waiting=row["waiting"],
        created_at=row["created_at"],
    )


def visible(
    db: Database, account_id: int | None = None, *, finished: bool = False
) -> list[Backlog]:
    """The runs an account may see (None = all, for an admin). By default only the unfinished
    ones and those that ended in the last day, so the dashboard does not keep them for ever."""
    where, params = [], []
    if account_id is not None:
        where.append("b.id IN (SELECT backlog_id FROM podcast_backlog_owners WHERE account_id = ?)")
        params.append(account_id)
    if not finished:
        where.append(
            "(b.state IN ('running', 'paused') "
            "OR b.updated_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day'))"
        )
    sql = _VIEW + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY b.id DESC"
    return [_backlog(row) for row in db.query(sql, tuple(params))]


def get(db: Database, backlog_id: int) -> Backlog | None:
    rows = db.query(_VIEW + " WHERE b.id = ?", (backlog_id,))
    return _backlog(rows[0]) if rows else None


def owns(db: Database, backlog_id: int, account_id: int) -> bool:
    return bool(
        db.query(
            "SELECT 1 FROM podcast_backlog_owners WHERE backlog_id = ? AND account_id = ?",
            (backlog_id, account_id),
        )
    )


def cancel(db: Database, backlog_id: int) -> bool:
    """Stop queuing. Episodes already in the queue finish; True if the run was going."""
    with db.transaction() as conn:
        return (
            conn.execute(
                "UPDATE podcast_backlogs SET state = 'cancelled', paused_reason = NULL, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND state IN ('running', 'paused')",
                (backlog_id,),
            ).rowcount
            == 1
        )


# ------------------------------------------------------------------------------------ the feeder


class BacklogFeeder:
    """Puts the next episodes of the runs into the job queue, a few at a time."""

    def __init__(
        self,
        db: Database,
        worker: PodcastWorker | None,
        settings: Settings,
        *,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        parallel: int = PARALLEL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.db = db
        self.worker = worker
        self.settings = settings
        self._disk_usage = disk_usage
        self.parallel = parallel
        self._now = now

    def tick(self) -> None:
        """One pass (scheduler job). Never raises into the scheduler."""
        try:
            self._tick()
        except Exception:
            logger.exception("podcast back catalogue feeder failed")

    def _waiting(self) -> int:
        return self.db.query(
            "SELECT COUNT(DISTINCT j.id) AS n FROM jobs j JOIN podcast_backlog_items i "
            "ON i.key = j.external_id AND i.submitted = 1 "
            "WHERE j.type = 'podcast' AND j.status IN ('queued', 'downloading', 'organizing')"
        )[0]["n"]

    def _tick(self) -> None:
        self._forget_old()
        waiting = self._waiting()
        queued_any = False
        for run in self.db.query(
            "SELECT * FROM podcast_backlogs WHERE state IN ('running', 'paused') ORDER BY id"
        ):
            while waiting < self.parallel:
                item = self.db.query(
                    "SELECT key, metadata, size_bytes FROM podcast_backlog_items "
                    "WHERE backlog_id = ? AND submitted = 0 ORDER BY position LIMIT 1",
                    (run["id"],),
                )
                if not item:
                    break
                if not self._room_for(run, item[0]["size_bytes"]):
                    break
                self._resume(run)
                self._submit(run, item[0])
                waiting += 1
                queued_any = True
            self._finish_if_done(run["id"])
        if queued_any and self.worker is not None:
            self.worker.wake()

    # ---------------------------------------------------------------- one episode

    def _room_for(self, run: sqlite3.Row, size: int | None) -> bool:
        try:
            usage = self._disk_usage(self.settings.data_root)
        except OSError:
            return False  # the media drive is not mounted: wait, the health check says so
        needed = size or self._average_size(run)
        if fits(usage.free, usage.total, needed):
            return True
        self._pause(run, usage)
        return False

    @staticmethod
    def _average_size(run: sqlite3.Row) -> int:
        return int((run["size_bytes"] or 0) / run["total"]) if run["total"] else 0

    def _submit(self, run: sqlite3.Row, item: sqlite3.Row) -> None:
        job, _ = jobs.submit_job(self.db, JOB_TYPE, item["key"], item["metadata"])
        for owner in self.db.query(
            "SELECT account_id FROM podcast_backlog_owners WHERE backlog_id = ?", (run["id"],)
        ):
            accounts.add_job_owner(self.db, job.id, owner["account_id"])
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE podcast_backlog_items SET submitted = 1 WHERE backlog_id = ? AND key = ?",
                (run["id"], item["key"]),
            )

    # ---------------------------------------------------------------- states

    def _pause(self, run: sqlite3.Row, usage: Any) -> None:
        if run["state"] == "paused":
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE podcast_backlogs SET state = 'paused', paused_reason = 'disk', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (run["id"],),
            )
        view = get(self.db, run["id"])
        free_gb = round(usage.free / 1e9)
        logger.warning("podcast back catalogue paused: disk space", extra={"backlog_id": run["id"]})
        alertlog.add(
            self.db,
            "backlog_paused",
            "warning",
            f"The back catalogue of {run['folder']} is paused",
            f"Only {free_gb} GB are free on the media drive. The remaining episodes are not "
            "queued until there is room again.",
            {
                "podcast": run["folder"],
                "free_gb": free_gb,
                "done": view.done if view else 0,
                "total": run["total"],
            },
            key=f"backlog-paused:{run['id']}:{self._now().strftime('%Y%m%dT%H%M%S')}",
        )

    def _resume(self, run: sqlite3.Row) -> None:
        if run["state"] != "paused":
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE podcast_backlogs SET state = 'running', paused_reason = NULL, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (run["id"],),
            )
        logger.info("podcast back catalogue continues", extra={"backlog_id": run["id"]})

    def _finish_if_done(self, backlog_id: int) -> None:
        left = self.db.query(
            "SELECT "
            "(SELECT COUNT(*) FROM podcast_backlog_items WHERE backlog_id = ? AND submitted = 0) "
            "AS pending, "
            "(SELECT COUNT(DISTINCT j.id) FROM podcast_backlog_items i JOIN jobs j "
            " ON j.type = 'podcast' AND j.external_id = i.key "
            " WHERE i.backlog_id = ? AND i.submitted = 1 "
            " AND j.status IN ('queued', 'downloading', 'organizing')) AS running",
            (backlog_id, backlog_id),
        )[0]
        if left["pending"] == 0 and left["running"] == 0:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE podcast_backlogs SET state = 'done', paused_reason = NULL, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ? AND state IN ('running', 'paused')",
                    (backlog_id,),
                )

    def _forget_old(self) -> None:
        cutoff = (self._now() - timedelta(days=KEEP_FINISHED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM podcast_backlogs WHERE state IN ('done', 'cancelled') "
                "AND updated_at < ?",
                (cutoff,),
            )
