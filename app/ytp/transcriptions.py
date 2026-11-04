"""The Whisper transcription queue (docs/transcription.md).

A YouTube video filed without subtitles (`ytp.youtube_download`) gets a row in `transcriptions`.
The app never runs Whisper itself (it has no GPU): a PC with one is woken over the network when
rows are waiting (`scripts/transcriber/wake.py` on the media VM), boots from the network, and its
worker (`scripts/transcriber/pc/worker.py`) talks to the app over `ytp.transcriber_api`:

1. **claim** the oldest queued row: it becomes `running`, gets a fresh random `media_key` and an
   attempt is counted;
2. **stream** the video from `/api/transcriber/jobs/<id>/media?key=<media_key>` (only this file,
   only while this attempt runs);
3. send a **heartbeat** while Whisper works (progress for the log; a row whose heartbeat stops
   for `LEASE_SECONDS` goes back to the queue: the PC crashed, lost power or was switched off);
4. send the **result** (SRT text and the language Whisper heard) or a **failure**.

The app files the SRT itself, next to the video as `<video stem>.<lang>.srt` (the name Jellyfin
reads as an external subtitle), written to a temporary name and renamed, never over an existing
subtitle, and asks Jellyfin to refresh that library. So the PC needs no access to the media share
and cannot write anywhere: everything it sends is validated here.

A failed attempt is retried up to `MAX_ATTEMPTS` times in all, then the row is `failed` and an
alert says so (the "never fail silently" rule). A video that is gone by the time it is claimed
fails at once.

Plain functions over `ytp.db.Database`, like `ytp.jobs`; standard library only.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import alertlog
from .db import Database
from .youtube_download import subtitle_path

logger = logging.getLogger(__name__)

STATUSES = ("queued", "running", "done", "failed")
MAX_ATTEMPTS = 3
LEASE_SECONDS = 600  # no heartbeat for 10 minutes = the PC is gone
MAX_SRT_BYTES = 10 * 1024**2
MAX_ERROR_LENGTH = 500
WORKER_STATE_KEY = "transcriber.worker"
_LANG = re.compile(r"[a-z]{2,3}")
_WORKER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_CUE_TIME = re.compile(
    r"^\d{1,3}:[0-5]\d:[0-5]\d[,.]\d{3} --> \d{1,3}:[0-5]\d:[0-5]\d[,.]\d{3}\s*$", re.M
)


class TranscriptionError(Exception):
    """A request of the transcription PC that cannot be done. `kind` picks the HTTP status."""

    def __init__(self, kind: str, message: str):
        self.kind = kind  # not_found | conflict | invalid | gone
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Transcription:
    id: int
    job_id: int | None
    media_path: str
    language: str | None
    status: str
    attempts: int
    error: str | None
    worker: str | None
    media_key: str | None
    progress: int | None
    heartbeat_at: str | None
    result_lang: str | None
    result_path: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Transcription:
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


def get(db: Database, transcription_id: int) -> Transcription | None:
    rows = db.query("SELECT * FROM transcriptions WHERE id = ?", (transcription_id,))
    return Transcription.from_row(rows[0]) if rows else None


def valid_language(value: object) -> str | None:
    """An ISO 639-1/-2 code in lower case (`de`, `en`, `yue`), or None."""
    text = str(value or "").strip().lower()
    return text if _LANG.fullmatch(text) else None


def relative_media_path(data_root: Path, video: Path) -> str:
    """The video's path below DATA_ROOT, as stored. Raises ValueError for anything outside."""
    return video.resolve().relative_to(data_root.resolve()).as_posix()


# ---------------------------------------------------------------------------------- queueing


def enqueue(
    db: Database,
    data_root: Path,
    video: Path,
    *,
    language: str | None = None,
    job_id: int | None = None,
) -> bool:
    """Queue `video` for Whisper. True if it is (now) waiting, False if it was done before.

    Idempotent: a video is queued once (keyed by its path). One that failed before is queued
    again with fresh attempts: downloading the same video again is how a person retries.
    """
    relative = relative_media_path(data_root, video)
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO transcriptions (job_id, media_path, language) VALUES (?, ?, ?) "
            "ON CONFLICT (media_path) DO NOTHING",
            (job_id, relative, valid_language(language)),
        )
        if cursor.rowcount == 1:
            logger.info("transcription queued", extra={"media": relative})
            return True
        row = conn.execute(
            "SELECT status FROM transcriptions WHERE media_path = ?", (relative,)
        ).fetchone()
        if row["status"] == "failed":
            conn.execute(
                "UPDATE transcriptions SET status = 'queued', attempts = 0, error = NULL, "
                "media_key = NULL, progress = NULL, worker = NULL, language = ? "
                "WHERE media_path = ?",
                (valid_language(language), relative),
            )
            return True
        return row["status"] in ("queued", "running")


