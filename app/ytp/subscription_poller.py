"""Looking at subscribed channels for new videos (issue #20).

A scheduler job (every minute) calls `SubscriptionPoller.run_once`, which polls the channels that
are DUE and hands new videos to the YouTube download queue (#18). Rules:

* **Slow and spread out.** A channel is due once its own interval has passed: the base interval
  (`SUBSCRIPTION_POLL_MINUTES`, default 60) times a fixed per-channel factor between 0.8 and 1.2,
  so channels drift apart instead of all going at once. Within a run there are a few seconds of
  pause between two channels. After a failure a channel is tried again after 10 minutes (or the
  base interval if that is shorter), not after a full hour.
* **Where the list comes from.** yt-dlp lists the channel's newest uploads first
  (`channel_videos.py`, the RSS feed answers 404 for whole networks); the RSS feed is the
  fallback if that fails. If both fail the error says it is YouTube (`feeds.SERVICE_*`), which the
  alert monitor turns into ONE alert, not one per channel.
* **First subscribe.** Only videos published AFTER the subscription was made are downloaded; the
  back catalogue never is. The subscription's `backfill` (default 0) is the exception: the first
  poll also takes that many of the newest older videos.
* **New or old, without dates.** The yt-dlp list has no dates (the RSS feed has). The poll walks
  the list from the newest video and stops at the first one it knows (settled in an earlier poll:
  `seen_videos`) or that is older than the subscription. A video it does not know is dated by
  one yt-dlp look at it (the same look that decides live / premiere / Short, #39), so it costs
  nothing extra. A video that already has a job, or was skipped on purpose, is known. The
  settled videos are written down only when a poll got to its end: if it had to stop early (a
  look failed for a passing reason, or `MAX_PROBES_PER_POLL`), the next poll starts again at the
  newest video and the work already done (the jobs) is not repeated.
* **No seen-list of its own.** A video is "seen" when the queue already has a job for it (done,
  running, queued or failed): the unique index of `jobs` decides, so a video fetched by hand, or
  shown by two channels, is downloaded once, and a failed video is NOT re-queued by every poll
  (the user retries it from the job list). yt-dlp's `--download-archive` is not used for that
  reason; see docs/youtube.md.
* **One failing channel does not stop the rest**; its reason goes to `last_error` and shows in
  the subscription list. If the feed cannot be reached at all (VPN down) the run stops after the
  first such failure instead of trying every channel against a dead connection.
* **Lives, premieres and Shorts are not queued** (#39, `ytp.video_filter`). Only for a video that
  is NEW to the queue: a free look at the feed's link first, then one yt-dlp metadata probe
  (same proxy, a pause between probes, at most `MAX_PROBES_PER_POLL` per poll; the rest wait for
  the next poll). The verdict is stored (`skipped_videos`) so a video is judged once and shown in
  the subscription's details. A probe that fails for a passing reason (network, bot check,
  timeout) postpones the video to the next poll; any other failure queues it, so the download
  shows the real reason. A video with a job already (also a failed one) is never probed.
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

from . import accounts, jobs, seen_videos, skipped_videos, subscriptions
from .channel_videos import ChannelLister
from .db import Database
from .feeds import (
    CHANNEL_GONE,
    SERVICE_BOT,
    SERVICE_DOWN,
    FeedClient,
    FeedEntry,
    FeedError,
    parse_feed,
    parse_time,
)
from .metadata import sanitize_name
from .subscriptions import Subscription
from .video_filter import VideoTraits, skip_reason
from .youtube import ProbeError, VideoRef, YoutubeProbe
from .youtube_jobs import JOB_TYPE, YoutubeJobMetadata, YoutubeWorker, job_external_id

logger = logging.getLogger(__name__)

POLL_JOB_ID = "subscription-poll"
POLL_JOB_SECONDS = 60
RETRY_MINUTES = 10
MAX_CHANNELS_PER_RUN = 20
PAUSE_SECONDS = (3.0, 8.0)  # between two channels of one run
PROBE_PAUSE_SECONDS = (1.0, 3.0)  # between two metadata probes (#39)
MAX_PROBES_PER_POLL = 8
# Probe failures that say nothing about the video: judge it again at the next poll.
TRANSIENT_PROBE_KINDS = frozenset({"network", "bot_check", "timeout", "busy"})


# "Newer than any subscription": a video that cannot be dated but must not be taken for old.
NEW_FOR_SURE = datetime.max.replace(tzinfo=UTC)


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
    dated = [e for e in entries if e.published is not None]
    wanted = [e for e in dated if e.published > subscribed]  # type: ignore[operator]
    if sub.last_checked is None and sub.backfill > 0:
        older = [e for e in dated if e.published <= subscribed]  # type: ignore[operator]
        wanted += older[: sub.backfill]  # entries are newest first
    return sorted(wanted, key=lambda e: e.published)  # type: ignore[arg-type,return-value]


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
        probe: YoutubeProbe | None = None,
        probe_pause: tuple[float, float] = PROBE_PAUSE_SECONDS,
        lister: ChannelLister | None = None,
    ):
        self.db = db
        self.feeds = feeds
        self.worker = worker
        self.poll_minutes = poll_minutes
        self._now = now
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._pause = pause
        self.probe = probe  # None: no filtering (every new video is queued)
        self.lister = lister  # None: the RSS feed alone (the list from yt-dlp comes first)
        self._probe_pause = probe_pause
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

    def _entries(self, sub: Subscription) -> list[FeedEntry]:
        """The channel's newest videos, newest first: from yt-dlp, else from the RSS feed.
        Raises FeedError with a text that says what really happened."""
        listing_error: ProbeError | None = None
        if self.lister is not None:
            try:
                return self.lister.list(sub.channel_id)
            except ProbeError as error:
                if error.kind == "not_found":
                    raise FeedError("not_found", CHANNEL_GONE) from None
                listing_error = error
                logger.warning(
                    "channel list from yt-dlp failed, trying the RSS feed",
                    extra={"subscription_id": sub.id, "kind": error.kind},
                )
        try:
            return parse_feed(self.feeds.fetch(sub.channel_id), sub.channel_id)
        except FeedError as feed_error:
            if listing_error is None:
                raise
            if listing_error.kind == "network" and feed_error.kind == "network":
                raise  # nothing reaches YouTube at all: the VPN or the line is down
            text = SERVICE_BOT if listing_error.kind == "bot_check" else SERVICE_DOWN
            raise FeedError("temporary", text) from None

    def poll(self, sub: Subscription) -> int:
        """Look at one channel now. Returns how many videos were queued; raises FeedError.
        Records last_checked/last_error either way."""
        try:
            entries = self._entries(sub)
        except FeedError as error:
            subscriptions.record_poll(self.db, sub.id, error.message, self._now())
            raise
        subscribed = parse_time(sub.created_at) or self._now()
        seen = seen_videos.ids(self.db, sub.id)
        skipped = skipped_videos.skipped_ids(self.db, sub.id)
        judge = _Judge(self, sub)

        # Walk from the newest video down to the first one that is settled or older than the
        # subscription; `wanted` are the new ones (newest first, with their place in the list).
        wanted: list[tuple[int, FeedEntry]] = []
        boundary: int | None = None
        stopped_early = False
        for index, entry in enumerate(entries):
            if entry.video_id in seen:
                boundary = index
                break
            has_job = (
                jobs.find_job(self.db, JOB_TYPE, job_external_id(entry.video_id, sub.target))
                is not None
            )
            if entry.video_id in skipped and not has_job:
                continue  # judged new at an earlier poll and left out on purpose
            published = entry.published
            if published is None and not has_job:
                try:
                    published = judge.date_of(entry)
                except _Postpone:
                    stopped_early = True
                    break
            if published is not None and published <= subscribed:
                boundary = index
                break
            wanted.append((index, entry))
        if (
            boundary is not None
            and sub.last_checked is None
            and sub.backfill > 0
            and not stopped_early
        ):
            wanted += list(enumerate(entries))[boundary : boundary + sub.backfill]

        queued = 0
        followers = accounts.followers(self.db, "channel", sub.id)
        for _, entry in sorted(wanted, key=lambda pair: pair[0], reverse=True):  # oldest first
            external_id = job_external_id(entry.video_id, sub.target)
            if jobs.find_job(self.db, JOB_TYPE, external_id) is None and not judge.allows(entry):
                continue
            published_day = (
                (entry.published or judge.published_of(entry))
                if (sub.target == "podcast")
                else None
            )
            metadata = YoutubeJobMetadata(
                video_id=entry.video_id,
                channel=sub.channel_name,
                title=sanitize_name(entry.title, entry.video_id),
                target=sub.target,
                audio_only=sub.audio_only,
                published=published_day.date().isoformat() if published_day else None,
            )
            job, outcome = jobs.submit_job(
                self.db,
                JOB_TYPE,
                external_id,
                metadata.to_json(),
                retry_failed=False,
            )
            for follower in followers:  # everybody who follows the channel sees the job
                accounts.add_job_owner(self.db, job.id, follower)
            queued += outcome == "created"
        if not stopped_early and not judge.postponed:
            seen_videos.add(self.db, sub.id, [e.video_id for e in entries])
        subscriptions.record_poll(self.db, sub.id, None, self._now())
        logger.info(
            "channel polled",
            extra={
                "subscription_id": sub.id,
                "entries": len(entries),
                "queued": queued,
                "skipped": judge.skipped,
                "postponed": judge.postponed,
            },
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


class _Postpone(Exception):
    """A video cannot be looked at now (a passing failure, or enough looks for this poll)."""


class _Judge:
    """Looks at the new videos of ONE poll: their date (`date_of`, for lists without dates) and
    whether each is queued (`allows`). Both use the same single yt-dlp look per video."""

    def __init__(self, poller: SubscriptionPoller, sub: Subscription):
        self._poller = poller
        self._sub = sub
        self._known = skipped_videos.skipped_ids(poller.db, sub.id)
        self._looked: dict[str, VideoTraits | ProbeError] = {}
        self._probes = 0
        self._probing_down = False
        self.skipped = 0
        self.postponed = 0

    def _traits(self, entry: FeedEntry) -> VideoTraits:
        """One look at the video (cached for this poll). Raises ProbeError for a permanent
        failure and `_Postpone` for a passing one or when the looks of this poll are used up."""
        cached = self._looked.get(entry.video_id)
        if isinstance(cached, ProbeError):
            raise cached
        if cached is not None:
            return cached
        if self._poller.probe is None or self._probing_down or self._probes >= MAX_PROBES_PER_POLL:
            self.postponed += 1
            raise _Postpone
        if self._probes:
            self._poller._sleep(self._poller._rng.uniform(*self._poller._probe_pause))
        self._probes += 1
        try:
            traits = self._poller.probe.traits_blocking(VideoRef(entry.video_id))
        except ProbeError as error:
            if error.kind in TRANSIENT_PROBE_KINDS and error.kind != "live":
                self._probing_down = error.kind in {"network", "bot_check"}
                self.postponed += 1
                logger.warning(
                    "video not judged yet, will try again",
                    extra={"subscription_id": self._sub.id, "kind": error.kind},
                )
                raise _Postpone from None
            self._looked[entry.video_id] = error
            raise
        self._looked[entry.video_id] = traits
        return traits

    def date_of(self, entry: FeedEntry) -> datetime | None:
        """When the video went online. A video that cannot be looked at for good (private,
        members-only, a stream that has not started, ...) counts as new: the download or the
        verdict then names the reason. Raises `_Postpone` if it cannot be looked at now."""
        try:
            return self._traits(entry).published or NEW_FOR_SURE
        except ProbeError:
            return NEW_FOR_SURE

    def published_of(self, entry: FeedEntry) -> datetime | None:
        """The date of a video already looked at (None if it was not, or has none)."""
        traits = self._looked.get(entry.video_id)
        return traits.published if isinstance(traits, VideoTraits) else None

    def allows(self, entry: FeedEntry) -> bool:
        if entry.video_id in self._known:
            return False  # judged at an earlier poll
        if entry.short_link:
            return self._skip(entry, "short")
        if self._poller.probe is None:
            return True
        try:
            traits = self._traits(entry)
        except _Postpone:
            return False
        except ProbeError as error:
            if error.kind == "live":  # yt-dlp refuses a stream that runs or has not started yet
                return self._skip(entry, "premiere" if error.upcoming else "live")
            # Private, deleted, members-only, ...: the download names the reason in the job list.
            return True
        reason = skip_reason(traits)
        return True if reason is None else self._skip(entry, reason)

    def _skip(self, entry: FeedEntry, reason: str) -> bool:
        skipped_videos.record(
            self._poller.db,
            self._sub.id,
            entry.video_id,
            reason,
            sanitize_name(entry.title, entry.video_id),
            entry.published.isoformat() if entry.published else None,
        )
        self._known.add(entry.video_id)
        self.skipped += 1
        logger.info(
            "video skipped",
            extra={"subscription_id": self._sub.id, "video_id": entry.video_id, "reason": reason},
        )
        return False
