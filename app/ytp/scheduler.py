"""The background scheduler (issue #12): APScheduler 3 inside the app's event loop.

* **Lifecycle.** `AppScheduler.start()` / `shutdown()` are called from the app's lifespan; the
  scheduler lives and dies with the container. It only runs when the database is open.
* **Jobs are code, not data.** They are registered at every start with `add_interval_job`
  (`replace_existing`), so after a restart or an update the schedule is exactly what the code
  says, with nothing stale in a job store. What a job needs to remember between runs belongs
  in the database (`app_state`, `jobs`, `subscriptions`), where the poller (#20) already keeps
  `last_checked`. That is what "survives container restarts" means here: the scheduler is back
  with its jobs immediately, and the placeholder job's last heartbeat is read from the database
  and logged at start. (APScheduler's SQLAlchemy job store would add SQLAlchemy to the image to
  store schedules that the code already defines.)
* **No overlap.** `add_interval_job` sets `max_instances=1` and `coalesce=True`: a run that is
  still going when the next one is due makes that next one be skipped (logged as a warning),
  and a backlog of missed runs collapses into one. A job is therefore never running twice at
  the same time, however slow qBittorrent or the VPN is.
* **A failing job does not stop anything.** APScheduler keeps the schedule; the error is logged.
* **Sync jobs run in worker threads** (APScheduler's asyncio executor), so a slow call never
  blocks the web server's event loop.

The placeholder job is a heartbeat: every 30 s it records the time in `app_state`. `check()`
tells /health whether the scheduler is running and the heartbeat is recent, which also notices a
stuck event loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    JobEvent,
    JobExecutionEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .db import Database, get_state, set_state
from .timeutil import now_text

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
HEARTBEAT_JOB_ID = "heartbeat"
STALE_AFTER_HEARTBEATS = 3  # health turns red after this many missed heartbeats
KEY_LAST_HEARTBEAT = "scheduler.last_heartbeat"
KEY_STARTED_AT = "scheduler.started_at"


class AppScheduler:
    def __init__(
        self,
        db: Database,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.db = db
        self.heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._scheduler = AsyncIOScheduler(timezone=UTC)
        self._scheduler.add_listener(
            self._on_event, EVENT_JOB_ERROR | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED
        )
        self._stop_requested = False
        self._started_at: float | None = None
        self._last_heartbeat: float | None = None
        self.previous_heartbeat: str | None = None  # what the last container run left behind

    # -- lifecycle -------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        # AsyncIOScheduler stops on the next turn of the event loop, so its own flag lags
        # behind a shutdown() call; ours does not.
        return self._scheduler.running and not self._stop_requested

    def start(self) -> None:
        """Register the jobs and start. Must be called from inside the running event loop."""
        if self.running:
            return
        if self._stop_requested:
            raise RuntimeError("a stopped AppScheduler cannot be started again; make a new one")
        self.previous_heartbeat = get_state(self.db, KEY_LAST_HEARTBEAT)
        self._started_at = self._clock()
        set_state(self.db, KEY_STARTED_AT, now_text())
        self.add_interval_job(
            self._heartbeat, job_id=HEARTBEAT_JOB_ID, seconds=self.heartbeat_seconds,
            run_immediately=True,
        )  # fmt: skip
        self._scheduler.start()
        logger.info(
            "scheduler started",
            extra={
                "jobs": [job.id for job in self._scheduler.get_jobs()],
                "previous_heartbeat": self.previous_heartbeat,
            },
        )

    def shutdown(self) -> None:
        """Stop scheduling. Runs that are in flight are not waited for. Safe to call twice."""
        if self.running:
            self._stop_requested = True
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler stopped")

    # -- jobs ------------------------------------------------------------------------------

    def add_interval_job(
        self,
        func: Callable[..., Any],
        *,
        job_id: str,
        seconds: float,
        args: Sequence[Any] = (),
        run_immediately: bool = False,
    ) -> None:
        """Register (or replace) a job that runs every `seconds`, never overlapping itself.

        Every job of the app goes through here so that the guarantees above hold for all of
        them. `func` may be a plain function (runs in a worker thread) or a coroutine function.
        """
        # An explicit next_run_time=None would create the job PAUSED, so only pass it when asked.
        extra: dict[str, Any] = {"next_run_time": datetime.now(UTC)} if run_immediately else {}
        self._scheduler.add_job(
            func,
            "interval",
            seconds=seconds,
            args=list(args),
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            # A run that could not start on time is still worth running for a while after
            # (the loop was busy), but not for ever.
            misfire_grace_time=max(seconds, 1),
            **extra,
        )

    def job_ids(self) -> list[str]:
        return [job.id for job in self._scheduler.get_jobs()]

    def _heartbeat(self) -> None:
        set_state(self.db, KEY_LAST_HEARTBEAT, now_text())
        self._last_heartbeat = self._clock()

    # -- health ----------------------------------------------------------------------------

    def check(self) -> str | None:
        """None if the scheduler is fine, else a short reason (for /health)."""
        if not self.running:
            return "not running"
        now = self._clock()
        reference = self._last_heartbeat if self._last_heartbeat is not None else self._started_at
        limit = STALE_AFTER_HEARTBEATS * self.heartbeat_seconds
        if reference is None or now - reference > limit:
            return f"no heartbeat for {int(now - (reference or now))} s"
        return None

    # -- logging ---------------------------------------------------------------------------

    @staticmethod
    def _on_event(event: JobEvent) -> None:
        job_id = event.job_id
        if isinstance(event, JobExecutionEvent) and event.exception is not None:
            logger.error(
                "job failed",
                extra={"job": job_id},
                exc_info=(type(event.exception), event.exception, event.traceback),
            )
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            logger.warning("job skipped: the previous run is still going", extra={"job": job_id})
        elif event.code == EVENT_JOB_MISSED:
            logger.warning("job run missed", extra={"job": job_id})
