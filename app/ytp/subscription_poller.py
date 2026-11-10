"""Looking at subscribed channels for new videos (issue #20).

A scheduler job (every minute) calls `SubscriptionPoller.run_once`, which polls the channels that
are DUE and hands new videos to the YouTube download queue (#18). Rules:

* **Slow and spread out.** Which channels are due is decided by `poll_schedule.decide` (#79):
  the channel's own interval and start time, else the server-wide default (an admin's value, else
  `SUBSCRIPTION_POLL_MINUTES`, default 60); see that module for the rules (spread factor,
  start-time anchor, retry after a failure, paused). Within a run there are a few seconds of
  pause between two channels.
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

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from . import accounts, jobs, publish_history, seen_videos, skipped_videos, subscriptions
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
from .poll_schedule import RETRY_MINUTES as RETRY_MINUTES
from .poll_schedule import Schedule, Smart, decide, effective, local_zone, spread_interval
from .server_defaults import DEFAULT_MODE, ServerDefaults, SmartSettings
from .smart_schedule import Pattern, learn
from .subscriptions import Subscription
from .video_filter import VideoTraits, skip_reason
from .youtube import ProbeError, VideoRef, YoutubeProbe
from .youtube_jobs import JOB_TYPE, YoutubeJobMetadata, YoutubeWorker, job_external_id

logger = logging.getLogger(__name__)

POLL_JOB_ID = "subscription-poll"
POLL_JOB_SECONDS = 60
MAX_CHANNELS_PER_RUN = 20
# At most this many feeds are looked at per rolling minute, over all subscriptions of a poller (the
# channels and the podcasts each have one): many feeds in a window at once must not burst through
# the VPN, and the pauses between two checks (a few seconds) come on top (#80).
MAX_CHECKS_PER_MINUTE = 12
HISTORY_THIN = 20  # fewer publication times than this: fill them from the RSS feed
HISTORY_RETRY_DAYS = 7  # ... and ask for them again after this long if it did not help
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
        defaults: ServerDefaults | None = None,
        zone: tzinfo | None = None,
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
        self.poll_minutes = poll_minutes  # used when no `defaults` (the admin's values) are given
        self.defaults = defaults
        self.zone = zone or (defaults.zone if defaults is not None else local_zone())
        self._now = now
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._pause = pause
        self.probe = probe  # None: no filtering (every new video is queued)
        self.lister = lister  # None: the RSS feed alone (the list from yt-dlp comes first)
        self._probe_pause = probe_pause
        self.stop = threading.Event()  # set at shutdown: a run in progress ends early
        self._manual = threading.Lock()
        self._checks: list[datetime] = []  # when the checks of the last minute began

    # -- what differs between channels and podcasts (PodcastPoller overrides these) ---------------

    def _all(self) -> list[Subscription]:
        return subscriptions.list_all(self.db)

    kind = "channel"  # which server default applies: "channel" or "podcast" (#79)

    def _key(self, sub: Subscription) -> str:
        """What spreads the intervals: the same subscription always gets the same factor."""
        return sub.channel_id

    def default_schedule(self) -> Schedule:
        """The server-wide interval and start time for this kind (read from the database)."""
        if self.defaults is not None:
            return self.defaults.schedule(self.kind)
        return Schedule(self.poll_minutes, None, DEFAULT_MODE)

    def schedule_of(self, sub: Subscription, default: Schedule | None = None) -> Schedule:
        """The effective interval, start time and mode: its own, else the server's."""
        default = default or self.default_schedule()
        return effective(
            sub.poll_minutes, sub.poll_at, default.minutes, default.at, sub.poll_mode, default.mode
        )

    def smart_settings(self) -> SmartSettings:
        return self.defaults.smart_settings() if self.defaults is not None else SmartSettings()

    def pattern_of(self, sub: Subscription, now: datetime) -> tuple[Pattern, datetime | None]:
        """What is learned about when this subscription publishes, and when it published last."""
        times = publish_history.times(self.db, self.kind, sub.id)
        return learn(times, self.zone, now), (times[0] if times else None)

    def smart_for(self, sub: Subscription, now: datetime, settings: SmartSettings) -> Smart:
        pattern, newest = self.pattern_of(sub, now)
        return Smart(pattern, settings.before, settings.after, settings.step, settings.base, newest)

    def _record(self, sub: Subscription, error: str | None) -> None:
        subscriptions.record_poll(self.db, sub.id, error, self._now())

    # -- which channels ----------------------------------------------------------------------

    def due(self) -> list[Subscription]:
        """Enabled subscriptions that are due; never-checked first, then the longest ago."""
        now = self._now()
        default = self.default_schedule()  # read once per tick, not once per subscription
        settings = self.smart_settings()
        result: list[tuple[int, datetime, Subscription]] = []
        for sub in self._all():
            if not sub.enabled:
                continue
            checked = parse_time(sub.last_checked) if sub.last_checked else None
            schedule = self.schedule_of(sub, default)
            smart = (
                self.smart_for(sub, now, settings)
                if schedule.mode == "smart" and schedule.minutes > 0
                else None
            )
            decision = decide(
                now=now,
                last_checked=checked,
                last_error=sub.last_error,
                schedule=schedule,
                key=self._key(sub),
                zone=self.zone,
                smart=smart,
                error_streak=sub.poll_errors,
            )
            if decision.due:
                # a feed inside its window comes first: that is what the smart mode is for
                rank = 0 if decision.phase == "window" else 1
                result.append((rank, checked or datetime.min.replace(tzinfo=UTC), sub))
        result.sort(key=lambda row: row[:2])
        return [sub for _, _, sub in result]

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
                # The channel's own cap (#65); None = the server default.
                max_height=sub.max_height,
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
        self._remember_publications(sub, [(e.video_id, e.published) for e in entries], judge)
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

    # -- the publication history the smart mode learns from (#80) ----------------------------------

    def _remember_publications(
        self,
        sub: Subscription,
        items: list[tuple[str, datetime | None]],
        judge: _Judge | None = None,
    ) -> None:
        """Keep the publication times this poll saw: the feed's own (RSS and podcasts have them)
        and the ones of the videos yt-dlp was asked about. Never fails the poll."""
        try:
            dated = [(item, when) for item, when in items if when is not None]
            if judge is not None:
                dated += judge.dated()
            publish_history.record(self.db, self.kind, sub.id, dated)
            if not any(when is not None for _, when in items):  # the RSS feed was not the source
                self._fill_history(sub)
        except Exception:
            logger.exception("publication history not saved", extra={"subscription_id": sub.id})

    def _fill_history(self, sub: Subscription) -> None:
        """The yt-dlp list of a channel has no times, so a new or thin history is filled from the
        RSS feed (15 uploads with their times): at once on the first poll, so the pattern is
        known without weeks of waiting. Asked rarely (`HISTORY_RETRY_DAYS`); a channel whose feed
        answers 404 is simply left to the interval."""
        if self.lister is None or publish_history.count(self.db, self.kind, sub.id) >= HISTORY_THIN:
            return
        asked = parse_time(sub.history_checked_at) if sub.history_checked_at else None
        if asked is not None and self._now() - asked < timedelta(days=HISTORY_RETRY_DAYS):
            return
        try:
            entries = parse_feed(self.feeds.fetch(sub.channel_id), sub.channel_id)
        except FeedError:
            entries = []
        publish_history.record(
            self.db, self.kind, sub.id, [(e.video_id, e.published) for e in entries if e.published]
        )
        subscriptions.mark_history_checked(self.db, sub.id, self._now())

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

    def _recent_checks(self) -> list[datetime]:
        """The checks of the last minute (the global cap counts them)."""
        cutoff = self._now() - timedelta(seconds=60)
        self._checks = [moment for moment in self._checks if moment > cutoff]
        return self._checks

    def run_once(self) -> RunResult:
        """One scheduler tick: poll the due channels (at most a few), spread out."""
        result = RunResult()
        started = self._recent_checks()
        room = max(0, MAX_CHECKS_PER_MINUTE - len(started))
        for position, sub in enumerate(self.due()[: min(MAX_CHANNELS_PER_RUN, room)]):
            if self.stop.is_set():
                break
            if position:
                self._sleep(self._rng.uniform(*self._pause))
            self._checks.append(self._now())
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

    def dated(self) -> list[tuple[str, datetime]]:
        """The videos looked at in this poll that have a publication time."""
        return [
            (video_id, traits.published)
            for video_id, traits in self._looked.items()
            if isinstance(traits, VideoTraits) and traits.published is not None
        ]

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