def release_stale(db: Database, lease_seconds: int = LEASE_SECONDS) -> int:
    """Running rows whose PC stopped sending heartbeats go back to the queue (or fail when they
    used up their attempts). Returns how many were released."""
    cutoff = f"-{int(lease_seconds)} seconds"
    with db.transaction() as conn:
        stale = conn.execute(
            "SELECT * FROM transcriptions WHERE status = 'running' AND (heartbeat_at IS NULL "
            "OR heartbeat_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?))",
            (cutoff,),
        ).fetchall()
        for row in stale:
            _retry_or_fail(conn, row, "the transcription PC stopped answering")
    return len(stale)


def claim(db: Database, data_root: Path, worker: str) -> Transcription | None:
    """Take the oldest queued row for `worker` (it becomes `running`); None if there is none.

    One transaction, so two PCs can never take the same video. A row whose video is gone (deleted
    in the meantime) fails on the spot and the next one is taken.
    """
    if not _WORKER.fullmatch(worker):
        raise TranscriptionError("invalid", "worker must be 1-64 letters, digits, . _ -")
    release_stale(db)
    touch_worker(db, worker)
    while True:
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM transcriptions WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            if media_file(data_root, row["media_path"]) is None:
                conn.execute(
                    "UPDATE transcriptions SET status = 'failed', error = ? WHERE id = ?",
                    ("the video is not in the library any more", row["id"]),
                )
                continue
            conn.execute(
                "UPDATE transcriptions SET status = 'running', attempts = attempts + 1, "
                "error = NULL, worker = ?, media_key = ?, progress = 0, "
                "heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (worker, secrets.token_urlsafe(32), row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM transcriptions WHERE id = ?", (row["id"],)
            ).fetchone()
        logger.info(
            "transcription claimed",
            extra={
                "transcription_id": claimed["id"],
                "worker": worker,
                "attempt": claimed["attempts"],
            },
        )
        return Transcription.from_row(claimed)


def media_file(data_root: Path, media_path: str) -> Path | None:
    """The stored path as a file below DATA_ROOT, or None (gone, or would lead outside)."""
    root = data_root.resolve()
    candidate = (root / media_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def media_for(
    db: Database, data_root: Path, transcription_id: int, key: str
) -> tuple[Transcription, Path]:
    """The file a running attempt may stream, checked against its `media_key`."""
    row = get(db, transcription_id)
    if row is None or row.status != "running" or not row.media_key:
        raise TranscriptionError("not_found", "no running transcription with this id")
    if not hmac.compare_digest(row.media_key.encode(), key.encode()):
        raise TranscriptionError("not_found", "no running transcription with this id")
    path = media_file(data_root, row.media_path)
    if path is None:
        raise TranscriptionError("gone", "the video is not in the library any more")
    return row, path


def heartbeat(db: Database, transcription_id: int, progress: int | None = None) -> bool:
    """The PC is still on it. False if the row is not running any more (released, or done)."""
    value = None if progress is None else max(0, min(100, int(progress)))
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE transcriptions SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
            "progress = COALESCE(?, progress) WHERE id = ? AND status = 'running'",
            (value, transcription_id),
        )
        return cursor.rowcount == 1


def fail(db: Database, transcription_id: int, error: str) -> Transcription:
    """The PC could not do it: try again later, or give up after `MAX_ATTEMPTS`."""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM transcriptions WHERE id = ? AND status = 'running'", (transcription_id,)
        ).fetchone()
        if row is None:
            raise TranscriptionError("conflict", "this transcription is not running")
        _retry_or_fail(conn, row, error.strip() or "the transcription failed")
        return Transcription.from_row(
            conn.execute(
                "SELECT * FROM transcriptions WHERE id = ?", (transcription_id,)
            ).fetchone()
        )


def _retry_or_fail(conn: sqlite3.Connection, row: sqlite3.Row, error: str) -> None:
    error = error[:MAX_ERROR_LENGTH]
    if row["attempts"] < MAX_ATTEMPTS:
        conn.execute(
            "UPDATE transcriptions SET status = 'queued', error = ?, media_key = NULL, "
            "progress = NULL WHERE id = ?",
            (error, row["id"]),
        )
        logger.warning(
            "transcription attempt failed, queued again",
            extra={"transcription_id": row["id"], "attempt": row["attempts"], "reason": error},
        )
        return
    conn.execute(
        "UPDATE transcriptions SET status = 'failed', error = ?, media_key = NULL WHERE id = ?",
        (error, row["id"]),
    )
    name = Path(row["media_path"]).name
    alertlog.insert(
        conn,
        "transcription_failed",
        "warning",
        f"A transcription failed: {name}",
        f"Whisper could not transcribe {name} after {row['attempts']} attempts: {error}",
        {"name": name, "reason": error, "attempts": row["attempts"]},
        key=f"transcription:{row['id']}:{row['updated_at']}",
    )
    logger.warning("transcription failed", extra={"transcription_id": row["id"], "reason": error})


