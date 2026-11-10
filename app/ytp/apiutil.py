"""Small pieces shared by the API modules (issues #13, #26)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from . import accounts, jobs, smart_info
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError, QbitUnavailable
from .db import Database
from .jobs import Job
from .poll_schedule import effective

logger = logging.getLogger(__name__)


# HTTP status per probe failure kind: the video itself (404), a restriction on it (403), the
# upstream or the VPN path (502/504), our own limit (429).
PROBE_STATUS = {
    "private": 404, "unavailable": 404, "not_found": 404, "unsupported": 422,
    "region": 403, "age": 403, "members": 403, "live": 409,
    "bot_check": 502, "network": 502, "unknown": 502, "timeout": 504, "busy": 429,
}  # fmt: skip


def visible_to(request: Request) -> int | None:
    """What a query must be limited to: None for an admin (sees everything), else the account id."""
    account = request.state.account
    return None if account.is_admin else account.id


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


# --- who follows what (issue #44): shared by the channel and the podcast subscription routes ---


async def followed_view(db: Database, kind: str, rows: list[Any], account) -> list[dict[str, Any]]:
    """The subscriptions an account may see, as dicts with `following` and (admin) `followers`.

    An admin sees every subscription, a user only the ones they follow. The names of the other
    followers are for admins only. The follower names of every subscription come from ONE
    grouped query, not two queries per row (#76)."""
    mine = await asyncio.to_thread(accounts.followed_ids, db, kind, account.id)
    names = await asyncio.to_thread(accounts.follower_names_by_subscription, db, kind)
    result = []
    for row in rows:
        if not account.is_admin and row.id not in mine:
            continue
        entry = row.as_dict()
        entry["following"] = row.id in mine
        followers = names.get(row.id, [])
        entry["follower_count"] = len(followers)
        if account.is_admin:
            entry["followers"] = followers
        result.append(entry)
    return result


async def may_use(db: Database, kind: str, subscription_id: int, account) -> bool:
    """May this account act on the subscription (follow it, or be an admin)?"""
    if account.is_admin:
        return True
    return await asyncio.to_thread(accounts.follows, db, kind, subscription_id, account.id)


def smart_views(defaults, db: Database, kind: str, rows: dict[int, Any]) -> dict[int, dict]:
    """What the smart mode knows about each listed subscription (`smart_info.describe`), by id.
    A failure for one subscription leaves it out: the list must not break because of it (#80)."""
    now = datetime.now(UTC)
    settings = defaults.smart_settings()
    base = defaults.schedule(kind)
    found: dict[int, dict] = {}
    for subscription_id, row in rows.items():
        try:
            schedule = effective(
                row.poll_minutes, row.poll_at, base.minutes, base.at, row.poll_mode, base.mode
            )
            found[subscription_id] = smart_info.describe(
                db, kind, subscription_id, schedule, settings, defaults.zone, now
            )
        except Exception:
            logger.exception("smart info failed", extra={"subscription_id": subscription_id})
    return found
