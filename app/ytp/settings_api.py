"""The server-wide defaults (#79): the resolution and how often subscriptions are looked at.

GET   /api/settings/defaults   any signed-in account (the Settings overlay of a subscription shows
                               what "server default" means right now)
PATCH /api/settings/defaults   admins only; send one or more fields, all or nothing

The answer (both routes):

    {"max_height": 1080,
     "subscription_poll_minutes": 60, "subscription_poll_at": null,
     "podcast_poll_minutes": 240,     "podcast_poll_at": null,
     "custom": {"max_height": false, ...},    # true where an admin set the value
     "timezone": "Europe/Berlin",             # the time zone of every "HH:MM" below
     "limits": {"choices": [0, 2160, ...], "min_minutes": {"channel": 15, "podcast": 30},
                "max_minutes": 1440}}

`custom` is false until an admin saves a field; the value then comes from the environment
(`YOUTUBE_MAX_HEIGHT`, `SUBSCRIPTION_POLL_MINUTES`, `PODCAST_POLL_MINUTES`), so an existing install
keeps working unchanged. A saved value takes effect at once, without a restart.

Fields (PATCH; `null` = back to the environment's value):

* `max_height`: 0 (best) or one of 2160, 1440, 1080, 720, 480, 360.
* `subscription_poll_minutes` / `podcast_poll_minutes`: 0 = paused, or 15..1440 (channels) and
  30..1440 (podcasts) minutes: how often a feed is looked at.
* `subscription_poll_at` / `podcast_poll_at`: an optional start time of day, "HH:MM" in the server's
  time zone, which anchors the interval (`""` or `null` = none).

The start time works like this: every local day restarts at the start time and steps by the
interval until the next start time. "Every 6 h from 06:00" is 06:00, 12:00, 18:00 and 00:00;
"every 24 h from 18:00" is once a day at 18:00; "every 5 h from 06:00" is 06:00, 11:00, 16:00,
21:00, 02:00 and then 06:00 again. A subscription is due when such a time has passed since its
last check. Without a start time it is due once the interval has passed since the last check.
A paused subscription ignores the start time. A subscription's own values (in
`PATCH /api/subscriptions/{id}` and `PATCH /api/podcasts/{id}`) override these defaults.
See `ytp.poll_schedule`.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import server_defaults
from .apiutil import problem


async def get_defaults(request: Request) -> JSONResponse:
    if request.app.state.db is None:
        return problem(503, "The database is not available; see /health.")
    return JSONResponse(await asyncio.to_thread(request.app.state.defaults.snapshot))


async def patch_defaults(request: Request) -> JSONResponse:
    if request.app.state.db is None:
        return problem(503, "The database is not available; see /health.")
    try:
        data = await request.json()
    except ValueError:
        return problem(422, "the request is not JSON")
    if not isinstance(data, dict):
        return problem(422, "send an object")
    try:
        fresh = await asyncio.to_thread(request.app.state.defaults.update, data)
    except server_defaults.Invalid as error:
        return problem(400, str(error))
    return JSONResponse(fresh)


def register(app: FastAPI) -> None:
    app.add_api_route("/api/settings/defaults", get_defaults, methods=["GET"])
    app.add_api_route("/api/settings/defaults", patch_defaults, methods=["PATCH"])
