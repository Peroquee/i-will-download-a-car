"""The dashboard's data (issue #38): GET /api/dashboard.

    {"active": [{id, type, kind, label, status, high_priority, progress, done_bytes,
                 total_bytes, speed, eta, created_at, updated_at}, ...],
     "recent": [{id, type, kind, label, status, error, note, can_retry, dismissed,
                 high_priority, created_at, updated_at}, ...],
     "dismissed": n,
     "vpn": {"current": {state, since, paused, egress, country}, "history": [{time, condition,
             paused, reason}, ...]},
     "disk": {free_percent, free_gb, total_gb} | null,
     "deploy": {deployed, deployed_at, result, message, paused, halted, checked_at} | null,
     "backlogs": [{id, folder, state, total, done, failed, waiting, skipped, size,
                   paused_reason, created_at}, ...]}

* `active`: everything queued, downloading or organizing, of every kind (torrent, YouTube,
  podcast). `progress` is a percent, `done_bytes`/`total_bytes` sizes, `speed` bytes per second and
  `eta` seconds; each is null while unknown (a queued job, metadata still loading, the worker has
  not reported yet). Torrent numbers come from the same cached qBittorrent call as the job list.
* `backlogs` (#45): the "download all episodes" runs the account may see (an admin sees all):
  unfinished ones and those that ended within a day. The page shows "Back catalogue: 40 of 312".
* `deploy` (#42) is the small status file the automatic update writes into the app's config
  folder (`autodeploy-status.json`: the installed version, the last result, paused or halted; no
  address and no secret), or null when automatic updates are not set up.
* `recent`: the 20 newest done/failed jobs with the reason for a failure (and the note of a done
  job that skipped files, #69); `can_retry` is true for
  a failed one (the retry itself is POST /api/jobs/{id}/retry). Failed jobs the asking account
  dismissed (POST /api/jobs/{id}/dismiss) are left out unless `?dismissed=1` is given; `dismissed`
  counts them either way (the page's "show dismissed (N)" toggle).
* `vpn.current` is the watchdog's status reduced to what is safe to show (no address); `history`
  the last changes of the VPN condition the app recorded, newest first.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import alertlog, jobs, podcast_backlog
from .apiutil import problem, visible_to
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
USER_VPN_KEYS = ("state", "since", "paused")  # no exit country for a user


DEPLOY_KEYS = ("deployed", "deployed_at", "result", "message", "paused", "halted", "checked_at")


def deploy_status(config_dir: Path) -> dict[str, Any] | None:
    """The automatic update's public status, reduced to known keys; None if there is none."""
    try:
        data = json.loads((config_dir / "autodeploy-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("enabled") is not True:
        return None
    return {key: data.get(key) for key in DEPLOY_KEYS}


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
        "high_priority": job.priority > 0,
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


def recent_row(job: Job, dismissed: bool = False) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "kind": job.metadata.get("type"),
        "label": job_label(job),
        "status": job.status,
        "error": job.error,
        "note": job.note,
        "can_retry": job.status == "failed",
        "dismissed": dismissed,
        "high_priority": job.priority > 0,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


async def dashboard(request: Request, dismissed: int = 0):
    db = request.app.state.db
    if db is None:
        return problem(503, "The database is not available; see /health.")
    state = request.app.state
    account = request.state.account
    owner = visible_to(request)  # a user sees their own jobs, an admin all of them
    active = await asyncio.to_thread(jobs.active_jobs, db, owner)
    viewer = account.id
    recent = await asyncio.to_thread(
        jobs.recent_finished, db, RECENT, owner, viewer, include_dismissed=bool(dismissed)
    )
    hidden = await asyncio.to_thread(jobs.dismissed_ids, db, viewer, [j.id for j in recent])
    dismissed_total = await asyncio.to_thread(jobs.dismissed_count, db, owner, viewer)
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
    keys = SAFE_VPN_KEYS if account.is_admin else USER_VPN_KEYS
    current = {key: status[key] for key in keys if key in status}
    current.setdefault("state", "unknown")
    # The history, the free space and the update status are the owner's business, not a user's.
    history = []
    disk = None
    deploy = None
    if account.is_admin:
        history = await asyncio.to_thread(alertlog.vpn_history, db, HISTORY)
        disk = await asyncio.to_thread(disk_summary, state.settings.data_root)
        deploy = await asyncio.to_thread(deploy_status, state.settings.config_dir)
    backlogs = await asyncio.to_thread(podcast_backlog.visible, db, owner)
    return JSONResponse(
        {
            "backlogs": [run.as_dict() for run in backlogs],
            "active": [active_row(j, torrents, progress, details) for j in active],
            "recent": [recent_row(j, j.id in hidden) for j in recent],
            "dismissed": dismissed_total,
            "vpn": {"current": current, "history": history},
            "disk": disk,
            "deploy": deploy,
        }
    )


def register(app: FastAPI) -> None:
    app.add_api_route("/api/dashboard", dashboard, methods=["GET"])
