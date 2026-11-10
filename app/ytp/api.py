"""The HTTP API (issue #13). Mounted by `create_app`; sits behind the reverse proxy's login.

POST /api/magnet
    {"magnet": "magnet:?xt=urn:btih:...", "metadata": {"type": "movies", "title": ..., "year": ...},
     "high_priority": false}

    Validates the link and the per-type metadata (field-level 422 errors), records a job keyed by
    the infohash, and hands the torrent to qBittorrent in the category named by `metadata.type`.
    `high_priority` (#51) starts the job before normal queued downloads and holds those back
    while it runs.

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
import time
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator
from pydantic_core import PydanticCustomError

from . import (
    accounts,
    alerts_api,
    dashboard_api,
    jobs,
    jobs_api,
    podcasts_api,
    resolution,
    settings_api,
    subscriptions_api,
    youtube_cookies_api,
)
from .apiutil import PROBE_STATUS, hand_to_qbittorrent, problem
from .clients.qbittorrent import QbitClient
from .clients.watchdog import WatchdogStatusClient
from .db import Database
from .magnet import InvalidMagnet, infohash_from_magnet
from .metadata import Metadata, Name, metadata_to_json
from .netstatus import watchdog_view
from .youtube import InvalidVideoUrl, ProbeError, YoutubeProbe, parse_video_url
from .youtube_jobs import JOB_TYPE as YOUTUBE_JOB_TYPE
from .youtube_jobs import YoutubeJobMetadata, YoutubeWorker, job_external_id

logger = logging.getLogger(__name__)


class MagnetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    magnet: str
    metadata: Metadata
    # Start before normal queued downloads and hold them back while this one runs (#51).
    high_priority: StrictBool = False

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
        jobs.submit_magnet_job,
        db,
        infohash,
        metadata_to_json(body.metadata),
        body.magnet,
        priority=1 if body.high_priority else 0,
    )
    # Whoever asked sees the job (also when somebody asked for the same torrent before).
    await asyncio.to_thread(accounts.add_job_owner, db, job.id, request.state.account.id)
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
    # The resolution cap chosen in the form (#47), validated in the handler below so a bad value
    # is a clear 400 rather than FastAPI's 422. Missing = the server default.
    max_height: object = None
    # Music only (issue #36): `channel` is then the artist, `title` the track. Left out, the track
    # is a single: the album is named like the track.
    album: Name | None = None
    # Start before normal queued downloads and hold them back while this one runs (#51).
    high_priority: StrictBool = False

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

    Optional `max_height` (#47): 0 (best) or one of 2160, 1440, 1080, 720, 480, 360. It caps the
    video stream of THIS job; missing = the account's `default_max_height`, else the server
    default (`YOUTUBE_MAX_HEIGHT`); audio-only downloads ignore it. Any other value is a 400.

    The resolution right (#65): without `can_set_resolution` (and not an admin) the account may
    not send a value other than the one that already applies to it; that is a 403 with a clear
    message, never silently ignored. Admins may always choose.

    Optional `high_priority` (#51): true starts this job before normal queued downloads and holds
    normal work back while it runs.
    """
    chosen = body.max_height
    if chosen is not None and not resolution.valid(chosen):
        return problem(400, resolution.DETAIL)
    db: Database | None = request.app.state.db
    worker: YoutubeWorker | None = request.app.state.youtube_worker
    if db is None or worker is None:
        return problem(503, "The database is not available; see /health.")
    account = request.state.account
    try:
        max_height = resolution.for_download(
            allowed=resolution.may_choose(account.is_admin, account.can_set_resolution),
            default_max_height=account.default_max_height,
            chosen=chosen,
            server_default=request.app.state.defaults.max_height(),
        )
    except resolution.NotAllowed as error:
        return problem(403, str(error))
    ref = parse_video_url(body.url)
    metadata = YoutubeJobMetadata(
        video_id=ref.video_id,
        channel=body.channel,
        title=body.title,
        target=body.target,
        audio_only=body.audio_only,
        max_height=max_height,
        album=(body.album or body.title) if body.target == "music" else None,
    )
    job, outcome = await asyncio.to_thread(
        jobs.submit_job,
        db,
        YOUTUBE_JOB_TYPE,
        job_external_id(ref.video_id, body.target),
        metadata.to_json(),
        priority=1 if body.high_priority else 0,
    )
    await asyncio.to_thread(accounts.add_job_owner, db, job.id, request.state.account.id)
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


