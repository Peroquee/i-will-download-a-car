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

The organizer is passed in (`organize(job, torrent)`), so the file work of #16 plugs in without
touching this module. Without one, finished torrents are left as `downloading` and the log says
so once; nothing is lost and a later run organizes them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import jobs
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError, QbitUnavailable, Torrent
from .db import Database
from .jobs import Job
from .scheduler import set_state

logger = logging.getLogger(__name__)

RECONCILE_JOB_ID = "reconcile"
RECONCILE_SECONDS = 30
KEY_LAST_OK = "reconciler.last_ok"
# A job is written BEFORE the torrent is added (#13). If the torrent is still unknown to
# qBittorrent after this long, the add never happened (the app stopped in between).
QUEUED_GRACE_SECONDS = 120


class OrganizeError(Exception):
    """An organizer's way to say what went wrong, in words a person can read (the job error)."""


Organizer = Callable[[Job, Torrent], None]


@dataclass
class RunResult:
    """What one run did; for tests and the log."""

    checked: int = 0
    started: list[int] = field(default_factory=list)  # queued -> downloading
    done: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    requeued: list[int] = field(default_factory=list)  # leftover `organizing` taken up again
    skipped: str | None = None  # why nothing was done (qBittorrent away)


class Reconciler:
    def __init__(
        self,
        db: Database,
        qbit: QbitClient,
        organize: Organizer | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        queued_grace_seconds: float = QUEUED_GRACE_SECONDS,
    ):
        self.db = db
        self.qbit = qbit
        self.organize = organize
        self._now = now
        self._queued_grace = queued_grace_seconds
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
            self.organize(job, torrent)
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
            if jobs.transition(self.db, job.id, "done", expected=("organizing",)):
                result.done.append(job.id)
                logger.info("job done", extra={"job_id": job.id, "infohash": job.external_id})

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


def _parse(timestamp: str) -> datetime:
    """The UTC ISO text SQLite stores ('2026-11-01T05:00:00.123Z')."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
