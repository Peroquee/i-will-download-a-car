"""The transcription PC's side of the app (docs/transcription.md).

These routes are not for people and not behind the login: the PC proves itself with the shared
secret `TRANSCRIBER_TOKEN` in the `X-Transcriber-Token` header (the proxy strips `Authorization`,
see proxy/Caddyfile). Without `TRANSCRIBER_TOKEN` in `.env` every route answers 404, as if it
did not exist. They are still LAN-only like everything behind the proxy.

GET  /api/transcriber/status
    {"queued", "running", "done", "failed", "pending_downloads", "worker_last_seen"}

POST /api/transcriber/claim                    {"worker": "main-pc"}
    200 {"id", "attempt", "language", "name", "size", "media_url"}  a video to transcribe
    204                                                              nothing to do
    `media_url` is relative to the app (`/api/transcriber/jobs/<id>/media?key=...`) and works
    only for this attempt, while it runs; it needs no token (so ffmpeg can read it directly,
    with range requests, without the token on its command line).

GET  /api/transcriber/jobs/{id}/media?key=...  the video file (ranges supported)
POST /api/transcriber/jobs/{id}/heartbeat      {"progress": 0-100}    200 | 409 not running
POST /api/transcriber/jobs/{id}/result         {"language": "de", "srt": "1\\n00:00:00,000 --> ..."}
    200 {"status": "done", "subtitle": "<file name>" | null}; the SRT is filed next to the video
    and Jellyfin refreshes that library. 422 not SRT / bad language, 409 not running, 410 gone.
POST /api/transcriber/jobs/{id}/fail           {"error": "CUDA out of memory"}
    200 {"status": "queued" | "failed"}: tried again later, or given up after 3 attempts.

The PC never names a path: it gets an id and sends text, and the app decides where it goes.
"""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import transcriptions
from .apiutil import problem
from .transcriptions import TranscriptionError

logger = logging.getLogger(__name__)

TOKEN_HEADER = "x-transcriber-token"
ERROR_STATUS = {"not_found": 404, "conflict": 409, "invalid": 422, "gone": 410}


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    progress: int | None = Field(default=None, ge=0, le=100)


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(min_length=2, max_length=3)
    srt: str = Field(max_length=transcriptions.MAX_SRT_BYTES)


class FailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: str = Field(min_length=1, max_length=2000)


def _guard(request: Request, *, token: bool = True):
    """(db, None) when the request may go on, else (None, the answer)."""
    settings = request.app.state.settings
    if not settings.transcription_enabled:
        return None, problem(404, "Not Found")
    if token:
        sent = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(sent.encode(), settings.transcriber_token.encode()):
            return None, problem(401, "a valid X-Transcriber-Token is required")
    db = request.app.state.db
    if db is None:
        return None, problem(503, "The database is not available; see /health.")
    return db, None


def _error(exc: TranscriptionError) -> JSONResponse:
    return problem(ERROR_STATUS.get(exc.kind, 400), exc.message)


async def status(request: Request):
    db, failure = _guard(request)
    if failure is not None:
        return failure
    return JSONResponse(await asyncio.to_thread(transcriptions.status, db))


async def claim(body: ClaimRequest, request: Request):
    db, failure = _guard(request)
    if failure is not None:
        return failure
    data_root = request.app.state.settings.data_root
    try:
        row = await asyncio.to_thread(transcriptions.claim, db, data_root, body.worker)
    except TranscriptionError as exc:
        return _error(exc)
    if row is None:
        return Response(status_code=204)
    path = transcriptions.media_file(data_root, row.media_path)
    return JSONResponse(
        {
            "id": row.id,
            "attempt": row.attempts,
            "language": row.language,
            "name": path.name if path else None,
            "size": path.stat().st_size if path else None,
            "media_url": f"/api/transcriber/jobs/{row.id}/media?key={row.media_key}",
        }
    )


async def media(transcription_id: int, request: Request, key: str = ""):
    db, failure = _guard(request, token=False)  # the per-attempt key is the credential
    if failure is not None:
        return failure
    data_root = request.app.state.settings.data_root
    try:
        _, path = await asyncio.to_thread(
            transcriptions.media_for, db, data_root, transcription_id, key
        )
    except TranscriptionError as exc:
        return _error(exc)
    return FileResponse(path, media_type="application/octet-stream")


async def heartbeat(transcription_id: int, body: HeartbeatRequest, request: Request):
    db, failure = _guard(request)
    if failure is not None:
        return failure
    alive = await asyncio.to_thread(transcriptions.heartbeat, db, transcription_id, body.progress)
    if not alive:
        return problem(409, "this transcription is not running any more")
    return JSONResponse({"status": "running"})


async def result(transcription_id: int, body: ResultRequest, request: Request):
    db, failure = _guard(request)
    if failure is not None:
        return failure
    settings = request.app.state.settings
    try:
        row = await asyncio.to_thread(
            transcriptions.complete,
            db,
            settings.data_root,
            transcription_id,
            body.language,
            body.srt,
        )
    except TranscriptionError as exc:
        return _error(exc)
    except OSError as exc:
        logger.warning("could not file the subtitle", extra={"error": str(exc)})
        return problem(500, f"could not write the subtitle: {exc.strerror or exc}")
    if row.result_path:
        request.app.state.jellyfin.request_refresh(row.result_path.split("/", 1)[0])
    name = row.result_path.rsplit("/", 1)[-1] if row.result_path else None
    return JSONResponse({"status": row.status, "subtitle": name})


async def fail(transcription_id: int, body: FailRequest, request: Request):
    db, failure = _guard(request)
    if failure is not None:
        return failure
    try:
        row = await asyncio.to_thread(transcriptions.fail, db, transcription_id, body.error)
    except TranscriptionError as exc:
        return _error(exc)
    return JSONResponse({"status": row.status, "attempts": row.attempts})


def register(app: FastAPI) -> None:
    app.add_api_route("/api/transcriber/status", status, methods=["GET"])
    app.add_api_route("/api/transcriber/claim", claim, methods=["POST"])
    app.add_api_route("/api/transcriber/jobs/{transcription_id}/media", media, methods=["GET"])
    app.add_api_route(
        "/api/transcriber/jobs/{transcription_id}/heartbeat", heartbeat, methods=["POST"]
    )
    app.add_api_route("/api/transcriber/jobs/{transcription_id}/result", result, methods=["POST"])
    app.add_api_route("/api/transcriber/jobs/{transcription_id}/fail", fail, methods=["POST"])
