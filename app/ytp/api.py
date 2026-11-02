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
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator
from pydantic_core import PydanticCustomError

from . import alerts_api, dashboard_api, jobs, jobs_api, podcasts_api, subscriptions_api
from .apiutil import PROBE_STATUS, hand_to_qbittorrent, problem
from .clients.qbittorrent import QbitClient
from .clients.watchdog import WatchdogStatusClient
from .db import Database
from .magnet import InvalidMagnet, infohash_from_magnet
from .metadata import Metadata, Name, metadata_to_json
from .youtube import InvalidVideoUrl, ProbeError, YoutubeProbe, parse_video_url
from .youtube_jobs import JOB_TYPE as YOUTUBE_JOB_TYPE
from .youtube_jobs import YoutubeJobMetadata, YoutubeWorker, job_external_id

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


async def add_magnet(body: MagnetRequest, request: Request) -> JSONResponse:
    db: Database | None = request.app.state.db
    qbit: QbitClient | None = request.app.state.qbit
    if db is None:
        return problem(503, "The database is not available; see /health.")
    if qbit is None:
        return problem(503, "qBittorrent is not configured (QBIT_PASSWORD is empty).")

    infohash = infohash_from_magnet(body.magnet)
    category = body.metadata.type  # the metadata types are the qBittorrent category names
    job, outcome = await asyncio.to_thread(
        jobs.submit_magnet_job, db, infohash, metadata_to_json(body.metadata), body.magnet
    )
    # Log the infohash, never the link: it can carry tracker URLs with a private passkey.
    logger.info("magnet job", extra={"job_id": job.id, "infohash": infohash, "outcome": outcome})
    summary = {"id": job.id, "infohash": infohash, "type": category, "status": job.status}
    if outcome == "duplicate":
        return JSONResponse({**summary, "duplicate": True}, status_code=200)

    failure = await hand_to_qbittorrent(db, qbit, job, body.magnet, category)
    if failure is not None:
        return failure
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


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    # The names the user confirmed (the probe suggests them): they become folder and file names.
    # For target "podcast" `channel` is the podcast and `title` the episode (issue #35).
    channel: Name
    title: Name
    target: Literal["video", "podcast", "music"] = "video"
    audio_only: StrictBool = False
    # Music only (issue #36): `channel` is then the artist, `title` the track. Left out, the track
    # is a single: the album is named like the track.
    album: Name | None = None

    @model_validator(mode="after")
    def _target_rules(self) -> DownloadRequest:
        if self.audio_only and self.target == "video":
            raise PydanticCustomError(
                "audio_only_invalid", "Audio only is for podcast episodes and music, not for videos"
            )
        if self.album is not None and self.target != "music":
            raise PydanticCustomError("album_invalid", "An album is only for music")
        return self

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


async def youtube_download(body: DownloadRequest, request: Request) -> JSONResponse:
    """POST /api/youtube/download {"url", "channel", "title"}: queue a download, return at once.

    201 {"id", "video_id", "type": "youtube", "status": "queued", "duplicate": false}  new job
    200 {... "duplicate": true}   the video is already queued, downloading or done
    422 invalid address or names (field-level)    503 the database is not available

    The download runs in the background, one at a time; the job's status says where it is
    (queued, downloading, organizing, done, failed with a reason). Sending a failed video again
    retries it. Files end up in `<MEDIA_DIR_YOUTUBE>/<channel>/<title> [<id>].mp4` with a
    thumbnail and a small `.info.json`.

    Optional `target` ("video", the default, "podcast" or "music") and `audio_only` (true only
    with "podcast" or "music"). With "podcast" the video becomes an episode in
    `<MEDIA_DIR_PODCASTS>/<podcast>/` (`channel` is the podcast, `title` the episode), named like
    an RSS episode; with `audio_only` the audio stream is kept (m4a/opus, no re-encode) with tags
    and cover. The same video can be queued once per target. 422 `audio_only_invalid` for audio
    only with the video target.

    "music" (issue #36): `channel` is the artist, `title` the track, `album` the album (default:
    the track's title, a single); the file goes to
    `<MEDIA_DIR_MUSIC>/<artist>/<album>/<title>.<ext>` like a torrent album, with
    artist/album/title tags and the cover when audio only.
    """
    db: Database | None = request.app.state.db
    worker: YoutubeWorker | None = request.app.state.youtube_worker
    if db is None or worker is None:
        return problem(503, "The database is not available; see /health.")
    ref = parse_video_url(body.url)
    metadata = YoutubeJobMetadata(
        video_id=ref.video_id,
        channel=body.channel,
        title=body.title,
        target=body.target,
        audio_only=body.audio_only,
        album=(body.album or body.title) if body.target == "music" else None,
    )
    job, outcome = await asyncio.to_thread(
        jobs.submit_job,
        db,
        YOUTUBE_JOB_TYPE,
        job_external_id(ref.video_id, body.target),
        metadata.to_json(),
    )
    logger.info(
        "youtube job", extra={"job_id": job.id, "video_id": ref.video_id, "outcome": outcome}
    )
    summary = {
        "id": job.id,
        "video_id": ref.video_id,
        "type": "youtube",
        "target": body.target,
        "status": job.status,
    }
    if outcome == "duplicate":
        return JSONResponse({**summary, "duplicate": True}, status_code=200)
    worker.wake()
    return JSONResponse({**summary, "duplicate": False}, status_code=201)


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
    app.add_api_route("/api/youtube/download", youtube_download, methods=["POST"])
    jobs_api.register(app)
    alerts_api.register(app)
    dashboard_api.register(app)
    subscriptions_api.register(app)
    podcasts_api.register(app)
