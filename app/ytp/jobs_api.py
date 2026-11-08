"""The job list and the retry action (issue #26).

GET /api/jobs?limit=50&status=failed
    {"jobs": [{"id", "type", "kind", "label", "status", "error", "progress", "can_retry",
               "dismissed", "created_at", "updated_at"}, ...],
     "counts": {"queued": n, "downloading": n, "organizing": n, "done": n, "failed": n,
                "dismissed": n}}

    Newest first. `kind` is the library (movies, series, audiobooks, music, youtube), `label` a
    readable name made from the stored metadata, `progress` 0-100 or null when unknown (queued,
    failed, or qBittorrent cannot be asked right now). The magnet link is never part of the answer.
    `counts.failed` leaves out the failed jobs the asking account dismissed (that is the number
    the input page shows in red); `counts.dismissed` is how many those are.

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

from . import accounts, jobs
from .apiutil import hand_to_qbittorrent, problem, visible_to
from .clients.qbittorrent import QbitClient, QbitError, Torrent
from .jobs import Job
from .podcast_jobs import JOB_TYPE as PODCAST_JOB_TYPE
from .youtube_jobs import JOB_TYPE as YOUTUBE_JOB_TYPE

logger = logging.getLogger(__name__)

STATUSES = ("queued", "downloading", "organizing", "done", "failed")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
PROGRESS_CACHE_SECONDS = 3.0


def _season_tag(meta: dict) -> str:
    """The season part of a series label: `S01-S03`, `S01,S02,S05` or the old `S03E07` (#52)."""
    seasons = meta.get("seasons")
    episode = meta.get("episode")
    if (
        isinstance(seasons, list)
        and seasons
        and all(isinstance(value, int) and not isinstance(value, bool) for value in seasons)
    ):
        ordered = sorted(set(seasons))
        if len(ordered) == 1:
            tag = f"S{ordered[0]:02d}"
            if isinstance(episode, int) and not isinstance(episode, bool):
                tag += f"E{episode:02d}"
            return tag
        if ordered == list(range(ordered[0], ordered[-1] + 1)):
            return f"S{ordered[0]:02d}{chr(0x2013)}S{ordered[-1]:02d}"
        return ",".join(f"S{season:02d}" for season in ordered)
    season = meta.get("season")
    if isinstance(season, int) and not isinstance(season, bool):
        tag = f"S{season:02d}"
        if isinstance(episode, int) and not isinstance(episode, bool):
            tag += f"E{episode:02d}"
        return tag
    return ""


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
        tag = _season_tag(meta)
        label = f"{text('title')} {tag}" if tag else text("title")
    elif kind == "audiobooks":
        label = f"{text('author')} - {text('title')}"
    elif kind == "music":
        label = f"{text('artist')} - {text('album')}"
    elif kind == "youtube":
        label = f"{text('channel')} - {text('title')}"
    elif kind == "podcast":
        label = f"{text('podcast')} - {text('title')}"
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
        self._torrents: dict[str, Torrent] = {}

    def _refresh(self) -> None:
        if time.monotonic() - self._fetched_at < self._ttl:
            return
        try:
            torrents = self._client.list_torrents()  # type: ignore[union-attr]
            self._torrents = {t.hash.lower(): t for t in torrents}
            self._values = {h: int(t.progress * 100) for h, t in self._torrents.items()}
        except QbitError as exc:
            logger.info("torrent progress unavailable", extra={"error": str(exc)})
            self._values = {}
            self._torrents = {}
        self._fetched_at = time.monotonic()

    def get(self) -> dict[str, int]:
        if self._client is None:
            return {}
        with self._lock:
            self._refresh()
            return self._values

    def details(self) -> dict[str, Torrent]:
        """The torrents themselves (size, speed, ETA) from the same cached call (#38)."""
        if self._client is None:
            return {}
        with self._lock:
            self._refresh()
            return self._torrents


def job_progress(job: Job, torrents: dict[str, int], youtube: dict[int, int]) -> int | None:
    if job.status in ("done", "organizing"):
        return 100
    if job.status != "downloading":
        return None
    if job.type in (YOUTUBE_JOB_TYPE, PODCAST_JOB_TYPE):
        return youtube.get(job.id)  # the workers' progress, by job id (one dict for both)
    return torrents.get(job.external_id.lower())


def summarize(
    job: Job, torrents: dict[str, int], youtube: dict[int, int], dismissed: bool = False
) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "kind": job.metadata.get("type"),
        "label": job_label(job),
        "status": job.status,
        "error": job.error,
        "progress": job_progress(job, torrents, youtube),
        "can_retry": job.status == "failed",
        "dismissed": dismissed,
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
    owner = visible_to(request)
    rows = await asyncio.to_thread(jobs.list_jobs, db, limit=limit, status=status, owner_id=owner)
    viewer = request.state.account.id
    counts = await asyncio.to_thread(jobs.status_counts, db, owner, viewer)
    counts["dismissed"] = await asyncio.to_thread(jobs.dismissed_count, db, owner, viewer)
    hidden = await asyncio.to_thread(jobs.dismissed_ids, db, viewer, [j.id for j in rows])
    torrents: dict[str, int] = {}
    own_work = (YOUTUBE_JOB_TYPE, PODCAST_JOB_TYPE)
    if any(j.status == "downloading" and j.type not in own_work for j in rows):
        torrents = await asyncio.to_thread(request.app.state.torrent_progress.get)
    worker = request.app.state.youtube_worker
    youtube = dict(worker.progress) if worker is not None else {}
    podcast_worker = request.app.state.podcast_worker
    if podcast_worker is not None:
        youtube.update(podcast_worker.progress)
    return {
        "jobs": [summarize(j, torrents, youtube, j.id in hidden) for j in rows],
        "counts": counts,
    }


async def retry_job(job_id: int, request: Request):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    job = await asyncio.to_thread(jobs.get_job, db, job_id)
    owner = visible_to(request)
    if job is None or (owner is not None and not accounts.owns_job(db, job_id, owner)):
        # Somebody else's job looks exactly like one that does not exist.
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
    if job.type == PODCAST_JOB_TYPE:
        podcast_worker = request.app.state.podcast_worker
        if podcast_worker is None:
            return problem(503, "the download worker is not running", job.id)
        queued, outcome = await asyncio.to_thread(
            jobs.submit_job, db, job.type, job.external_id, metadata_json
        )
        if outcome != "requeued":
            return problem(409, "only a failed job can be retried", job.id)
        podcast_worker.wake()
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


async def _own_job(request: Request, job_id: int):
    """(job, None) if the asking account may see the job, else (None, the 404/503 answer)."""
    db, failure = _db_or_503(request)
    if failure is not None:
        return None, failure
    job = await asyncio.to_thread(jobs.get_job, db, job_id)
    owner = visible_to(request)
    if job is None or (owner is not None and not accounts.owns_job(db, job_id, owner)):
        return None, problem(404, "no such job")
    return job, None


async def dismiss_job(job_id: int, request: Request):
    job, failure = await _own_job(request, job_id)
    if failure is not None:
        return failure
    stored = await asyncio.to_thread(
        jobs.dismiss, request.app.state.db, job_id, request.state.account.id
    )
    if not stored:
        return problem(409, "only a failed job can be dismissed", job.id)
    return {"id": job_id, "dismissed": True}


async def restore_job(job_id: int, request: Request):
    _, failure = await _own_job(request, job_id)
    if failure is not None:
        return failure
    await asyncio.to_thread(jobs.restore, request.app.state.db, job_id, request.state.account.id)
    return {"id": job_id, "dismissed": False}


async def dismiss_failed(request: Request):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    count = await asyncio.to_thread(
        jobs.dismiss_all_failed, db, visible_to(request), request.state.account.id
    )
    return {"dismissed": count}


def register(app: FastAPI) -> None:
    app.add_api_route("/api/jobs", list_jobs, methods=["GET"])
    app.add_api_route("/api/jobs/dismiss-failed", dismiss_failed, methods=["POST"])
    app.add_api_route("/api/jobs/{job_id}/dismiss", dismiss_job, methods=["POST"])
    app.add_api_route("/api/jobs/{job_id}/dismiss", restore_job, methods=["DELETE"])
    app.add_api_route("/api/jobs/{job_id}/retry", retry_job, methods=["POST"])
