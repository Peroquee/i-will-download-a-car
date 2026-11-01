"""The job list and the retry action (issue #26).

GET /api/jobs?limit=50&status=failed
    {"jobs": [{"id", "type", "kind", "label", "status", "error", "progress", "can_retry",
               "created_at", "updated_at"}, ...],
     "counts": {"queued": n, "downloading": n, "organizing": n, "done": n, "failed": n}}

    Newest first. `kind` is the library (movies, series, audiobooks, music, youtube), `label` a
    readable name made from the stored metadata, `progress` 0-100 or null when unknown (queued,
    failed, or qBittorrent cannot be asked right now). The magnet link is never part of the answer.

POST /api/jobs/{id}/retry
    Only for a `failed` job: it goes back to `queued` and is handed over again (a torrent to
    qBittorrent, a video to the download worker). 200 with the job summary; 404 unknown job;
    409 not failed; 502/503 as for POST /api/magnet when qBittorrent refuses or is unavailable
    (the job is then `failed` again with that reason).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from fastapi import FastAPI, Request

from . import jobs
from .apiutil import hand_to_qbittorrent, problem
from .clients.qbittorrent import QbitClient, QbitError
from .jobs import Job
from .youtube_jobs import JOB_TYPE as YOUTUBE_JOB_TYPE

logger = logging.getLogger(__name__)

STATUSES = ("queued", "downloading", "organizing", "done", "failed")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
PROGRESS_CACHE_SECONDS = 3.0


def job_label(job: Job) -> str:
    """A readable name from the stored metadata; never raises on odd or old data."""
    meta = job.metadata
    kind = meta.get("type")

    def text(key: str) -> str:
        value = meta.get(key)
        return value if isinstance(value, str) else ""

    if kind == "movies":
        label = f"{text('title')} ({meta.get('year')})" if meta.get("year") else text("title")
    elif kind == "series":
        season = meta.get("season")
        episode = meta.get("episode")
        if isinstance(season, int):
            tag = f"S{season:02d}" + (f"E{episode:02d}" if isinstance(episode, int) else "")
            label = f"{text('title')} {tag}"
        else:
            label = text("title")
    elif kind == "audiobooks":
        label = f"{text('author')} - {text('title')}"
    elif kind == "music":
        label = f"{text('artist')} - {text('album')}"
    elif kind == "youtube":
        label = f"{text('channel')} - {text('title')}"
    else:
        label = ""
    label = label.strip(" -")
    return label or f"{job.type} {job.external_id}"


class TorrentProgress:
    """Progress of torrents by infohash, from one qBittorrent call cached for a few seconds.

    The page polls every few seconds; this keeps that from turning into a qBittorrent call per
    poll and per viewer. A failure gives an empty answer (progress unknown), never an error.
    """

    def __init__(self, client: QbitClient | None, ttl: float = PROGRESS_CACHE_SECONDS):
        self._client = client
        self._ttl = ttl
        self._lock = threading.Lock()
        self._fetched_at = float("-inf")
        self._values: dict[str, int] = {}

    def get(self) -> dict[str, int]:
        if self._client is None:
            return {}
        with self._lock:
            if time.monotonic() - self._fetched_at < self._ttl:
                return self._values
            try:
                torrents = self._client.list_torrents()
                self._values = {t.hash.lower(): int(t.progress * 100) for t in torrents}
            except QbitError as exc:
                logger.info("torrent progress unavailable", extra={"error": str(exc)})
                self._values = {}
            self._fetched_at = time.monotonic()
            return self._values


def job_progress(job: Job, torrents: dict[str, int], youtube: dict[int, int]) -> int | None:
    if job.status in ("done", "organizing"):
        return 100
    if job.status != "downloading":
        return None
    if job.type == YOUTUBE_JOB_TYPE:
        return youtube.get(job.id)
    return torrents.get(job.external_id.lower())


def summarize(job: Job, torrents: dict[str, int], youtube: dict[int, int]) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "kind": job.metadata.get("type"),
        "label": job_label(job),
        "status": job.status,
        "error": job.error,
        "progress": job_progress(job, torrents, youtube),
        "can_retry": job.status == "failed",
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _db_or_503(request: Request):
    db = request.app.state.db
    if db is None:
        return None, problem(503, "the database is not available")
    return db, None


async def list_jobs(request: Request, limit: int = DEFAULT_LIMIT, status: str | None = None):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    if status is not None and status not in STATUSES:
        return problem(422, f"status must be one of: {', '.join(STATUSES)}")
    limit = max(1, min(limit, MAX_LIMIT))
    rows = await asyncio.to_thread(jobs.list_jobs, db, limit=limit, status=status)
    counts = await asyncio.to_thread(jobs.status_counts, db)
    torrents: dict[str, int] = {}
    if any(j.status == "downloading" and j.type != YOUTUBE_JOB_TYPE for j in rows):
        torrents = await asyncio.to_thread(request.app.state.torrent_progress.get)
    worker = request.app.state.youtube_worker
    youtube = dict(worker.progress) if worker is not None else {}
    return {"jobs": [summarize(j, torrents, youtube) for j in rows], "counts": counts}


async def retry_job(job_id: int, request: Request):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    job = await asyncio.to_thread(jobs.get_job, db, job_id)
    if job is None:
        return problem(404, "no such job")
    if job.status != "failed":
        return problem(409, "only a failed job can be retried", job.id)
    metadata_json = json.dumps(job.metadata)
    if job.type == YOUTUBE_JOB_TYPE:
        worker = request.app.state.youtube_worker
        if worker is None:
            return problem(503, "the download worker is not running", job.id)
        queued, outcome = await asyncio.to_thread(
            jobs.submit_job, db, job.type, job.external_id, metadata_json
        )
        if outcome != "requeued":
            return problem(409, "only a failed job can be retried", job.id)
        worker.wake()
        return summarize(queued, {}, {})
    if job.type != "magnet":
        return problem(409, "this kind of job cannot be retried", job.id)
    qbit = request.app.state.qbit
    if qbit is None:
        return problem(503, "qBittorrent is not configured", job.id)
    source = await asyncio.to_thread(jobs.get_source, db, job.id)
    magnet = source or f"magnet:?xt=urn:btih:{job.external_id}"
    queued, outcome = await asyncio.to_thread(
        jobs.submit_magnet_job, db, job.external_id, metadata_json
    )
    if outcome != "requeued":
        return problem(409, "only a failed job can be retried", job.id)
    category = str(job.metadata.get("type", ""))
    answer = await hand_to_qbittorrent(db, qbit, queued, magnet, category)
    if answer is not None:
        return answer
    return summarize(queued, {}, {})


def register(app: FastAPI) -> None:
    app.add_api_route("/api/jobs", list_jobs, methods=["GET"])
    app.add_api_route("/api/jobs/{job_id}/retry", retry_job, methods=["POST"])
