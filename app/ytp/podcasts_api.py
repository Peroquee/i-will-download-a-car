"""Podcasts (issue #34).

POST /api/podcasts/probe   {"feed": "<feed address>"}
    Reads the feed and answers 200 {feed_url, title, folder, image: bool, subscribed: bool,
    subscription_id, episodes: [{key, title, published, size, media_type}, ...]} (newest first,
    at most 50). Nothing is stored. `folder` is the library folder the episodes would go to: the
    subscription's name if the feed is subscribed, else the feed's title made safe.

POST /api/podcasts/episode
    {"feed": "...", "key": "<episode key from the probe>", "name": "<folder, optional>"}
    Queues ONE episode. The app reads the feed again and takes the episode's file address from
    there: an address from the browser is never fetched. 201 {id, type: "podcast", status,
    duplicate: false}; 200 duplicate: true if that episode is already queued, running or done (a
    failed one is queued again); 404 no such episode in the feed.

    `catalogue` (#45) says what "all episodes" would do: {episodes (in the feed), to_download,
    skipped (already queued, done or on disk), size (bytes, of the episodes whose size the feed
    gives, null if none), sized (how many those are), space_ok}.

POST /api/podcasts/backlog {"feed": "...", "name": "<folder, optional>"}   (#45)
    Download the whole back catalogue, newest first, a few at a time (podcast_backlog.py).
    201 {backlog: {...}} a run was started; 200 {started: false, total: 0} nothing to do (all
    already there); 200 {joined: true, backlog} a run for this feed is going and the asker joined
    it; 409 not_enough_space (the known sizes do not fit on the drive).
GET    /api/podcasts/backlog         {"backlogs": [{id, folder, state: running|paused|done|
                                      cancelled, total, done, failed, waiting, skipped, size,
                                      paused_reason, created_at}]}   (own runs; an admin sees all)
DELETE /api/podcasts/backlog/{id}    204: stop queuing (episodes already queued finish); 404 not
                                      yours or not running.

POST /api/podcasts         {"feed": "...", "name": "<folder, optional>", "backfill": 0}
    Subscribes (201, or 200 `duplicate: true`). `backfill` (0-20): how many of the feed's existing
    episodes the first poll also takes; 0 = only episodes published after subscribing.
GET    /api/podcasts            {"podcasts": [{id, kind: "podcast", feed_url, title, enabled,
                                 backfill, created_at, last_checked, last_error}, ...]}
DELETE /api/podcasts/{id}       204; episodes already downloaded stay where they are.
POST   /api/podcasts/{id}/poll  "Check now": 200 with the subscription and `queued`.

Failures of the feed itself answer {"detail", "kind"} with a `feed_*` kind: feed_blocked (403: the
address points into a private network, or uses a forbidden port), feed_not_found (404),
feed_network (502), feed_temporary (503), feed_bad (502: not a readable RSS feed),
feed_bad_response (502), feed_too_large (502). 429: too many lookups at once.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from pydantic_core import PydanticCustomError

from . import accounts, jobs, podcast_backlog, podcast_subscriptions
from .apiutil import followed_view, may_use, problem, visible_to
from .metadata import Name, sanitize_name
from .podcast_jobs import JOB_TYPE, PodcastWorker, episode_metadata
from .podcasts import Podcast, PodcastError, PodcastFeedClient, normalise_feed_url
from .subscription_poller import PollBusy

logger = logging.getLogger(__name__)

MAX_EPISODES_SHOWN = 50
PARALLEL_LOOKUPS = 3
_gate = threading.BoundedSemaphore(PARALLEL_LOOKUPS)

# status per PodcastError kind (the way to the feed is the problem, not this request's input)
STATUS = {
    "invalid": 422, "blocked": 403, "not_found": 404, "network": 502, "temporary": 503,
    "bad_response": 502, "too_large": 502, "bad_feed": 502,
}  # fmt: skip
KIND = {"bad_feed": "feed_bad"}


def failure(error: PodcastError) -> JSONResponse:
    kind = KIND.get(error.kind, f"feed_{error.kind}")
    return JSONResponse(
        {"detail": error.message, "kind": kind}, status_code=STATUS.get(error.kind, 502)
    )


class FeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feed: str

    @field_validator("feed")
    @classmethod
    def _feed(cls, value: str) -> str:
        try:
            return normalise_feed_url(value)
        except PodcastError as error:
            raise PydanticCustomError(
                "feed_invalid", "{reason}", {"reason": error.message}
            ) from None


class SubscribeRequest(FeedRequest):
    name: Name | None = None
    backfill: int = Field(default=0, ge=0, le=podcast_subscriptions.MAX_BACKFILL, strict=True)


class EpisodeFeedRequest(FeedRequest):
    name: Name | None = None


class EpisodeRequest(FeedRequest):
    key: str = Field(pattern=r"^[0-9a-f]{40}$")
    name: Name | None = None
    # Start before normal queued downloads and hold them back while this one runs (#51).
    high_priority: StrictBool = False


def _context(request: Request):
    db = request.app.state.db
    feeds: PodcastFeedClient | None = request.app.state.podcast_feeds
    if db is None or feeds is None:
        return None, None, problem(503, "The database is not available; see /health.")
    return db, feeds, None


def _load(feeds: PodcastFeedClient, feed: str) -> Podcast:
    """Read the feed (blocking; run in a thread). At most a few lookups at once."""
    if not _gate.acquire(blocking=False):
        raise PodcastError("busy", "Several feed lookups are running. Try again in a moment.")
    try:
        return feeds.load(feed)
    finally:
        _gate.release()


def _busy() -> JSONResponse:
    return problem(429, "Several feed lookups are running. Try again in a moment.")


def _folder_for(podcast: Podcast, name: str | None, existing_title: str | None) -> str:
    """The library folder: the subscription's (so everything of a podcast shares one), else the
    name the user gave, else the feed's title made safe."""
    if existing_title:
        return existing_title
    if name:
        return name
    host = podcast.feed_url.split("/")[2] if "//" in podcast.feed_url else "podcast"
    return sanitize_name(podcast.title, host)


