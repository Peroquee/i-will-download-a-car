"""Podcast downloads: the job metadata, the worker and the feed poller (issue #34).

The database is the queue, exactly as for YouTube (`youtube_jobs.py`): a job of type `podcast`
is keyed by `podcasts.episode_key`, starts `queued` and moves

    queued -> downloading -> organizing -> done          any of them -> failed (readable reason)

One worker thread takes one job at a time. At start, jobs that were `downloading` or `organizing`
go back to `queued` and run again from the beginning (the work folder is made fresh).

The download itself is `SafeFetcher.download`: public addresses only, through the VPN proxy, size
and time limits, content checks (see `safefetch.py`). The enclosure address is re-checked here
when it is used, not trusted because it was stored. A failure never leaves a partial file in the
library.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from . import accounts, jobs, podcast_subscriptions
from .clients.jellyfin import LibraryRefresher
from .config import Settings
from .db import Database
from .jobs import Job
from .metadata import Name, sanitize_name
from .podcast_subscriptions import PodcastSubscription
from .podcasts import (
    MAX_EPISODE_BYTES,
    Episode,
    PodcastError,
    PodcastFeedClient,
    choose_extension,
    has_cover,
    place_episode,
    save_cover,
)
from .safefetch import FetchError
from .subscription_poller import SubscriptionPoller, new_entries, spread_interval

logger = logging.getLogger(__name__)

JOB_TYPE = "podcast"
MIN_FREE_BYTES = 1024**3  # refuse to start with less than 1 GiB free
DEADLINE_SECONDS = 2 * 3600.0
POLL_JOB_ID = "podcast-poll"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class PodcastJobMetadata(BaseModel):
    """What `jobs.metadata` holds for a podcast job. The names become folder and file names."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["podcast"] = "podcast"
    podcast: Name
    title: Name
    published: str  # YYYY-MM-DD (UTC)
    feed_url: str
    enclosure_url: str
    media_type: str = ""
    cover_url: str | None = None

    @field_validator("published")
    @classmethod
    def _date(cls, value: str) -> str:
        if not _DATE.fullmatch(value):
            raise ValueError("published must be YYYY-MM-DD")
        date.fromisoformat(value)
        return value


def episode_metadata(
    folder: str, episode: Episode, feed_url: str, cover_url: str | None
) -> PodcastJobMetadata:
    return PodcastJobMetadata(
        podcast=folder,
        title=sanitize_name(episode.title, "Episode"),
        published=episode.published.date().isoformat(),
        feed_url=feed_url,
        enclosure_url=episode.enclosure_url,
        media_type=episode.media_type,
        cover_url=cover_url,
    )


