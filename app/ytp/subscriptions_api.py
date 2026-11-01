"""Channel subscriptions (issue #19).

POST /api/subscriptions
    {"channel": "<channel id | @handle | channel address | video address>",
     "name": "<folder name, optional>", "backfill": 0}

    Resolves the channel once (the RSS feed needs its id, not the handle), stores id and name and
    answers 201 with the subscription (`duplicate: false`), or 200 `duplicate: true` if the
    channel is already subscribed (nothing changes). `name` is the library folder the channel's
    videos go to (default: YouTube's channel name, made safe); `backfill` (0-15) is how many of
    the channel's existing videos the first poll should also fetch: 0 = only videos published
    after subscribing, never the back catalogue.

    422 invalid input (field-level)   409 the limit of 200 subscriptions
    404/403/502/504/429 {"detail", "kind"}: the lookup failed (see POST /api/youtube/probe)

GET /api/subscriptions
    {"subscriptions": [{id, channel_id, channel_name, channel_url, enabled, backfill, created_at,
                        last_checked, last_error}, ...]}

DELETE /api/subscriptions/{id}
    204; 404 if unknown. Videos that were already downloaded stay where they are.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from . import subscriptions
from .apiutil import PROBE_STATUS, problem
from .channels import ChannelResolver, InvalidChannel, parse_channel_ref
from .metadata import Name, sanitize_name
from .youtube import ProbeError

logger = logging.getLogger(__name__)


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    name: Name | None = None
    backfill: int = Field(default=0, ge=0, le=subscriptions.MAX_BACKFILL, strict=True)

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
            subscriptions.add, db, channel.channel_id, name, body.backfill
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


def register(app: FastAPI) -> None:
    app.add_api_route("/api/subscriptions", subscribe, methods=["POST"])
    app.add_api_route("/api/subscriptions", list_subscriptions, methods=["GET"])
    app.add_api_route("/api/subscriptions/{subscription_id}", unsubscribe, methods=["DELETE"])