async def youtube_options(request: Request) -> JSONResponse:
    """GET /api/youtube/options: the resolution of the logged-in account and the fixed choices.

    200 {"max_height": 1080, "choices": [0, 2160, 1440, 1080, 720, 480, 360], "can_choose": true}

    `max_height` is the account's effective default (#65): its `default_max_height` when set,
    else the server-wide default (an admin's value, #79, else `YOUTUBE_MAX_HEIGHT`; 0 = best).
    `can_choose` says whether the account may pick a resolution at all (an admin always may); the
    page disables the dropdown when it is false. The list is fixed; no formats are fetched from
    YouTube.
    """
    account = request.state.account
    return JSONResponse(
        {
            "max_height": resolution.effective_default(
                account.default_max_height, request.app.state.defaults.max_height()
            ),
            "choices": list(resolution.MAX_HEIGHTS),
            "can_choose": resolution.may_choose(account.is_admin, account.can_set_resolution),
        }
    )


async def vpn_status(request: Request) -> JSONResponse:
    """GET /api/vpn: the watchdog's view of the VPN, for the UI badge (issue #22).

    `state` is ok, down, blind (the watchdog cannot read the VPN status), starting, or unknown
    (the watchdog does not answer). Always 200: "unknown" is an answer, not an error.

    Since #81 the watchdog's own answer also says WHY and whether the tunnel is connected, besides
    gluetun's "running": `cause` (stopped | unreachable | tunnel, empty while up), `tunnel`
    (connected | reconnecting | down | unknown), `tunnel_since`, `probe` (off | unknown | ok |
    failing), `restart` (off | armed | denied) and `restarts`. `reconnecting` is still `state: ok`
    (a few minutes' grace); `state: down` with `cause: tunnel` is the process running while the
    tunnel is dead. None of it holds an address.

    The dashboard's two independent signals are added next to the unchanged watchdog fields
    (issue #63):

    * `internet`: {state: ok | offline | unknown, checked_at[, reason]}, a DNS + TCP check on the
      app's normal egress path to INTERNET_CHECK_HOST:INTERNET_CHECK_PORT, cached by the checker.
      With an empty host the check is off: `unknown` with `reason: "disabled"` (`error` = the
      check itself failed unexpectedly).
    * `watchdog`: {state: ok | blind | stale | unreachable | starting, last_poll}, whether the
      watchdog answers and its last poll is fresh. The VPN's own state is separate (top-level
      `state`), so a watchdog that answers "down" is still healthy.

    In the first `STARTUP_GRACE_SECONDS` after the app started, a watchdog that does not answer
    yet is reported as `starting`: during an install or an update the containers restart one
    after the other, and "the watchdog is not answering" would only alarm (issue #43).
    """
    client: WatchdogStatusClient | None = request.app.state.watchdog_status
    if client is None:
        status: dict[str, object] = {"state": "unknown", "reason": "not configured"}
    else:
        status = await asyncio.to_thread(client.fetch_status)
        uptime = time.monotonic() - request.app.state.started_at
        if status.get("state") == "unknown" and uptime < STARTUP_GRACE_SECONDS:
            status = {"state": "starting", "reason": "the app has just started"}
    internet = await asyncio.to_thread(request.app.state.internet.refresh)
    return JSONResponse({**status, "internet": internet, "watchdog": watchdog_view(status)})


STARTUP_GRACE_SECONDS = 60


_FORM_MISTAKE_TYPES = {"season_invalid", "episode_multi_season"}


async def _form_mistake_is_a_400(request: Request, exc: RequestValidationError):
    """A form mistake about seasons answers 400, not FastAPI's generic 422 (#52).

    Two cases: a season spec that does not parse, and an episode number together with several
    seasons (which names no single episode).
    """
    for error in exc.errors():
        if error.get("type") in _FORM_MISTAKE_TYPES:
            return problem(400, str(error.get("msg") or "invalid season"))
    return await request_validation_exception_handler(request, exc)


def register(app: FastAPI) -> None:
    """Add the API routes to the app (plain routes, so `app.routes` lists every path)."""
    # One deliberate exception to the generic 422: the season and episode fields of a magnet
    # job (#52).
    app.add_exception_handler(RequestValidationError, _form_mistake_is_a_400)
    app.add_api_route("/api/magnet", add_magnet, methods=["POST"])
    app.add_api_route("/api/vpn", vpn_status, methods=["GET"])
    app.add_api_route("/api/youtube/probe", youtube_probe, methods=["POST"])
    app.add_api_route("/api/youtube/options", youtube_options, methods=["GET"])
    app.add_api_route("/api/youtube/download", youtube_download, methods=["POST"])
    jobs_api.register(app)
    alerts_api.register(app)
    dashboard_api.register(app)
    subscriptions_api.register(app)
    podcasts_api.register(app)
    youtube_cookies_api.register(app)
    settings_api.register(app)
