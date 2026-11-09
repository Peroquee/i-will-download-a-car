"""Admin-only YouTube cookies (#46): store, inspect and remove the login cookies file.

PUT    /api/youtube/cookies    body: the cookies.txt text (at most 1 MiB, UTF-8)
                                200 {"present": true, "updated_at", "cookie_count"}
                                400 not a Netscape file, no youtube.com cookie, too large
DELETE /api/youtube/cookies    200 {"present": false, "updated_at": null, "cookie_count": 0}
GET    /api/youtube/cookies    200 {"present", "updated_at", "cookie_count"}

The cookies ARE the login. **Nothing in this module ever returns, echoes or logs a cookie
name or value**: the GET answer is only presence, time and count, and the validation messages
name the problem without quoting the file. The request body is read by hand (never through
pydantic), so an error cannot quote it either. The raw body is limited while it streams, so an
oversized upload is refused instead of being read into memory.

Admins only: the three routes are in `ytp.auth.POLICY` as `admin`. Log lines carry only the
message "youtube cookies updated"/"removed" and the admin's account name.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import youtube_cookies
from .apiutil import problem

logger = logging.getLogger(__name__)

MAX_BYTES = youtube_cookies.MAX_BYTES


async def _limited_body(request: Request) -> bytes | None:
    """The request body, or None when it is larger than the limit (read while streaming)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _settings(request: Request):
    return request.app.state.settings


async def get_cookies(request: Request) -> JSONResponse:
    status = await asyncio.to_thread(youtube_cookies.status, _settings(request))
    return JSONResponse(status)


async def put_cookies(request: Request) -> JSONResponse:
    body = await _limited_body(request)
    if body is None:
        return problem(400, f"the cookies file is larger than {MAX_BYTES // 1024**2} MiB")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return problem(400, "the cookies file must be UTF-8 text")
    settings = _settings(request)
    try:
        count = await asyncio.to_thread(youtube_cookies.save, settings, text)
    except youtube_cookies.CookieFileError as error:
        return problem(400, str(error))
    except OSError:
        logger.exception("youtube cookies could not be written")
        return problem(500, "the cookies could not be saved")
    logger.info("youtube cookies updated", extra={"account": request.state.account.username})
    status = await asyncio.to_thread(youtube_cookies.status, settings)
    status["cookie_count"] = count  # the lines just written, not a re-parse of the file
    return JSONResponse(status)


async def delete_cookies(request: Request) -> JSONResponse:
    settings = _settings(request)
    removed = await asyncio.to_thread(youtube_cookies.remove, settings)
    if removed:
        logger.info("youtube cookies removed", extra={"account": request.state.account.username})
    return JSONResponse({"present": False, "updated_at": None, "cookie_count": 0})


def register(app: FastAPI) -> None:
    app.add_api_route("/api/youtube/cookies", get_cookies, methods=["GET"])
    app.add_api_route("/api/youtube/cookies", put_cookies, methods=["PUT"])
    app.add_api_route("/api/youtube/cookies", delete_cookies, methods=["DELETE"])
