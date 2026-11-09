"""High-priority downloads (issue #51): hold normal torrent work back while one runs.

A job created with `high_priority` has `jobs.priority = 1`. While at least one such job is
`downloading`, this scheduled manager pauses the torrents of NORMAL magnet jobs, so the high
priority download gets the limited connection. Once no high-priority job downloads any more, it
resumes exactly the torrents it paused itself.

**Only the app's own pauses are undone.** Before pausing, the manager asks qBittorrent for the
torrents' states and skips every torrent that is already paused: that pause belongs to the user,
and the app must never resume it. The hashes it did pause are stored in `priority_paused` (not in
memory), so a restart in the middle of a high-priority download still resumes them afterwards.
The table only ever holds hashes the app paused; `resume_torrents` is called with exactly those.

**Only running downloads.** A finished torrent (progress 1.0) is left alone even if its job is
still `downloading` in the database (the reconciler has not organized it yet); pausing it would
stop seeding a completed download. YouTube and podcast downloads are never touched: a running
normal download of those is not interrupted (#51 requirement 7), and the queue (`claim_next_queued`)
already stops new normal claims while a high-priority job downloads.

qBittorrent trouble is normal (the VPN reconnects): a failed tick logs once per state change and
tries again on the next run. A pause is only recorded after qBittorrent accepted it, so a lost
connection can never make the app "remember" a pause it did not do.
"""

from __future__ import annotations

import logging

from . import jobs
from .clients.qbittorrent import QbitClient, QbitError
from .db import Database

logger = logging.getLogger(__name__)

PRIORITY_JOB_ID = "priority"
PRIORITY_SECONDS = 5  # how quickly a high-priority download holds the line


class PriorityManager:
    def __init__(self, db: Database, qbit: QbitClient | None):
        self.db = db
        self.qbit = qbit
        self._last_problem: str | None = None

    def tick(self) -> None:
        """One pass of the schedule. Never raises for qBittorrent trouble."""
        if self.qbit is None:
            return
        try:
            if jobs.high_priority_downloading(self.db):
                self._hold_normal_torrents()
            else:
                self._resume_app_paused()
        except QbitError as exc:
            # The VPN is probably reconnecting; the next tick tries again.
            if self._last_problem != str(exc):
                logger.warning(
                    "priority: qBittorrent is not usable, will retry", extra={"error": str(exc)}
                )
                self._last_problem = str(exc)
            return
        if self._last_problem is not None:
            logger.info("priority: qBittorrent is usable again")
            self._last_problem = None

    # ------------------------------------------------------------------ holding back

    def _hold_normal_torrents(self) -> None:
        wanted = set(jobs.normal_magnet_hashes(self.db))
        known = set(jobs.app_paused_hashes(self.db))
        if not wanted - known:
            return
        torrents = {torrent.hash: torrent for torrent in self.qbit.list_torrents()}
        to_pause = sorted(
            infohash
            for infohash in wanted - known
            if (torrent := torrents.get(infohash)) is not None
            and not torrent.paused  # already paused: the user's pause, leave it alone
            and not torrent.finished  # a finished torrent may still be seeding
        )
        if not to_pause:
            return
        self.qbit.pause_torrents(to_pause)
        jobs.remember_app_paused(self.db, to_pause)
        logger.info(
            "priority: paused normal torrents for a high-priority download",
            extra={"torrents": len(to_pause)},
        )

    # ------------------------------------------------------------------ resuming

    def _resume_app_paused(self) -> None:
        hashes = jobs.app_paused_hashes(self.db)
        if not hashes:
            return
        self.qbit.resume_torrents(hashes)
        jobs.forget_app_paused(self.db, hashes)
        logger.info(
            "priority: resumed the torrents the app paused",
            extra={"torrents": len(hashes)},
        )
