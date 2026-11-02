"""The dashboard's data (issue #38): GET /api/dashboard.

    {"active": [{id, type, kind, label, status, progress, done_bytes, total_bytes, speed, eta,
                 created_at, updated_at}, ...],
     "recent": [{id, type, kind, label, status, error, can_retry, created_at, updated_at}, ...],
     "vpn": {"current": {state, since, paused, egress, country}, "history": [{time, condition,
             paused, reason}, ...]},
     "disk": {free_percent, free_gb, total_gb} | null}

* `active`: everything queued, downloading or organizing, of every kind (torrent, YouTube,
  podcast). `progress` is a percent, `done_bytes`/`total_bytes` sizes, `speed` bytes per second and
  `eta` seconds; each is null while unknown (a queued job, metadata still loading, the worker has
  not reported yet). Torrent numbers come from the same cached qBittorrent call as the job list.
* `recent`: the 20 newest done/failed jobs with the reason for a failure; `can_retry` is true for
  a failed one (the retry itself is POST /api/jobs/{id}/retry).
* `vpn.current` is the watchdog's status reduced to what is safe to show (no address); `history`
  the last changes of the VPN condition the app recorded, newest first.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import alertlog, jobs
from .apiutil import problem
from .clients.qbittorrent import Torrent
from .jobs import Job
from .jobs_api import job_label
from .monitor import disk_summary
from .podcast_jobs import JOB_TYPE as PODCAST_JOB_TYPE
from .youtube_jobs import JOB_TYPE as YOUTUBE_JOB_TYPE

RECENT = 20
HISTORY = 30
ETA_UNKNOWN = 8640000  # qBittorrent's "infinity"
SAFE_VPN_KEYS = ("state", "since", "paused", "egress", "egress_country")


def active_row(
    job: Job,
    torrents: dict[str, Torrent],
    progress: dict[int, int],
    details: dict[int, dict[str, int | None]],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": job.id,
        "type": job.type,
        "kind": job.metadata.get("type"),
        "label": job_label(job),
        "status": job.status,
        "progress": None,
        "done_bytes": None,
        "total_bytes": None,
        "speed": None,
        "eta": None,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    if job.status == "organizing":
        row["progress"] = 100
    elif job.status == "downloading":
        if job.type in (YOUTUBE_JOB_TYPE, PODCAST_JOB_TYPE):
            row["progress"] = progress.get(job.id)
            row.update(details.get(job.id, {}))
        else:
            torrent = torrents.get(job.external_id.lower())
            if torrent is not None:
                row["progress"] = int(torrent.progress * 100)
                row["done_bytes"] = torrent.bytes_done or None
                row["total_bytes"] = torrent.size or None
                row["speed"] = torrent.dlspeed or None
                row["eta"] = torrent.eta if 0 < torrent.eta < ETA_UNKNOWN else None
    return row


def recent_row(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "kind": job.metadata.get("type"),
        "label": job_label(job),
        "status": job.status,
        "error": job.error,
        "can_retry": job.status == "failed",
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


async def dashboard(request: Request):
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    state = request.app.state
    active = await asyncio.to_thread(jobs.active_jobs, db)
    recent = await asyncio.to_thread(jobs.recent_finished, db, RECENT)
    torrents: dict[str, Torrent] = {}
    if any(
        j.status == "downloading" and j.type not in (YOUTUBE_JOB_TYPE, PODCAST_JOB_TYPE)
        for j in active
    ):
        torrents = await asyncio.to_thread(state.torrent_progress.details)
    progress: dict[int, int] = {}
    details: dict[int, dict[str, int | None]] = {}
    for worker in (state.youtube_worker, state.podcast_worker):
        if worker is not None:
            progress.update(worker.progress)
            details.update(worker.details)
    watchdog = state.watchdog_status
    status = await asyncio.to_thread(watchdog.fetch_status) if watchdog is not None else {}
    current = {key: status[key] for key in SAFE_VPN_KEYS if key in status}
    current.setdefault("state", "unknown")
    history = await asyncio.to_thread(alertlog.vpn_history, db, HISTORY)
    disk = await asyncio.to_thread(disk_summary, state.settings.data_root)
    return JSONResponse(
        {
            "active": [active_row(j, torrents, progress, details) for j in active],
            "recent": [recent_row(j) for j in recent],
            "vpn": {"current": current, "history": history},
            "disk": disk,
        }
    )


def register(app: FastAPI) -> None:
    app.add_api_route("/api/dashboard", dashboard, methods=["GET"])