def _subscription_for(db, feed_url: str):
    rows = [s for s in podcast_subscriptions.list_all(db) if s.feed_url == feed_url]
    return rows[0] if rows else None


def _free_space(request: Request) -> tuple[int, int] | None:
    """(free, total) bytes of the media drive, None if it cannot be read."""
    try:
        usage = shutil.disk_usage(request.app.state.settings.data_root)
    except OSError:
        return None
    return usage.free, usage.total


def _catalogue(request: Request, db, podcast: Podcast, folder: str) -> dict:
    """What "all episodes" would do for this feed (nothing is stored)."""
    library = request.app.state.settings.library_path("podcasts")
    chosen = podcast_backlog.plan(db, podcast, folder, library)
    space = _free_space(request)
    return {
        "episodes": chosen.episodes,
        "to_download": chosen.total,
        "skipped": chosen.skipped,
        "size": chosen.size_bytes or None,
        "sized": chosen.sized,
        "space_ok": space is None or podcast_backlog.fits(*space[:1], space[1], chosen.size_bytes),
    }


async def probe(body: FeedRequest, request: Request) -> JSONResponse:
    db, feeds, failed = _context(request)
    if failed is not None:
        return failed
    try:
        podcast = await asyncio.to_thread(_load, feeds, body.feed)
    except PodcastError as error:
        if error.kind == "busy":
            return _busy()
        logger.info("podcast lookup failed", extra={"kind": error.kind})
        return failure(error)
    subscription = await asyncio.to_thread(_subscription_for, db, podcast.feed_url)
    folder = _folder_for(podcast, None, subscription.title if subscription else None)
    catalogue = await asyncio.to_thread(_catalogue, request, db, podcast, folder)
    return JSONResponse(
        {
            "feed_url": podcast.feed_url,
            "title": podcast.title,
            "folder": folder,
            "catalogue": catalogue,
            "image": podcast.image_url is not None,
            "subscribed": subscription is not None,
            "subscription_id": subscription.id if subscription else None,
            "episodes": [
                {
                    "key": e.key,
                    "title": e.title,
                    "published": e.published.date().isoformat(),
                    "size": e.size,
                    "media_type": e.media_type,
                }
                for e in podcast.episodes[:MAX_EPISODES_SHOWN]
            ],
        }
    )


