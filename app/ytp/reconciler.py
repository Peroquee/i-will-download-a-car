"""The completion reconciler (issue #15): turns finished torrents into organized files.

A scheduled job (every 30 s) compares the unfinished magnet jobs in the database with what
qBittorrent reports, and moves each job along:

    queued ──► downloading ──► organizing ──► done
       └───────────┴───────────────┴────────► failed (with a readable reason)

It polls instead of using qBittorrent's "run on completion" hook: that hook has a record of
silent failures, and it would run inside gluetun's network namespace and need a hole in the VPN
firewall. Polling needs nothing but the Web API the stack already uses.

**Idempotent, so safe after downtime.** Every run starts from what is true NOW: a torrent that
finished while the app was down is simply found complete on the first run. A job is claimed for
organizing with a conditional UPDATE, so nothing is done twice, and organizing itself (#16) must
be repeatable (hardlinks that already exist are not an error).

**Never stuck in `organizing`.** The organizer runs inside `try`: an `OrganizeError` (or any
other exception) turns the job `failed` with a message a person can read. If the app dies in the
middle, the job is left `organizing`; the next run finds such a job (nothing else is organizing
at the start of a run) and puts it back to be organized again.

**qBittorrent being away is normal** (the VPN reconnects): the run does nothing, logs the outage
once and the recovery once, and the schedule carries on. Jobs are not failed because of it.

**Giving up (issue #40).** A magnet link nobody seeds sits in qBittorrent forever, either stuck
without its file list or stopped at some percentage. When a torrent shows no progress for
`MAGNET_STALL_DAYS` (default 7; 0 = never) the job fails with "no sources for 7 days", and the
torrent and its unfinished files are removed from qBittorrent. Progress (more bytes than before)
restarts the clock, so a slow download is never cut off. Only time in which qBittorrent was really
trying counts: a torrent that is paused (by hand or by the VPN watchdog), queued by qBittorrent,
being checked or moved restarts the clock too, and so does downtime of the app or of qBittorrent
(the run after a gap of more than `GAP_SECONDS`). The files are only deleted when they are inside
`downloads/`, and a torrent that has finished is never looked at, so library files are out of
reach. The failed job stays retryable (#26): the retry adds the magnet link again and the clock
starts from zero.

The organizer is passed in (`organize(job, torrent)`), so the file work of #16 plugs in without
touching this module; a returned string becomes the job's note when it finishes `done` although
some files were skipped (#69). Without an organizer, finished torrents are left as `downloading`
and the log says so once; nothing is lost and a later run organizes them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import jobs
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError, QbitUnavailable, Torrent
from .db import Database
from .jobs import Job
from .scheduler import get_state, set_state

logger = logging.getLogger(__name__)

RECONCILE_JOB_ID = "reconcile"
RECONCILE_SECONDS = 30
KEY_LAST_OK = "reconciler.last_ok"
# A job is written BEFORE the torrent is added (#13). If the torrent is still unknown to
# qBittorrent after this long, the add never happened (the app stopped in between).
QUEUED_GRACE_SECONDS = 120
# Progress is written at most this often per job (the stall limit is days, minutes do not matter).
PROGRESS_WRITE_SECONDS = 600
# A gap between two runs longer than this (the app or qBittorrent was away) restarts the clocks.
GAP_SECONDS = 600


class OrganizeError(Exception):
    """An organizer's way to say what went wrong, in words a person can read (the job error)."""


Organizer = Callable[[Job, Torrent], str | None]  # a note for a `done` job with skipped files (#69)


@dataclass
class RunResult:
    """What one run did; for tests and the log."""

    checked: int = 0
    started: list[int] = field(default_factory=list)  # queued -> downloading
    done: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    gave_up: list[int] = field(
        default_factory=list
    )  # failed for lack of sources (subset of failed)
    requeued: list[int] = field(default_factory=list)  # leftover `organizing` taken up again
    skipped: str | None = None  # why nothing was done (qBittorrent away)


