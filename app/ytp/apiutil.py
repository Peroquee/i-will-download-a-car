"""Small pieces shared by the API modules (issues #13, #26)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi.responses import JSONResponse

from . import jobs
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError, QbitUnavailable
from .db import Database
from .jobs import Job

logger = logging.getLogger(__name__)


# HTTP status per probe failure kind: the video itself (404), a restriction on it (403), the
# upstream or the VPN path (502/504), our own limit (429).
PROBE_STATUS = {
    "private": 404, "unavailable": 404, "not_found": 404, "unsupported": 422,
    "region": 403, "age": 403, "members": 403, "live": 409,
    "bot_check": 502, "network": 502, "unknown": 502, "timeout": 504, "busy": 429,
}  # fmt: skip


def problem(status: int, detail: str, job_id: int | None = None) -> JSONResponse:
    body: dict[str, Any] = {"detail": detail}
    if job_id is not None:
        body["job_id"] = job_id
    return JSONResponse(body, status_code=status)


async def hand_to_qbittorrent(
    db: Database, qbit: QbitClient, job: Job, magnet: str, category: str
) -> JSONResponse | None:
    """Give a freshly queued magnet job to qBittorrent. None = fine, else the error answer.

    On failure the job is marked `failed` with the reason (so it shows in the job list and can be
    retried) and the answer is 503 (unreachable or login refused) or 502 (it refused the torrent).
    The magnet itself is never logged: it can carry a tracker passkey.
    """
    try:
        await asyncio.to_thread(qbit.add_magnet, magnet, category)
    except QbitError as exc:
        reason = str(exc)
        await asyncio.to_thread(jobs.mark_failed, db, job.id, reason)
        logger.warning(
            "adding the torrent failed",
            extra={"job_id": job.id, "infohash": job.external_id, "error": reason},
        )
        unavailable = isinstance(exc, (QbitUnavailable, QbitAuthError))
        return problem(
            503 if unavailable else 502, f"qBittorrent did not take the torrent: {reason}", job.id
        )
    return None
