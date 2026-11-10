"""The job loop shared by the download workers (issue #75).

The database is the queue: `run` puts jobs that a restart left `downloading` or `organizing`
back to `queued` once, then takes one queued job at a time; `run_one` claims it, processes it
and, if anything escapes, fails it with a readable reason. A failed job never stops the next,
and the worker sleeps until `wake` says a new job arrived.

A subclass sets `job_type`, `label` (the word in the log messages) and `workdir_name` (the
subfolder of `downloads/incomplete/`), adds its own collaborators and then calls
`super().__init__(...)`; it implements `_process` (one job) and `_progress` (the progress hook
its downloader takes).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import jobs
from .clients.jellyfin import LibraryRefresher
from .config import Settings
from .db import Database
from .jobs import Job

logger = logging.getLogger(__name__)


class QueueWorker:
    job_type: str
    label: str
    workdir_name: str

    def __init__(
        self,
        db: Database,
        settings: Settings,
        refresher: LibraryRefresher,
        *,
        idle_seconds: float = 5.0,
        on_done: Callable[[], None] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.refresher = refresher
        self._on_done = on_done  # e.g. the dashboard's storage sizes (#82)
        self._idle = idle_seconds
        self._wake = threading.Event()
        self.processed = 0
        # Percent of the running download by job id, for the job list (not persisted).
        self.progress: dict[int, int] = {}
        # Size, speed and ETA of the running download by job id, for the dashboard (#38).
        self.details: dict[int, dict[str, int | None]] = {}

    def wake(self) -> None:
        """A job was just queued: look now instead of at the next idle check."""
        self._wake.set()

    # ------------------------------------------------------------------ the loop

    def run(self, stop: threading.Event) -> None:
        requeued = False
        while not stop.is_set():
            try:
                if not requeued:
                    # Inside the guard: a database that is busy or locked at start must not
                    # kill the thread (the API would keep accepting jobs nobody runs, #69).
                    jobs.requeue_interrupted(self.db, self.job_type)
                    requeued = True
                handled = self.run_one()
            except Exception:  # the worker must outlive any bug in one job
                logger.exception("%s worker failed", self.label)
                handled = False
            if not handled:
                self._wake.wait(self._idle)
                self._wake.clear()

    def run_one(self) -> bool:
        """Take the next queued job and finish it (done or failed). False if none."""
        job = jobs.claim_next_queued(self.db, self.job_type)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("%s job crashed", self.label, extra={"job_id": job.id})
            self._fail(job, f"the download failed: {type(exc).__name__}")
        finally:
            self.progress.pop(job.id, None)
            self.details.pop(job.id, None)
        self.processed += 1
        return True

    # ------------------------------------------------------------------ one job

    def _workdir(self, key: str) -> Path:
        return self.settings.downloads_dir / "incomplete" / self.workdir_name / key

    def _fail(self, job: Job, reason: str) -> None:
        jobs.transition(
            self.db,
            job.id,
            "failed",
            expected=("queued", "downloading", "organizing"),
            error=reason,
        )
        logger.warning("%s job failed", self.label, extra={"job_id": job.id, "reason": reason})

    def _process(self, job: Job) -> None:
        raise NotImplementedError

    def _progress(self, job: Job) -> Callable[..., Any]:
        raise NotImplementedError
