"""YouTube download jobs: the queue in the database and the worker that runs it (issue #18).

`POST /api/youtube/download` only records a job (type `youtube`, keyed by the video id, status
`queued`) and wakes the worker; the download happens in the background, one at a time, and the
job's status says where it is:

    queued -> downloading -> organizing -> done          any of them -> failed (readable reason)

The database is the queue, so nothing is lost when the app restarts: at start, jobs that were
`downloading` or `organizing` go back to `queued` and run again from the beginning (the work
folder is thrown away and made fresh). One failed download does not stop the next. A failed job
is retried by submitting the same video again, as for magnets.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from . import jobs
from .clients.jellyfin import LibraryRefresher
from .config import Settings
from .db import Database
from .jobs import Job
from .metadata import Name
from .youtube import ProbeError, VideoRef, parse_video_url
from .youtube_download import (
    Downloaded,
    DownloadFailed,
    YoutubeDownloader,
    place,
)

logger = logging.getLogger(__name__)

JOB_TYPE = "youtube"


class YoutubeJobMetadata(BaseModel):
    """What `jobs.metadata` holds for a YouTube job: the CONFIRMED names, which become folders."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["youtube"] = "youtube"
    video_id: str
    channel: Name
    title: Name


class YoutubeWorker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        downloader: YoutubeDownloader,
        refresher: LibraryRefresher,
        *,
        idle_seconds: float = 5.0,
    ):
        self.db = db
        self.settings = settings
        self.downloader = downloader
        self.refresher = refresher
        self._idle = idle_seconds
        self._wake = threading.Event()
        self.processed = 0
        # Percent of the running download by job id, for the job list (not persisted).
        self.progress: dict[int, int] = {}

    def wake(self) -> None:
        """A job was just queued: look now instead of at the next idle check."""
        self._wake.set()

    # ------------------------------------------------------------------ the loop

    def run(self, stop: threading.Event) -> None:
        jobs.requeue_interrupted(self.db, JOB_TYPE)
        while not stop.is_set():
            try:
                handled = self.run_one()
            except Exception:  # the worker must outlive any bug in one job
                logger.exception("youtube worker failed")
                handled = False
            if not handled:
                self._wake.wait(self._idle)
                self._wake.clear()

    def run_one(self) -> bool:
        """Take the next queued YouTube job and finish it (done or failed). False if none."""
        job = jobs.claim_next_queued(self.db, JOB_TYPE)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("youtube job crashed", extra={"job_id": job.id})
            self._fail(job, f"the download failed: {type(exc).__name__}")
        finally:
            self.progress.pop(job.id, None)
        self.processed += 1
        return True

    # ------------------------------------------------------------------ one job

    def _workdir(self, video_id: str) -> Path:
        return self.settings.downloads_dir / "incomplete" / "youtube" / video_id

    def _process(self, job: Job) -> None:
        try:
            meta = YoutubeJobMetadata.model_validate(job.metadata)
            ref = parse_video_url(f"https://www.youtube.com/watch?v={job.external_id}")
        except Exception:
            self._fail(job, "the stored job data is not valid any more; submit the video again")
            return
        if meta.video_id != ref.video_id:
            self._fail(job, "the stored job data does not match its video; submit it again")
            return
        library = self.settings.data_root / self.settings.library_dirs["youtube"]
        if not library.is_dir():
            self._fail(job, f"the library folder {library.name} does not exist (run make layout)")
            return
        workdir = self._workdir(ref.video_id)
        logger.info("youtube download starts", extra={"job_id": job.id, "video_id": ref.video_id})
        try:
            downloaded = self.downloader.download(ref, workdir, self._progress(job))
            if not jobs.transition(self.db, job.id, "organizing", expected=("downloading",)):
                return  # someone else changed the job; leave it alone
            self._file(job, ref, meta, downloaded, library)
        except (ProbeError, DownloadFailed) as exc:
            message = exc.message if isinstance(exc, ProbeError) else str(exc)
            self._fail(job, message)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _file(
        self,
        job: Job,
        ref: VideoRef,
        meta: YoutubeJobMetadata,
        downloaded: Downloaded,
        library: Path,
    ) -> None:
        try:
            place(downloaded, library, meta.channel, meta.title, ref.video_id)
        except DownloadFailed as exc:
            self._fail(job, str(exc))
            return
        if jobs.transition(self.db, job.id, "done", expected=("organizing",)):
            logger.info("job done", extra={"job_id": job.id, "video_id": ref.video_id})
            self.refresher.request_refresh(self.settings.library_dirs["youtube"])

    def _fail(self, job: Job, reason: str) -> None:
        jobs.transition(
            self.db,
            job.id,
            "failed",
            expected=("queued", "downloading", "organizing"),
            error=reason,
        )
        logger.warning("youtube job failed", extra={"job_id": job.id, "reason": reason})

    def _progress(self, job: Job):
        """A yt-dlp progress hook that logs each quarter once (not every chunk)."""
        reached = {"quarter": 0}

        def hook(data: dict[str, Any]) -> None:
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes")
            if data.get("status") != "downloading" or not total or not done:
                return
            self.progress[job.id] = min(int(done * 100 / total), 100)
            quarter = int(done * 4 / total)
            if quarter > reached["quarter"]:
                reached["quarter"] = quarter
                logger.info(
                    "youtube download progress",
                    extra={"job_id": job.id, "percent": min(quarter * 25, 100)},
                )

        return hook
