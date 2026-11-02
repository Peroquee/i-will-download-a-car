"""Looking at subscribed channels for new videos (issue #20).

A scheduler job (every minute) calls `SubscriptionPoller.run_once`, which polls the channels that
are DUE and hands new videos to the YouTube download queue (#18). Rules:

* **Slow and spread out.** A channel is due once its own interval has passed: the base interval
  (`SUBSCRIPTION_POLL_MINUTES`, default 60) times a fixed per-channel factor between 0.8 and 1.2,
  so channels drift apart instead of all going at once. Within a run there are a few seconds of
  pause between two channels. After a failure a channel is tried again after 10 minutes (or the
  base interval if that is shorter), not after a full hour.
* **First subscribe.** Only videos published AFTER the subscription was made are downloaded; the
  back catalogue never is. The subscription's `backfill` (default 0) is the exception: the first
  poll also takes that many of the newest older videos.
* **No seen-list of its own.** A video is "seen" when the queue already has a job for it (done,
  running, queued or failed): the unique index of `jobs` decides, so a video fetched by hand, or
  shown by two channels, is downloaded once, and a failed video is NOT re-queued by every poll
  (the user retries it from the job list). yt-dlp's `--download-archive` is not used for that
  reason; see docs/youtube.md.
* **One failing channel does not stop the rest**; its reason goes to `last_error` and shows in
  the subscription list. If the feed cannot be reached at all (VPN down) the run stops after the
  first such failure instead of trying every channel against a dead connection.
* The download worker is woken once at the end, and the Jellyfin refresh is debounced by
  `LibraryRefresher`, so a batch of new videos causes one library refresh, not one per video.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import jobs, subscriptions
from .db import Database
from .feeds import FeedClient, FeedEntry, FeedError, parse_feed, parse_time
from .metadata import sanitize_name
from .subscriptions import Subscription
from .youtube_jobs import JOB_TYPE, YoutubeJobMetadata, YoutubeWorker, job_external_id

logger = logging.getLogger(__name__)

POLL_JOB_ID = "subscription-poll"
POLL_JOB_SECONDS = 60
RETRY_MINUTES = 10
MAX_CHANNELS_PER_RUN = 20
PAUSE_SECONDS = (3.0, 8.0)  # between two channels of one run


class PollBusy(Exception):
    """A manual poll is already running."""


@dataclass
class RunResult:
    polled: int = 0
    queued: int = 0
    failed: int = 0


def spread_interval(key: str, last_error: str | None, base_minutes: int) -> timedelta:
    """How long to wait after the last check: the base interval times a factor in [0.8, 1.2]
    (stable, derived from `key`), or the short retry time after an error."""
    if last_error:
        return timedelta(minutes=min(RETRY_MINUTES, base_minutes))
    digest = hashlib.sha256(key.encode()).digest()
    fraction = int.from_bytes(digest[:2], "big") / 0xFFFF
    return timedelta(minutes=base_minutes * (0.8 + 0.4 * fraction))


def interval_for(sub: Subscription, base_minutes: int) -> timedelta:
    """`spread_interval` for a channel (the key is the channel id)."""
    return spread_interval(sub.channel_id, sub.last_error, base_minutes)


def new_entries(sub: Subscription, entries: list[FeedEntry]) -> list[FeedEntry]:
    """The entries to queue, oldest first: published after subscribing, plus on the very first
    poll the `backfill` newest older ones."""
    subscribed = parse_time(sub.created_at) or datetime.now(UTC)
    wanted = [e for e in entries if e.published > subscribed]
    if sub.last_checked is None and sub.backfill > 0:
        older = [e for e in entries if e.published <= subscribed]  # entries are newest first
        wanted += older[: sub.backfill]
    return sorted(wanted, key=lambda e: e.published)


class SubscriptionPoller:
    # What `poll` may raise for a feed that cannot be read (PodcastPoller overrides this).
    feed_errors: tuple[type[Exception], ...] = (FeedError,)

    def __init__(
        self,
        db: Database,
        feeds: FeedClient,
        worker: YoutubeWorker | None,
        *,
        poll_minutes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        pause: tuple[float, float] = PAUSE_SECONDS,
    ):
        self.db = db
        self.feeds = feeds
        self.worker = worker
        self.poll_minutes = poll_minutes
        self._now = now
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._pause = pause
        self.stop = threading.Event()  # set at shutdown: a run in progress ends early
        self._manual = threading.Lock()

    # -- what differs between channels and podcasts (PodcastPoller overrides these) ---------------

    def _all(self) -> list[Subscription]:
        return subscriptions.list_all(self.db)

    def _interval(self, sub: Subscription) -> timedelta:
        return interval_for(sub, self.poll_minutes)

    def _record(self, sub: Subscription, error: str | None) -> None:
        subscriptions.record_poll(self.db, sub.id, error, self._now())

    # -- which channels ----------------------------------------------------------------------

    def due(self) -> list[Subscription]:
        """Enabled channels whose interval has passed; never-checked first, then the longest ago."""
        now = self._now()
        result: list[tuple[datetime, Subscription]] = []
        for sub in self._all():
            if not sub.enabled:
                continue
            checked = parse_time(sub.last_checked) if sub.last_checked else None
            if checked is None or now - checked >= self._interval(sub):
                result.append((checked or datetime.min.replace(tzinfo=UTC), sub))
        result.sort(key=lambda pair: pair[0])
        return [sub for _, sub in result]

    # -- polling -------------------------------------------------------------------------------

    def poll(self, sub: Subscription) -> int:
        """Look at one channel now. Returns how many videos were queued; raises FeedError.
        Records last_checked/last_error either way."""
        try:
            entries = parse_feed(self.feeds.fetch(sub.channel_id), sub.channel_id)
        except FeedError as error:
            subscriptions.record_poll(self.db, sub.id, error.message, self._now())
            raise
        queued = 0
        for entry in new_entries(sub, entries):
            metadata = YoutubeJobMetadata(
                video_id=entry.video_id,
                channel=sub.channel_name,
                title=sanitize_name(entry.title, entry.video_id),
                target=sub.target,
                audio_only=sub.audio_only,
                published=(entry.published.date().isoformat() if sub.target == "podcast" else None),
            )
            _, outcome = jobs.submit_job(
                self.db,
                JOB_TYPE,
                job_external_id(entry.video_id, sub.target),
                metadata.to_json(),
                retry_failed=False,
            )
            queued += outcome == "created"
        subscriptions.record_poll(self.db, sub.id, None, self._now())
        logger.info(
            "channel polled",
            extra={"subscription_id": sub.id, "entries": len(entries), "queued": queued},
        )
        return queued

    def poll_now(self, sub: Subscription) -> int:
        """The "check now" button (#27): poll one channel at once, whatever its interval.

        One manual poll at a time (PollBusy otherwise). Raises FeedError like `poll`; the worker
        is woken if something was queued."""
        if not self._manual.acquire(blocking=False):
            raise PollBusy
        try:
            queued = self.poll(sub)
        finally:
            self._manual.release()
        if queued and self.worker is not None:
            self.worker.wake()
        return queued

    def run_once(self) -> RunResult:
        """One scheduler tick: poll the due channels (at most a few), spread out."""
        result = RunResult()
        for position, sub in enumerate(self.due()[:MAX_CHANNELS_PER_RUN]):
            if self.stop.is_set():
                break
            if position:
                self._sleep(self._rng.uniform(*self._pause))
            try:
                result.queued += self.poll(sub)
            except self.feed_errors as error:
                result.failed += 1
                logger.warning(
                    "channel poll failed",
                    extra={"subscription_id": sub.id, "kind": error.kind, "reason": error.message},
                )
                if error.kind == "network":
                    break  # the connection is down: the other channels would only fail the same way
            except Exception:  # a bug in one channel must not stop the others
                result.failed += 1
                logger.exception("channel poll crashed", extra={"subscription_id": sub.id})
                self._record(sub, "internal error while polling")
            result.polled += 1
        if result.queued and self.worker is not None:
            self.worker.wake()
        return result
