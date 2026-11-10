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

**The VPN watchdog's pause wins.** The watchdog (#21) pauses ALL torrents when the VPN drops and,
with `WATCHDOG_RESUME_AFTER=0` (the default), leaves them paused until the owner resumes them by
hand. The manager therefore never resumes while the watchdog is not healthy, and when the
watchdog's own pause is newer than the app's it forgets its rows WITHOUT resuming: the watchdog's
safety policy owns those torrents. The same applies to a confirmed egress mismatch, which the
watchdog never auto-resumes either. With no status client the manager behaves as before.

**A watchdog pause is noticed during a hold, too (#66).** The status is read at the start of a
tick whenever the app has paused rows, not only on the resume path: an outage that starts AND ends
while a high-priority download still runs would otherwise never be seen (the watchdog clears its
`paused` flag when it gets healthy again), and the manager would later resume against the "stay
paused" policy. The status is read once per tick and passed on, so the resume path does not fetch
it a second time.

**A stuck high-priority torrent must not freeze the normal queues (#70).** A high-priority MAGNET
job only holds the line while qBittorrent is really transferring its torrent. Paused and queued
torrents release it at once; a torrent qBittorrent calls stalled (or checking/moving) keeps it for
a short grace period (`HOLD_GRACE_SECONDS`), because that can be a momentary hiccup. The answer is
stored through `jobs.set_magnet_transferring`, where `claim_next_queued` and the hold/resume
decision below read it. Without that flag (no qBittorrent configured, or before the first tick) a
magnet never holds: better to start the normal queue than to freeze it forever.

qBittorrent trouble is normal (the VPN reconnects): a failed tick logs once per state change and
tries again on the next run. A pause is only recorded after qBittorrent accepted it, so a lost
connection can never make the app "remember" a pause it did not do.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from . import jobs
from .clients.qbittorrent import QbitClient, QbitError
from .clients.watchdog import WatchdogStatusClient
from .db import Database

logger = logging.getLogger(__name__)

PRIORITY_JOB_ID = "priority"
PRIORITY_SECONDS = 5  # how quickly a high-priority download holds the line
# How long a high-priority torrent that just stopped moving (stalled, checking, moving) still
# holds the line: a short hiccup must not start the normal queue, a dead torrent must not freeze
# it for days (#70).
HOLD_GRACE_SECONDS = 120.0


def _parse_time(value: object) -> datetime | None:
    """An ISO timestamp (the watchdog's seconds or SQLite's milliseconds) as aware UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class PriorityManager:
    def __init__(
        self,
        db: Database,
        qbit: QbitClient | None,
        watchdog: WatchdogStatusClient | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.db = db
        self.qbit = qbit
        self.watchdog = watchdog
        self._clock = clock
        # When each high-priority torrent was last seen transferring (for the stall grace).
        self._last_transfer: dict[str, float] = {}
        self._last_problem: str | None = None

    def tick(self) -> None:
        """One pass of the schedule. Never raises for qBittorrent trouble."""
        if self.qbit is None:
            return
        try:
            self._update_magnet_transferring()
            # Read the watchdog once at the start (#66): its pause may start and end while a
            # high-priority download still runs, so the resume path alone would miss it.
            paused = jobs.app_paused_hashes(self.db)
            status: dict[str, object] | None = None
            if self.watchdog is not None and paused:
                status = self._watchdog_status()
                reason = self._watchdog_owns_the_pause(status)
                if reason is not None:
                    self._leave_to_watchdog(paused, reason)
                    paused = []
            if jobs.high_priority_downloading(self.db):
                self._hold_normal_torrents()
            else:
                self._resume_app_paused(paused, status)
        except QbitError as exc:
            # Cannot see the torrents: do not let a stale "transferring" hold the queue.
            self._store_transferring(False)
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

    # ------------------------------------------------------------------ does a magnet hold?

    def _update_magnet_transferring(self) -> None:
        """Store whether a high-priority magnet is really moving data (#70).

        Paused and queued torrents release the line at once. A torrent that just stopped
        moving (stalled by qBittorrent, being checked or moved) keeps it for
        `HOLD_GRACE_SECONDS`. Nothing is asked of qBittorrent when no high-priority magnet is
        downloading.
        """
        hashes = jobs.high_priority_magnet_hashes(self.db)
        if not hashes:
            self._last_transfer.clear()
            self._store_transferring(False)
            return
        torrents = {t.hash: t for t in self.qbit.list_torrents(hashes=hashes)}
        now = self._clock()
        moving = False
        for infohash in hashes:
            torrent = torrents.get(infohash)
            if torrent is not None and torrent.transferring:
                self._last_transfer[infohash] = now
                moving = True
            elif torrent is not None and (torrent.paused or torrent.queued):
                # Deliberately not transferring (the user, the watchdog or qBittorrent's
                # queue): the normal work must not wait for it.
                self._last_transfer.pop(infohash, None)
            elif (last := self._last_transfer.get(infohash)) is not None and (
                now - last <= HOLD_GRACE_SECONDS
            ):
                moving = True  # a short stall: keep the line, it may start again any moment
            else:
                self._last_transfer.pop(infohash, None)
        self._store_transferring(moving)

    def _store_transferring(self, value: bool) -> None:
        """Write the flag only when it changes (this ticks every five seconds)."""
        if jobs.magnet_transferring(self.db) != value:
            jobs.set_magnet_transferring(self.db, value)

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

    def _resume_app_paused(self, hashes: list[str], status: dict[str, object] | None) -> None:
        """Resume the app's own pauses, unless the watchdog's policy owns them.

        `status` is the watchdog answer `tick` already read (None without a watchdog or when
        there were no rows to compare), so the status is never fetched twice in one tick (#66).
        """
        if not hashes:
            return
        if status is not None:
            if status.get("state") != "ok":
                # The watchdog is not healthy (VPN down, blind, starting, or not answering):
                # never undo a pause while it is protecting the stack. If its own pause is in
                # force, it now owns these torrents; otherwise keep the rows and try next tick.
                if self._watchdog_paused_after_us(status):
                    self._leave_to_watchdog(hashes, "the watchdog paused during an outage")
                return
            reason = self._watchdog_owns_the_pause(status)
            if reason is not None:
                self._leave_to_watchdog(hashes, reason)
                return
        self.qbit.resume_torrents(hashes)
        jobs.forget_app_paused(self.db, hashes)
        logger.info(
            "priority: resumed the torrents the app paused",
            extra={"torrents": len(hashes)},
        )

    def _leave_to_watchdog(self, hashes: list[str], reason: str) -> None:
        """Forget our rows without resuming: the watchdog's pause policy now owns the torrents."""
        jobs.forget_app_paused(self.db, hashes)
        logger.info(
            "priority: torrents stay paused, %s",
            reason,
            extra={"torrents": len(hashes)},
        )

    def _watchdog_status(self) -> dict[str, object] | None:
        """The watchdog's status, or None when no watchdog is configured.

        A status client never raises in production; a broken one is treated as `unknown`,
        which never resumes anything.
        """
        if self.watchdog is None:
            return None
        try:
            status = self.watchdog.fetch_status()
        except Exception:
            logger.warning("priority: the watchdog status could not be read", exc_info=True)
            return {"state": "unknown"}
        return status if isinstance(status, dict) else {"state": "unknown"}

    def _watchdog_owns_the_pause(self, status: dict[str, object]) -> str | None:
        """Why the watchdog's pause policy owns the app's paused torrents, or None (#66).

        A pause delivered after the app's own pause, or a confirmed egress mismatch (which the
        watchdog never auto-resumes), means the app must only forget its rows, never resume.
        """
        if status.get("egress") == "mismatch":
            return "the watchdog paused after an egress mismatch"
        if self._watchdog_paused_after_us(status):
            return "the watchdog paused during an outage"
        return None

    def _watchdog_paused_after_us(self, status: dict[str, object]) -> bool:
        """Did the watchdog's own pause start after the app paused its torrents?

        The status has `paused` (a pause was delivered during the current outage) and `since`
        (when the watchdog entered that state; ISO with SECOND precision). `paused_at` in the
        database has millisecond precision, so a watchdog pause in the same second as the app's
        pause counts as "after": never resume a torrent the watchdog may have paused. When the
        times cannot be compared, the reported watchdog pause wins as well.
        """
        if status.get("paused") is not True:
            return False
        since = _parse_time(status.get("since"))
        oldest = _parse_time(jobs.oldest_app_pause_at(self.db))
        if since is None or oldest is None:
            return True  # cannot decide: the safe rule is that the watchdog owns the pause
        return since + timedelta(seconds=1) > oldest