async def queue_episode(body: EpisodeRequest, request: Request) -> JSONResponse:
    db, feeds, failed = _context(request)
    if failed is not None:
        return failed
    worker: PodcastWorker | None = request.app.state.podcast_worker
    if worker is None:
        return problem(503, "The database is not available; see /health.")
    try:
        podcast = await asyncio.to_thread(_load, feeds, body.feed)
    except PodcastError as error:
        if error.kind == "busy":
            return _busy()
        return failure(error)
    episode = next((e for e in podcast.episodes if e.key == body.key), None)
    if episode is None:
        return problem(404, "This episode is not in the feed (any more).")
    subscription = await asyncio.to_thread(_subscription_for, db, podcast.feed_url)
    folder = _folder_for(podcast, body.name, subscription.title if subscription else None)
    metadata = episode_metadata(folder, episode, podcast.feed_url, podcast.image_url)
    job, outcome = await asyncio.to_thread(
        jobs.submit_job,
        db,
        JOB_TYPE,
        episode.key,
        metadata.model_dump_json(),
        priority=1 if body.high_priority else 0,
    )
    await asyncio.to_thread(accounts.add_job_owner, db, job.id, request.state.account.id)
    logger.info("podcast job", extra={"job_id": job.id, "outcome": outcome})
    summary = {"id": job.id, "type": JOB_TYPE, "status": job.status}
    if outcome == "duplicate":
        return JSONResponse({**summary, "duplicate": True}, status_code=200)
    worker.wake()
    return JSONResponse({**summary, "duplicate": False}, status_code=201)


async def start_backlog(body: EpisodeFeedRequest, request: Request) -> JSONResponse:
    db, feeds, failed = _context(request)
    if failed is not None:
        return failed
    feeder: podcast_backlog.BacklogFeeder | None = request.app.state.backlog_feeder
    if feeder is None:
        return problem(503, "The database is not available; see /health.")
    try:
        podcast = await asyncio.to_thread(_load, feeds, body.feed)
    except PodcastError as error:
        if error.kind == "busy":
            return _busy()
        return failure(error)
    subscription = await asyncio.to_thread(_subscription_for, db, podcast.feed_url)
    folder = _folder_for(podcast, body.name, subscription.title if subscription else None)
    library = request.app.state.settings.library_path("podcasts")
    chosen = await asyncio.to_thread(podcast_backlog.plan, db, podcast, folder, library)
    if chosen.total == 0:
        return JSONResponse({"started": False, "total": 0, "skipped": chosen.skipped})
    space = _free_space(request)
    if space is not None and not podcast_backlog.fits(space[0], space[1], chosen.size_bytes):
        return JSONResponse(
            {
                "detail": "There is not enough free space on the media drive for these episodes.",
                "kind": "not_enough_space",
            },
            status_code=409,
        )
    account = request.state.account
    joined = False
    try:
        backlog_id = await asyncio.to_thread(
            podcast_backlog.start, db, podcast, folder, account.id, chosen
        )
    except podcast_backlog.BacklogExists as exists:
        backlog_id, joined = exists.backlog_id, True
    await asyncio.to_thread(feeder.tick)  # the first episodes go into the queue at once
    view = await asyncio.to_thread(podcast_backlog.get, db, backlog_id)
    logger.info(
        "podcast back catalogue",
        extra={"backlog_id": backlog_id, "episodes": chosen.total, "joined": joined},
    )
    assert view is not None
    return JSONResponse(
        {"started": not joined, "joined": joined, "backlog": view.as_dict()},
        status_code=200 if joined else 201,
    )


async def list_backlogs(request: Request) -> JSONResponse:
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    runs = await asyncio.to_thread(podcast_backlog.visible, db, visible_to(request))
    return JSONResponse({"backlogs": [run.as_dict() for run in runs]})


async def cancel_backlog(backlog_id: int, request: Request) -> Response:
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    account = request.state.account
    mine = account.is_admin or await asyncio.to_thread(
        podcast_backlog.owns, db, backlog_id, account.id
    )
    # somebody else's run looks exactly like one that does not exist
    if not mine or not await asyncio.to_thread(podcast_backlog.cancel, db, backlog_id):
        return problem(404, "no such running back catalogue")
    logger.info("podcast back catalogue cancelled", extra={"backlog_id": backlog_id})
    return Response(status_code=204)