# ---------------------------------------------------------------------------------- the result


def normalise_srt(text: str) -> str:
    """The SRT as it is filed: UTF-8 text, `\\n` line ends, ending in one newline.

    Raises TranscriptionError("invalid") for something that is not an SRT (an empty text is
    allowed and means: no speech in the video, no file).
    """
    if len(text.encode("utf-8", errors="replace")) > MAX_SRT_BYTES:
        raise TranscriptionError("invalid", "the subtitle text is too large")
    if "\x00" in text:
        raise TranscriptionError("invalid", "the subtitle text contains NUL characters")
    cleaned = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""
    if not _CUE_TIME.search(cleaned):
        raise TranscriptionError("invalid", "this is not SRT: no cue with a time line")
    return cleaned + "\n"


def complete(
    db: Database, data_root: Path, transcription_id: int, language: str, srt: str
) -> Transcription:
    """File the SRT the PC sent and mark the row `done`. Returns the updated row.

    An existing subtitle in that language (from YouTube, or one a person put there) is never
    replaced; the row is done all the same. An empty SRT (no speech) files nothing.
    """
    lang = valid_language(language)
    if lang is None:
        raise TranscriptionError("invalid", "language must be an ISO 639 code like 'de'")
    text = normalise_srt(srt)
    row = get(db, transcription_id)
    if row is None or row.status != "running":
        raise TranscriptionError("conflict", "this transcription is not running")
    video = media_file(data_root, row.media_path)
    if video is None:
        raise TranscriptionError("gone", "the video is not in the library any more")
    target: Path | None = None
    if text:
        target = subtitle_path(video, lang)
        if target.exists() or target.is_symlink():
            logger.info("subtitle exists, leaving it", extra={"target": target.name})
        else:
            _write_atomically(target, text)
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE transcriptions SET status = 'done', error = NULL, media_key = NULL, "
            "progress = 100, result_lang = ?, result_path = ? WHERE id = ? AND status = 'running'",
            (
                lang,
                relative_media_path(data_root, target) if target is not None else None,
                transcription_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TranscriptionError("conflict", "this transcription is not running")
    logger.info(
        "transcription filed",
        extra={"transcription_id": transcription_id, "language": lang, "empty": not text},
    )
    done = get(db, transcription_id)
    assert done is not None
    return done


def _write_atomically(target: Path, text: str) -> None:
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------------- status


def touch_worker(db: Database, worker: str) -> None:
    """Remember that a transcription PC asked (the wake script does not wake a PC that is up)."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (WORKER_STATE_KEY, json.dumps({"worker": worker})),
        )


# YouTube jobs that may still end up in the queue: not finished, and not audio only / music.
PENDING_DOWNLOADS_SQL = (
    "SELECT COUNT(*) AS n FROM jobs WHERE type = 'youtube' "
    "AND status IN ('queued', 'downloading', 'organizing') "
    "AND COALESCE(json_extract(metadata, '$.target'), 'video') IN ('video', 'podcast') "
    "AND COALESCE(json_extract(metadata, '$.audio_only'), 0) = 0"
)


def status(db: Database) -> dict[str, Any]:
    """What the wake script and the PC decide on: is there (or will there soon be) work?

    `pending_downloads` are YouTube downloads still in progress that may end without subtitles,
    i.e. *potential* transcriptions: the PC stays up a while for them instead of shutting down
    and being woken again ten minutes later.
    """
    release_stale(db)
    counts = {name: 0 for name in STATUSES}
    for row in db.query("SELECT status, COUNT(*) AS n FROM transcriptions GROUP BY status"):
        counts[row["status"]] = row["n"]
    pending = db.query(PENDING_DOWNLOADS_SQL)[0]["n"]
    seen = db.query("SELECT updated_at FROM app_state WHERE key = ?", (WORKER_STATE_KEY,))
    return {
        **counts,
        "pending_downloads": pending,
        "worker_last_seen": seen[0]["updated_at"] if seen else None,
    }


def recent(db: Database, limit: int = 20) -> list[Transcription]:
    """The newest rows, newest first."""
    rows = db.query("SELECT * FROM transcriptions ORDER BY id DESC LIMIT ?", (limit,))
    return [Transcription.from_row(row) for row in rows]
