"""The alert log over HTTP (issue #38).

GET /api/alerts?limit=30&unread=1
    {"alerts": [{id, time, kind, severity, title, detail, params, acknowledged}, ...],
     "unread": n}
    Newest first. `limit` 1-200 (default 30); `unread=1` leaves out acknowledged ones. `title` and
    `detail` are English fallbacks; the page translates by `kind` and fills in `params`.

GET /api/alerts/{id}
    One alert; 404 if there is none (it may have been pruned).

POST /api/alerts/{id}/ack
    Mark it read. 200 with the alert; 404 unknown. Idempotent.

POST /api/alerts/ack-all
    Mark everything read. 200 {"acknowledged": n}.

All of it is behind the proxy login, and the POSTs pass the same-origin check of
`ytp.web.SecurityMiddleware` like every other state-changing route. No address or secret is ever
stored in an alert (see `ytp.alertlog`).
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import alertlog
from .apiutil import problem

DEFAULT_LIMIT = 30
MAX_LIMIT = 200


def _db_or_503(request: Request):
    db = request.app.state.db
    if db is None:
        return None, problem(503, "The database is not available; see /health.")
    return db, None


async def list_alerts(request: Request, limit: int = DEFAULT_LIMIT, unread: int = 0):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    limit = max(1, min(limit, MAX_LIMIT))
    rows = await asyncio.to_thread(alertlog.list_alerts, db, limit=limit, unread_only=bool(unread))
    unread_count = await asyncio.to_thread(alertlog.unread_count, db)
    return JSONResponse({"alerts": [a.as_dict() for a in rows], "unread": unread_count})


async def get_alert(alert_id: int, request: Request):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    alert = await asyncio.to_thread(alertlog.get, db, alert_id)
    if alert is None:
        return problem(404, "no such alert")
    return JSONResponse(alert.as_dict())


async def acknowledge(alert_id: int, request: Request):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    if not await asyncio.to_thread(alertlog.acknowledge, db, alert_id):
        return problem(404, "no such alert")
    alert = await asyncio.to_thread(alertlog.get, db, alert_id)
    return JSONResponse(alert.as_dict() if alert else {})


async def acknowledge_all(request: Request):
    db, failure = _db_or_503(request)
    if failure is not None:
        return failure
    count = await asyncio.to_thread(alertlog.acknowledge_all, db)
    return JSONResponse({"acknowledged": count})


def register(app: FastAPI) -> None:
    # ack-all is registered before the {alert_id} routes so it is never read as an id.
    app.add_api_route("/api/alerts/ack-all", acknowledge_all, methods=["POST"])
    app.add_api_route("/api/alerts", list_alerts, methods=["GET"])
    app.add_api_route("/api/alerts/{alert_id}", get_alert, methods=["GET"])
    app.add_api_route("/api/alerts/{alert_id}/ack", acknowledge, methods=["POST"])