async def subscribe(body: SubscribeRequest, request: Request) -> JSONResponse:
    db, feeds, failed = _context(request)
    if failed is not None:
        return failed
    try:
        podcast = await asyncio.to_thread(_load, feeds, body.feed)
    except PodcastError as error:
        if error.kind == "busy":
            return _busy()
        return failure(error)
    folder = _folder_for(podcast, body.name, None)
    try:
        subscription, created = await asyncio.to_thread(
            podcast_subscriptions.add, db, podcast.feed_url, folder, body.backfill
        )
    except podcast_subscriptions.TooManyPodcasts:
        return problem(
            409, f"At most {podcast_subscriptions.MAX_PODCASTS} podcasts can be subscribed."
        )
    account = request.state.account
    await asyncio.to_thread(accounts.follow, db, "podcast", subscription.id, account.id)
    others = len(await asyncio.to_thread(accounts.followers, db, "podcast", subscription.id)) - 1
    logger.info("podcast subscription", extra={"podcast_id": subscription.id, "new": created})
    return JSONResponse(
        {
            **subscription.as_dict(),
            "duplicate": not created,
            "following": True,
            "other_followers": others,
        },
        status_code=201 if created else 200,
    )


async def list_podcasts(request: Request) -> JSONResponse:
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    rows = await asyncio.to_thread(podcast_subscriptions.list_all, db)
    return JSONResponse(
        {"podcasts": await followed_view(db, "podcast", rows, request.state.account)}
    )


async def unsubscribe(subscription_id: int, request: Request, everyone: int = 0) -> Response:
    """Stop following; the subscription ends when nobody follows it. Admins: `?everyone=1`."""
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    account = request.state.account
    if everyone and account.is_admin:
        if not await asyncio.to_thread(podcast_subscriptions.delete, db, subscription_id):
            return problem(404, "no such podcast subscription")
        return Response(status_code=204)
    outcome = await asyncio.to_thread(accounts.unfollow, db, "podcast", subscription_id, account.id)
    if outcome == "none":
        return problem(404, "no such podcast subscription")
    return Response(status_code=204)


async def poll_now(subscription_id: int, request: Request) -> JSONResponse:
    db = request.app.state.db
    poller = request.app.state.podcast_poller
    if db is None or poller is None:
        return problem(503, "The database is not available; see /health.")
    sub = await asyncio.to_thread(podcast_subscriptions.get, db, subscription_id)
    if sub is None or not await may_use(db, "podcast", subscription_id, request.state.account):
        return problem(404, "no such podcast subscription")
    try:
        queued = await asyncio.to_thread(poller.poll_now, sub)
    except PollBusy:
        return problem(429, "Another check is running; try again in a moment.")
    except PodcastError as error:
        return failure(error)
    except Exception:
        logger.exception("manual podcast poll crashed", extra={"podcast_id": sub.id})
        await asyncio.to_thread(
            podcast_subscriptions.record_poll, db, sub.id, "internal error while polling"
        )
        return problem(500, "Checking the podcast failed unexpectedly; see the logs.")
    fresh = await asyncio.to_thread(podcast_subscriptions.get, db, sub.id)
    return JSONResponse({**(fresh or sub).as_dict(), "queued": queued})


def register(app: FastAPI) -> None:
    app.add_api_route("/api/podcasts/probe", probe, methods=["POST"])
    app.add_api_route("/api/podcasts/episode", queue_episode, methods=["POST"])
    app.add_api_route("/api/podcasts/backlog", start_backlog, methods=["POST"])
    app.add_api_route("/api/podcasts/backlog", list_backlogs, methods=["GET"])
    app.add_api_route("/api/podcasts/backlog/{backlog_id}", cancel_backlog, methods=["DELETE"])
    app.add_api_route("/api/podcasts", subscribe, methods=["POST"])
    app.add_api_route("/api/podcasts", list_podcasts, methods=["GET"])
    app.add_api_route("/api/podcasts/{subscription_id}", unsubscribe, methods=["DELETE"])
    app.add_api_route("/api/podcasts/{subscription_id}/poll", poll_now, methods=["POST"])
