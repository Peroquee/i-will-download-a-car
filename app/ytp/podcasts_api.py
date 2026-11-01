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
import threading

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from . import jobs, podcast_subscriptions
from .apiutil import problem
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


class EpisodeRequest(FeedRequest):
    key: str = Field(pattern=r"^[0-9a-f]{40}$")
    name: Name | None = None


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
    return JSONResponse(
        {
            "feed_url": podcast.feed_url,
            "title": podcast.title,
            "folder": _folder_for(podcast, None, subscription.title if subscription else None),
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
        jobs.submit_job, db, JOB_TYPE, episode.key, metadata.model_dump_json()
    )
    logger.info("podcast job", extra={"job_id": job.id, "outcome": outcome})
    summary = {"id": job.id, "type": JOB_TYPE, "status": job.status}
    if outcome == "duplicate":
        return JSONResponse({**summary, "duplicate": True}, status_code=200)
    worker.wake()
    return JSONResponse({**summary, "duplicate": False}, status_code=201)


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
    logger.info("podcast subscription", extra={"podcast_id": subscription.id, "new": created})
    return JSONResponse(
        {**subscription.as_dict(), "duplicate": not created}, status_code=201 if created else 200
    )


async def list_podcasts(request: Request) -> JSONResponse:
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    rows = await asyncio.to_thread(podcast_subscriptions.list_all, db)
    return JSONResponse({"podcasts": [row.as_dict() for row in rows]})


async def unsubscribe(subscription_id: int, request: Request) -> Response:
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    if not await asyncio.to_thread(podcast_subscriptions.delete, db, subscription_id):
        return problem(404, "no such podcast subscription")
    return Response(status_code=204)


async def poll_now(subscription_id: int, request: Request) -> JSONResponse:
    db = request.app.state.db
    poller = request.app.state.podcast_poller
    if db is None or poller is None:
        return problem(503, "The database is not available; see /health.")
    sub = await asyncio.to_thread(podcast_subscriptions.get, db, subscription_id)
    if sub is None:
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
    app.add_api_route("/api/podcasts", subscribe, methods=["POST"])
    app.add_api_route("/api/podcasts", list_podcasts, methods=["GET"])
    app.add_api_route("/api/podcasts/{subscription_id}", unsubscribe, methods=["DELETE"])
    app.add_api_route("/api/podcasts/{subscription_id}/poll", poll_now, methods=["POST"])
