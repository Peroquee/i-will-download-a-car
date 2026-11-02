"""Channel subscriptions (issue #19).

POST /api/subscriptions
    {"channel": "<channel id | @handle | channel address | video address>",
     "name": "<folder name, optional>", "backfill": 0,
     "target": "video", "audio_only": false}

    Resolves the channel once (the RSS feed needs its id, not the handle), stores id and name and
    answers 201 with the subscription (`duplicate: false`), or 200 `duplicate: true` if the
    channel is already subscribed (nothing changes). `name` is the library folder the channel's
    videos go to (default: YouTube's channel name, made safe); `backfill` (0-15) is how many of
    the channel's existing videos the first poll should also fetch: 0 = only videos published
    after subscribing, never the back catalogue. `target` (issue #35) says where new videos go:
    "video" (default: the Youtube library) or "podcast" (episodes of a podcast named `name`, by
    default the channel, in the Podcasts library); `audio_only` (only with "podcast") keeps just the
    audio, tagged, with the thumbnail as cover. Subscribing again to a channel that is already
    subscribed changes nothing, the target included.

    422 invalid input (field-level)   409 the limit of 200 subscriptions
    404/403/502/504/429 {"detail", "kind"}: the lookup failed (see POST /api/youtube/probe)

GET /api/subscriptions
    {"subscriptions": [{id, channel_id, channel_name, channel_url, enabled, backfill, created_at,
                        last_checked, last_error}, ...]}

DELETE /api/subscriptions/{id}
    204; 404 if unknown. Videos that were already downloaded stay where they are.

POST /api/subscriptions/{id}/poll
    "Check now" (#27): looks at the channel's feed at once, whatever its interval, and queues new
    videos like the scheduled poll. 200 with the subscription (fresh last_checked/last_error) and
    `queued` (how many videos); 404 unknown; 429 another manual check is running; 502/503
    {"detail", "kind"} if the feed could not be read (the reason is also in `last_error`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from pydantic_core import PydanticCustomError

from . import subscriptions
from .apiutil import PROBE_STATUS, problem
from .channels import ChannelResolver, InvalidChannel, parse_channel_ref
from .feeds import FeedError
from .metadata import Name, sanitize_name
from .subscription_poller import PollBusy
from .youtube import ProbeError

logger = logging.getLogger(__name__)


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    name: Name | None = None
    backfill: int = Field(default=0, ge=0, le=subscriptions.MAX_BACKFILL, strict=True)
    target: Literal["video", "podcast"] = "video"
    audio_only: StrictBool = False

    @model_validator(mode="after")
    def _audio_needs_a_target(self) -> SubscribeRequest:
        if self.audio_only and self.target == "video":
            raise PydanticCustomError(
                "audio_only_invalid", "Audio only is for podcast episodes, not for videos"
            )
        return self

    @field_validator("channel")
    @classmethod
    def _valid_channel(cls, value: str) -> str:
        try:
            parse_channel_ref(value)
        except InvalidChannel as exc:
            raise PydanticCustomError("channel_invalid", "{reason}", {"reason": str(exc)}) from exc
        return value.strip()


async def subscribe(body: SubscribeRequest, request: Request) -> JSONResponse:
    db = request.app.state.db
    resolver: ChannelResolver | None = request.app.state.channels
    if db is None or resolver is None:
        return problem(503, "The database is not available; see /health.")
    ref = parse_channel_ref(body.channel)
    try:
        channel = await resolver.resolve(ref)
    except ProbeError as error:
        logger.info("channel lookup failed", extra={"kind": error.kind})
        return JSONResponse(
            {"detail": error.message, "kind": error.kind}, status_code=PROBE_STATUS[error.kind]
        )
    name = body.name or sanitize_name(channel.name, channel.channel_id)
    try:
        subscription, created = await asyncio.to_thread(
            subscriptions.add,
            db,
            channel.channel_id,
            name,
            body.backfill,
            body.target,
            body.audio_only,
        )
    except subscriptions.TooManySubscriptions:
        return problem(
            409, f"At most {subscriptions.MAX_SUBSCRIPTIONS} channels can be subscribed."
        )
    logger.info("subscription", extra={"subscription_id": subscription.id, "new": created})
    return JSONResponse(
        {**subscription.as_dict(), "duplicate": not created}, status_code=201 if created else 200
    )


async def list_subscriptions(request: Request) -> JSONResponse:
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    rows = await asyncio.to_thread(subscriptions.list_all, db)
    return JSONResponse({"subscriptions": [row.as_dict() for row in rows]})


async def unsubscribe(subscription_id: int, request: Request) -> Response:
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    if not await asyncio.to_thread(subscriptions.delete, db, subscription_id):
        return problem(404, "no such subscription")
    return Response(status_code=204)


# The status of a failed "check now" by feed error kind (the channel is fine, the way to it is not).
FEED_STATUS = {"network": 502, "not_found": 502, "temporary": 503, "bad_feed": 502}


async def poll_now(subscription_id: int, request: Request) -> JSONResponse:
    db = request.app.state.db
    poller = request.app.state.poller
    if db is None or poller is None:
        return problem(503, "The database is not available; see /health.")
    sub = await asyncio.to_thread(subscriptions.get, db, subscription_id)
    if sub is None:
        return problem(404, "no such subscription")
    try:
        queued = await asyncio.to_thread(poller.poll_now, sub)
    except PollBusy:
        return problem(429, "Another check is running; try again in a moment.")
    except FeedError as error:
        return JSONResponse(
            {"detail": error.message, "kind": error.kind}, status_code=FEED_STATUS[error.kind]
        )
    except Exception:
        logger.exception("manual channel poll crashed", extra={"subscription_id": sub.id})
        await asyncio.to_thread(
            subscriptions.record_poll, db, sub.id, "internal error while polling"
        )
        return problem(500, "Checking the channel failed unexpectedly; see the logs.")
    fresh = await asyncio.to_thread(subscriptions.get, db, sub.id)
    return JSONResponse({**(fresh or sub).as_dict(), "queued": queued})


def register(app: FastAPI) -> None:
    app.add_api_route("/api/subscriptions", subscribe, methods=["POST"])
    app.add_api_route("/api/subscriptions", list_subscriptions, methods=["GET"])
    app.add_api_route("/api/subscriptions/{subscription_id}", unsubscribe, methods=["DELETE"])
    app.add_api_route("/api/subscriptions/{subscription_id}/poll", poll_now, methods=["POST"])