class Reconciler:
    def __init__(
        self,
        db: Database,
        qbit: QbitClient,
        organize: Organizer | None = None,
        *,
        on_organized: Callable[[], None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        queued_grace_seconds: float = QUEUED_GRACE_SECONDS,
        stall_seconds: float | None = None,
        downloads_dir: Path | None = None,
        gap_seconds: float = GAP_SECONDS,
    ):
        self.db = db
        self.qbit = qbit
        self.organize = organize
        self._on_organized = on_organized  # the dashboard's storage sizes want a new walk (#82)
        self._now = now
        self._queued_grace = queued_grace_seconds
        self._stall_seconds = stall_seconds if stall_seconds and stall_seconds > 0 else None
        self._downloads_dir = downloads_dir
        self._gap_seconds = gap_seconds
        self._previous_ok: datetime | None | bool = False  # False = not read yet
        self._after_gap = False
        self._qbit_away = False  # we have already logged the current outage
        self._warned_no_organizer = False
        self._auth_reported = False

    # ------------------------------------------------------------------ the run

    def run_once(self) -> RunResult:
        """One pass. Never raises for qBittorrent trouble or for a single bad job."""
        result = RunResult()
        try:
            torrents = {t.hash: t for t in self.qbit.list_torrents()}
        except QbitUnavailable as exc:
            result.skipped = "qBittorrent unreachable"
            if not self._qbit_away:
                logger.warning(
                    "reconciler: qBittorrent is away, waiting", extra={"error": str(exc)}
                )
            self._qbit_away = True
            return result
        except QbitAuthError as exc:
            result.skipped = "qBittorrent login refused"
            if not self._auth_reported:
                logger.error("reconciler: cannot log in to qBittorrent", extra={"error": str(exc)})
                self._auth_reported = True
            return result
        except QbitError as exc:
            result.skipped = "qBittorrent error"
            logger.error("reconciler: qBittorrent error", extra={"error": str(exc)})
            return result
        if self._qbit_away or self._auth_reported:
            logger.info("reconciler: qBittorrent is reachable again")
        self._qbit_away = self._auth_reported = False

        self._after_gap = self._gap_before_this_run()

        # Anything still `organizing` now was left behind by a run that did not finish
        # (nothing organizes concurrently: jobs never overlap themselves).
        for job in jobs.active_magnet_jobs(self.db):
            if job.status == "organizing" and jobs.transition(
                self.db, job.id, "downloading", expected=("organizing",)
            ):
                logger.warning(
                    "reconciler: organizing was interrupted, trying again", extra={"job_id": job.id}
                )
                result.requeued.append(job.id)

        for job in jobs.active_magnet_jobs(self.db):
            result.checked += 1
            try:
                self._reconcile(job, torrents.get(job.external_id), result)
            except Exception:  # one bad job must not stop the others
                logger.exception(
                    "reconciler: job could not be reconciled", extra={"job_id": job.id}
                )
        set_state(self.db, KEY_LAST_OK, self._now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        return result

    # ------------------------------------------------------------------ one job

    def _reconcile(self, job: Job, torrent: Torrent | None, result: RunResult) -> None:
        if torrent is None:
            self._missing(job, result)
            return
        if torrent.failed:
            self._fail(
                job, f"qBittorrent reports the torrent as '{torrent.state}'"
                + (" (its files are gone)" if torrent.state == "missingFiles" else ""),
                result,
            )  # fmt: skip
            return
        if torrent.ready_to_organize:
            self._complete(job, torrent, result)
            return
        if job.status == "queued" and jobs.transition(
            self.db, job.id, "downloading", expected=("queued",)
        ):
            result.started.append(job.id)
        self._watch_progress(job, torrent, result)

    # ------------------------------------------------------------------ giving up (#40)

    def _gap_before_this_run(self) -> bool:
        """Was the last successful run long ago (app down, qBittorrent away)? Read once from the
        stored time of the last good run, then kept in memory."""
        now = self._now()
        if self._previous_ok is False:
            stored = get_state(self.db, KEY_LAST_OK)
            try:
                self._previous_ok = _parse(stored) if stored else None
            except ValueError:
                self._previous_ok = None
        previous = self._previous_ok
        self._previous_ok = now
        return (
            isinstance(previous, datetime) and (now - previous).total_seconds() > self._gap_seconds
        )

    def _watch_progress(self, job: Job, torrent: Torrent, result: RunResult) -> None:
        if self._stall_seconds is None:
            return
        now = self._now()
        since = _parse_or_none(job.progress_at)
        done = torrent.bytes_done
        age = (now - since).total_seconds() if since is not None else None
        if since is None or self._after_gap:
            self._remember(job, done, now)  # first sight, or the time away does not count
        elif not torrent.looking_for_sources:
            if age is not None and age >= PROGRESS_WRITE_SECONDS:
                self._remember(job, done, now)  # paused, queued, checking: keep the clock young
        elif done != job.progress_bytes:
            if age is not None and age >= PROGRESS_WRITE_SECONDS:
                self._remember(job, done, now)  # it moved
        elif age is not None and age >= self._stall_seconds:
            self._give_up(job, torrent, result)

    def _remember(self, job: Job, done: int, now: datetime) -> None:
        jobs.record_progress(self.db, job.id, done, now.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    def _give_up(self, job: Job, torrent: Torrent, result: RunResult) -> None:
        days = max(1, round(self._stall_seconds / 86400))
        if torrent.metadata_missing:
            detail = "the torrent never got its file list"
        else:
            detail = f"the download stopped at {int(torrent.progress * 100)} %"
        reason = f"no sources for {days} days: {detail}"
        try:
            # Nothing is removed unless the job can be failed afterwards: the torrent first, and
            # if qBittorrent refuses, the next run simply tries again.
            self.qbit.delete_torrents([torrent.hash], delete_files=self._inside_downloads(torrent))
        except QbitError as exc:
            logger.warning(
                "reconciler: could not remove a stalled torrent, will retry",
                extra={"job_id": job.id, "error": str(exc)},
            )
            return
        before = len(result.failed)
        self._fail(job, reason, result)
        if len(result.failed) > before:
            result.gave_up.append(job.id)
            logger.warning(
                "job gave up: no sources",
                extra={"job_id": job.id, "infohash": job.external_id, "days": days},
            )

    def _inside_downloads(self, torrent: Torrent) -> bool:
        """May qBittorrent delete this torrent's files? Only inside downloads/, never the folder
        itself; with an unknown location (or no downloads folder configured) it does not."""
        if self._downloads_dir is None:
            return False
        location = torrent.content_path or torrent.save_path
        if not location:
            return False
        path = Path(os.path.normpath(location))
        root = Path(os.path.normpath(self._downloads_dir))
        return path != root and path.is_relative_to(root)

    def _missing(self, job: Job, result: RunResult) -> None:
        if job.status == "queued":
            age = (self._now() - _parse(job.created_at)).total_seconds()
            if age < self._queued_grace:
                return  # being added right now
            self._fail(
                job,
                "the torrent never reached qBittorrent (the app stopped before adding it); "
                "send the magnet link again",
                result,
            )
        else:
            self._fail(
                job,
                "the torrent is no longer in qBittorrent (it was removed); "
                "send the magnet link again to download it anew",
                result,
            )

    def _complete(self, job: Job, torrent: Torrent, result: RunResult) -> None:
        if self.organize is None:
            if not self._warned_no_organizer:
                logger.warning("reconciler: a torrent finished but no organizer is installed yet")
                self._warned_no_organizer = True
            if job.status == "queued":
                jobs.transition(self.db, job.id, "downloading", expected=("queued",))
            return
        # Claim the job; if it changed under us (it cannot, but the UPDATE is the guard) skip.
        if not jobs.transition(self.db, job.id, "organizing", expected=("queued", "downloading")):
            return
        try:
            note = self.organize(job, torrent)
        except OrganizeError as exc:
            self._fail(job, f"organizing failed: {exc}", result, expected=("organizing",))
        except Exception as exc:
            logger.exception("reconciler: organizer crashed", extra={"job_id": job.id})
            self._fail(
                job,
                f"organizing failed: {type(exc).__name__}: {exc}",
                result,
                expected=("organizing",),
            )
        else:
            # `done` even when some files were left out; the organizer's note says so (#69).
            if jobs.transition(self.db, job.id, "done", expected=("organizing",), note=note):
                result.done.append(job.id)
                logger.info("job done", extra={"job_id": job.id, "infohash": job.external_id})
                if self._on_organized is not None:
                    self._on_organized()

    def _fail(
        self,
        job: Job,
        reason: str,
        result: RunResult,
        *,
        expected: tuple[str, ...] = ("queued", "downloading", "organizing"),
    ) -> None:
        if jobs.transition(self.db, job.id, "failed", expected=expected, error=reason):
            result.failed.append(job.id)
            logger.warning(
                "job failed",
                extra={"job_id": job.id, "infohash": job.external_id, "reason": reason},
            )


def _parse_or_none(timestamp: str | None) -> datetime | None:
    try:
        return _parse(timestamp) if timestamp else None
    except ValueError:
        return None


def _parse(timestamp: str) -> datetime:
    """The UTC ISO text SQLite stores ('2026-11-01T05:00:00.123Z')."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
