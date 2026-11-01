"""The HTTP API (issue #13). Mounted by `create_app`; sits behind the reverse proxy's login.

POST /api/magnet
    {"magnet": "magnet:?xt=urn:btih:...", "metadata": {"type": "movies", "title": ..., "year": ...}}

    Validates the link and the per-type metadata (field-level 422 errors), records a job keyed by
    the infohash, and hands the torrent to qBittorrent in the category named by `metadata.type`.

    201 {"id", "infohash", "type", "status": "queued", "duplicate": false}   new job
    200 {... "duplicate": true}                                              already in the system
    422                                                                      invalid input
    502 / 503 {"detail", "job_id"}                                           qBittorrent refused /
                                                                             is unavailable: the job
                                                                             is marked failed, and
                                                                             sending the same magnet
                                                                             again retries it

The job row is written BEFORE qBittorrent is asked: the unique index makes the dedupe race-free,
and a failure leaves a visible `failed` job instead of a torrent nobody knows about.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from . import jobs
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError, QbitUnavailable
from .clients.watchdog import WatchdogStatusClient
from .db import Database
from .magnet import InvalidMagnet, infohash_from_magnet
from .metadata import Metadata, metadata_to_json
from .youtube import InvalidVideoUrl, ProbeError, YoutubeProbe, parse_video_url

logger = logging.getLogger(__name__)


class MagnetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    magnet: str
    metadata: Metadata

    @field_validator("magnet")
    @classmethod
    def _valid_magnet(cls, value: str) -> str:
        # A pasted link often carries a trailing newline or spaces; that is not a reason to refuse.
        value = value.strip()
        try:
            infohash_from_magnet(value)
        except InvalidMagnet as exc:
            raise PydanticCustomError("magnet_invalid", "{reason}", {"reason": str(exc)}) from exc
        return value


def _problem(status: int, detail: str, job_id: int | None = None) -> JSONResponse:
    body: dict[str, Any] = {"detail": detail}
    if job_id is not None:
        body["job_id"] = job_id
    return JSONResponse(body, status_code=status)


async def add_magnet(body: MagnetRequest, request: Request) -> JSONResponse:
    db: Database | None = request.app.state.db
    qbit: QbitClient | None = request.app.state.qbit
    if db is None:
        return _problem(503, "The database is not available; see /health.")
    if qbit is None:
        return _problem(503, "qBittorrent is not configured (QBIT_PASSWORD is empty).")

    infohash = infohash_from_magnet(body.magnet)
    category = body.metadata.type  # the metadata types are the qBittorrent category names
    job, outcome = await asyncio.to_thread(
        jobs.submit_magnet_job, db, infohash, metadata_to_json(body.metadata)
    )
    # Log the infohash, never the link: it can carry tracker URLs with a private passkey.
    logger.info("magnet job", extra={"job_id": job.id, "infohash": infohash, "outcome": outcome})
    summary = {"id": job.id, "infohash": infohash, "type": category, "status": job.status}
    if outcome == "duplicate":
        return JSONResponse({**summary, "duplicate": True}, status_code=200)

    try:
        await asyncio.to_thread(qbit.add_magnet, body.magnet, category)
    except QbitError as exc:
        reason = str(exc)
        await asyncio.to_thread(jobs.mark_failed, db, job.id, reason)
        logger.warning(
            "adding the torrent failed",
            extra={"job_id": job.id, "infohash": infohash, "error": reason},
        )
        unavailable = isinstance(exc, QbitUnavailable)
        status = 503 if unavailable or isinstance(exc, QbitAuthError) else 502
        return _problem(status, f"qBittorrent did not take the torrent: {reason}", job.id)
    return JSONResponse({**summary, "duplicate": False}, status_code=201)


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str

    @field_validator("url")
    @classmethod
    def _video_url(cls, value: str) -> str:
        try:
            parse_video_url(value)
        except InvalidVideoUrl as exc:
            raise PydanticCustomError(
                "video_url_invalid", "{reason}", {"reason": str(exc)}
            ) from exc
        return value


# HTTP status per probe failure kind: the video itself (404), a restriction on it (403), the
# upstream or the VPN path (502/504), our own limit (429).
PROBE_STATUS = {
    "private": 404, "unavailable": 404, "unsupported": 422,
    "region": 403, "age": 403, "members": 403, "live": 409,
    "bot_check": 502, "network": 502, "unknown": 502, "timeout": 504, "busy": 429,
}  # fmt: skip


async def youtube_probe(body: ProbeRequest, request: Request) -> JSONResponse:
    """POST /api/youtube/probe {"url"}: title, channel, duration and thumbnail of ONE video.

    200 {video_id, url, title, channel, channel_id, duration, duration_text, thumbnail}
    422 the text is not a single-video YouTube address (field-level, loc ["body","url"])
    404/403/409/502/504/429 {"detail", "kind"}: the video is deleted/private, restricted (region,
    age, members), a live stream, YouTube not reachable, too slow, or too many probes at once.
    Nothing is downloaded and nothing is stored.
    """
    probe: YoutubeProbe = request.app.state.youtube
    ref = parse_video_url(body.url)
    try:
        info = await probe.probe(ref)
    except ProbeError as error:
        logger.info("youtube probe failed", extra={"video_id": ref.video_id, "kind": error.kind})
        return JSONResponse(
            {"detail": error.message, "kind": error.kind}, status_code=PROBE_STATUS[error.kind]
        )
    return JSONResponse(info.as_dict())


async def vpn_status(request: Request) -> JSONResponse:
    """GET /api/vpn: the watchdog's view of the VPN, for the UI badge (issue #22).

    `state` is ok, down, blind (the watchdog cannot read the VPN status), starting, or unknown
    (the watchdog does not answer). Always 200: "unknown" is an answer, not an error.
    """
    client: WatchdogStatusClient | None = request.app.state.watchdog_status
    if client is None:
        return JSONResponse({"state": "unknown", "reason": "not configured"})
    return JSONResponse(await asyncio.to_thread(client.fetch_status))


def register(app: FastAPI) -> None:
    """Add the API routes to the app (plain routes, so `app.routes` lists every path)."""
    app.add_api_route("/api/magnet", add_magnet, methods=["POST"])
    app.add_api_route("/api/vpn", vpn_status, methods=["GET"])
    app.add_api_route("/api/youtube/probe", youtube_probe, methods=["POST"])