class PodcastWorker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        feeds: PodcastFeedClient,
        refresher: LibraryRefresher,
        *,
        idle_seconds: float = 5.0,
        min_free_bytes: int = MIN_FREE_BYTES,
    ):
        self.db = db
        self.settings = settings
        self.feeds = feeds
        self.refresher = refresher
        self._idle = idle_seconds
        self._min_free = min_free_bytes
        self._wake = threading.Event()
        self.processed = 0
        # Percent of the running download by job id, for the job list (not persisted).
        self.progress: dict[int, int] = {}
        # Size, speed and ETA of the running download by job id, for the dashboard (#38).
        self.details: dict[int, dict[str, int | None]] = {}

    def wake(self) -> None:
        """A job was just queued: look now instead of at the next idle check."""
        self._wake.set()

    def run(self, stop: threading.Event) -> None:
        jobs.requeue_interrupted(self.db, JOB_TYPE)
        while not stop.is_set():
            try:
                handled = self.run_one()
            except Exception:  # the worker must outlive any bug in one job
                logger.exception("podcast worker failed")
                handled = False
            if not handled:
                self._wake.wait(self._idle)
                self._wake.clear()

    def run_one(self) -> bool:
        """Take the next queued podcast job and finish it (done or failed). False if none."""
        job = jobs.claim_next_queued(self.db, JOB_TYPE)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("podcast job crashed", extra={"job_id": job.id})
            self._fail(job, f"the download failed: {type(exc).__name__}")
        finally:
            self.progress.pop(job.id, None)
            self.details.pop(job.id, None)
        self.processed += 1
        return True

    # ------------------------------------------------------------------ one job

    def _workdir(self, key: str) -> Path:
        return self.settings.downloads_dir / "incomplete" / "podcasts" / key

    def _process(self, job: Job) -> None:
        try:
            meta = PodcastJobMetadata.model_validate(job.metadata)
        except Exception:
            self._fail(job, "the stored job data is not valid any more; pick the episode again")
            return
        library = self.settings.library_path("podcasts")
        if not library.is_dir():
            self._fail(job, f"the library folder {library.name} does not exist (run make layout)")
            return
        workdir = self._workdir(job.external_id)
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        try:
            free = shutil.disk_usage(workdir).free
            if free < self._min_free:
                self._fail(
                    job,
                    f"not enough free space on the media volume ({free // 1024**2} MiB left, "
                    f"{self._min_free // 1024**2} MiB needed to start)",
                )
                return
            logger.info("podcast download starts", extra={"job_id": job.id})
            part = workdir / "episode.part"
            try:
                fetched = self.feeds.fetcher.download(
                    meta.enclosure_url,
                    part,
                    kind="media",
                    max_bytes=MAX_EPISODE_BYTES,
                    deadline_seconds=DEADLINE_SECONDS,
                    progress=self._progress(job),
                )
            except FetchError as error:
                self._fail(job, error.message)
                return
            extension = choose_extension(
                ("url", meta.enclosure_url),
                ("url", fetched.url),
                ("type", fetched.content_type),
                ("type", meta.media_type),
            )
            if extension is None:
                self._fail(job, "the file type of this episode is not recognised")
                return
            if not jobs.transition(self.db, job.id, "organizing", expected=("downloading",)):
                return  # someone else changed the job; leave it alone
            try:
                place_episode(
                    part,
                    library,
                    meta.podcast,
                    date.fromisoformat(meta.published),
                    meta.title,
                    extension,
                )
            except (PodcastError, OSError) as error:
                self._fail(job, getattr(error, "message", None) or f"could not file it: {error}")
                return
            self._cover(library, meta)
            if jobs.transition(self.db, job.id, "done", expected=("organizing",)):
                logger.info("job done", extra={"job_id": job.id})
                self.refresher.request_refresh(
                    self.settings.library_dirs["podcasts"],
                    immediate=job.priority > 0,  # high priority skips the debounce once (#51)
                )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _cover(self, library: Path, meta: PodcastJobMetadata) -> None:
        """Once per podcast folder; a cover that cannot be had never fails the episode."""
        if not meta.cover_url:
            return
        try:
            if has_cover(library / meta.podcast):
                return
            cover = self.feeds.cover(meta.cover_url)
            if cover is not None:
                save_cover(library, meta.podcast, cover[0], cover[1])
        except Exception:
            logger.warning("cover not saved", exc_info=True)

    def _fail(self, job: Job, reason: str) -> None:
        jobs.transition(
            self.db,
            job.id,
            "failed",
            expected=("queued", "downloading", "organizing"),
            error=reason,
        )
        logger.warning("podcast job failed", extra={"job_id": job.id, "reason": reason})

    def _progress(self, job: Job):
        reached = {"quarter": 0}
        started = time.monotonic()

        def hook(done: int, total: int | None) -> None:
            if not total:
                return
            self.progress[job.id] = min(int(done * 100 / total), 100)
            elapsed = time.monotonic() - started
            speed = int(done / elapsed) if elapsed > 0 and done > 0 else None
            self.details[job.id] = {
                "done_bytes": int(done),
                "total_bytes": int(total),
                "speed": speed,
                "eta": int((total - done) / speed) if speed else None,
            }
            quarter = int(done * 4 / total)
            if quarter > reached["quarter"]:
                reached["quarter"] = quarter
                logger.info(
                    "podcast download progress",
                    extra={"job_id": job.id, "percent": min(quarter * 25, 100)},
                )

        return hook


# ------------------------------------------------------------------------------------ polling


class PodcastPoller(SubscriptionPoller):
    """The channel poller's rules (slow, spread out, retry after errors, first subscribe takes only
    new episodes plus `backfill`) applied to podcast feeds. See `SubscriptionPoller`."""

    feed_errors = (PodcastError,)

    def __init__(self, db: Database, feeds: PodcastFeedClient, worker: PodcastWorker | None, **kw):
        super().__init__(db, feeds, worker, **kw)  # type: ignore[arg-type]
        self.feeds = feeds

    def _all(self) -> list[PodcastSubscription]:  # type: ignore[override]
        return podcast_subscriptions.list_all(self.db)

    def _interval(self, sub: PodcastSubscription) -> timedelta:  # type: ignore[override]
        return spread_interval(sub.feed_url, sub.last_error, self.poll_minutes)

    def _record(self, sub: PodcastSubscription, error: str | None) -> None:  # type: ignore[override]
        podcast_subscriptions.record_poll(self.db, sub.id, error, self._now())

    def poll(self, sub: PodcastSubscription) -> int:  # type: ignore[override]
        """Look at one feed now. Returns how many episodes were queued; raises PodcastError."""
        try:
            podcast = self.feeds.load(sub.feed_url)
        except PodcastError as error:
            self._record(sub, error.message)
            raise
        queued = 0
        followers = accounts.followers(self.db, "podcast", sub.id)
        for episode in new_entries(sub, podcast.episodes):  # type: ignore[arg-type]
            metadata = episode_metadata(sub.title, episode, sub.feed_url, podcast.image_url)
            job, outcome = jobs.submit_job(
                self.db,
                JOB_TYPE,
                episode.key,
                metadata.model_dump_json(),
                retry_failed=False,
            )
            for follower in followers:
                accounts.add_job_owner(self.db, job.id, follower)
            queued += outcome == "created"
        self._record(sub, None)
        logger.info(
            "podcast polled",
            extra={"podcast_id": sub.id, "episodes": len(podcast.episodes), "queued": queued},
        )
        return queued
