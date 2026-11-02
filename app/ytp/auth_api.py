"""Login, logout, the own password, and the account list for admins (issue #44).

POST /api/auth/login        {"username", "password"}
    200 {"account": {...}} and the session cookie; 401 {"detail"} for a wrong name or password
    (the same answer for an unknown name, a disabled account and a wrong password); 429
    {"detail", "retry_after"} with `Retry-After` when too many tries (see `auth.LoginLimiter`).
    A session that came with the request is ended first and a NEW one is made: the cookie value is
    never carried over from before the login (session fixation).
POST /api/auth/logout       the session ends, the cookie is cleared. 200 {"ok": true}
GET  /api/auth/me           200 {"account": {...}} or 401 (no valid session); the 401 carries
                            "setup": true when no account exists at all (nobody could sign in)
POST /api/auth/password     {"current", "new"}: 200 and the account's OTHER sessions end; 403 if
                            the current password is wrong (counts for the brake like a login)

Admins only:
GET    /api/accounts                          {"accounts": [{id, username, role, disabled, ...}]}
POST   /api/accounts                          {"username", "password", "role"}: 201 {"account"}
PATCH  /api/accounts/{id}                     {"role"?, "disabled"?}: 200 {"account"}
POST   /api/accounts/{id}/password            {"password"}: 200; ends that account's sessions
DELETE /api/accounts/{id}                     200; its follows end, a channel nobody follows ends
    409 `last_admin`: there must always be one active admin; 409 `exists`; 422 `invalid`.

Request bodies are read by hand, not through pydantic: a validation error of FastAPI quotes the
input it did not like, and for these routes that can be a password. No password is ever put into
an answer or a log line.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import accounts, passwords
from .accounts import Account, AccountError
from .apiutil import problem
from .auth import COOKIE, LoginLimiter, client_address

logger = logging.getLogger(__name__)

MAX_BODY = 4096
INVALID_LOGIN = "wrong name or password"
STATUS = {"invalid": 422, "exists": 409, "not_found": 404, "last_admin": 409}


async def read_body(request: Request, fields: dict[str, int]) -> dict[str, str] | JSONResponse:
    """The JSON object of the request with the named text fields (name -> longest allowed), or a
    422 that says only which field is wrong, never what was sent."""
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return problem(413, "the request is too large")
    try:
        data = await request.json()
    except ValueError:
        return problem(422, "the request is not JSON")
    if not isinstance(data, dict):
        return problem(422, "the request must be a JSON object")
    values: dict[str, str] = {}
    for name, limit in fields.items():
        value = data.get(name)
        if not isinstance(value, str) or not 0 < len(value) <= limit:
            return problem(422, f"{name} is missing or not a text of 1 to {limit} characters")
        values[name] = value
    return values


def _db(request: Request):
    db = request.app.state.db
    return (db, None) if db is not None else (None, problem(503, "The database is not available."))


def _with_cookie(response: JSONResponse, token: str | None) -> JSONResponse:
    if token is None:
        response.set_cookie(COOKIE, "", max_age=0, httponly=True, samesite="strict", path="/")
    else:
        response.set_cookie(
            COOKIE,
            token,
            max_age=accounts.MAX_DAYS * 86400,
            httponly=True,
            samesite="strict",
            path="/",
        )
    response.headers["Cache-Control"] = "no-store"
    return response


async def login(request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    body = await read_body(request, {"username": 200, "password": passwords.MAX_LENGTH + 100})
    if isinstance(body, JSONResponse):
        return body
    limiter: LoginLimiter = request.app.state.limiter
    address = client_address(request.scope)
    keys = limiter.keys(body["username"], address)
    wait = limiter.wait(*keys)
    if wait > 0:
        logger.warning("login refused: too many tries", extra={"client": address})
        seconds = int(wait) + 1
        answer = JSONResponse(
            {"detail": "too many tries, wait a moment", "retry_after": seconds}, status_code=429
        )
        answer.headers["Retry-After"] = str(seconds)
        return answer
    found = await asyncio.to_thread(accounts.find, db, body["username"])
    stored = found[1] if found else None
    # The hash is always computed (against a dummy for an unknown name): same time either way.
    ok = await asyncio.to_thread(passwords.verify_password, body["password"], stored)
    if not ok or found is None or found[0].disabled:
        limiter.failed(*keys)
        logger.warning("login failed", extra={"client": address})
        return problem(401, INVALID_LOGIN)
    account = found[0]
    limiter.succeeded(keys[0])
    old = request.state.session_token
    if old:
        await asyncio.to_thread(accounts.end_session, db, old)
    if stored and passwords.needs_rehash(stored):
        await asyncio.to_thread(accounts.set_password, db, account.id, body["password"])
    token = await asyncio.to_thread(accounts.new_session, db, account.id)
    await asyncio.to_thread(accounts.record_login, db, account.id)
    logger.info("login", extra={"account_id": account.id, "client": address})
    return _with_cookie(JSONResponse({"account": account.as_dict()}), token)


async def logout(request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    token = request.state.session_token
    if token:
        await asyncio.to_thread(accounts.end_session, db, token)
    return _with_cookie(JSONResponse({"ok": True}), None)


async def me(request: Request) -> JSONResponse:
    account: Account | None = request.state.account
    if account is None:
        body: dict[str, Any] = {"detail": "login required"}
        db = request.app.state.db
        if db is not None and not await asyncio.to_thread(accounts.list_all, db):
            body["setup"] = True  # no account exists: the page says how to make the first admin
        return JSONResponse(body, status_code=401, headers={"Cache-Control": "no-store"})
    return JSONResponse({"account": account.as_dict()}, headers={"Cache-Control": "no-store"})


async def change_password(request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    body = await read_body(
        request, {"current": passwords.MAX_LENGTH + 100, "new": passwords.MAX_LENGTH + 100}
    )
    if isinstance(body, JSONResponse):
        return body
    account: Account = request.state.account
    limiter: LoginLimiter = request.app.state.limiter
    key = limiter.keys(account.username, client_address(request.scope))[0]
    if limiter.wait(key) > 0:
        return problem(429, "too many tries, wait a moment")
    found = await asyncio.to_thread(accounts.find, db, account.username)
    stored = found[1] if found else None
    if not await asyncio.to_thread(passwords.verify_password, body["current"], stored):
        limiter.failed(key)
        return problem(403, "the current password is wrong")
    token = request.state.session_token
    try:
        await asyncio.to_thread(
            accounts.set_password,
            db,
            account.id,
            body["new"],
            keep_session=accounts.token_hash(token) if token else None,
        )
    except AccountError as error:
        return problem(STATUS.get(error.kind, 422), error.message)
    limiter.succeeded(key)
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------------------- the admin


def _error(error: AccountError) -> JSONResponse:
    body: dict[str, Any] = {"detail": error.message, "kind": error.kind}
    return JSONResponse(body, status_code=STATUS.get(error.kind, 422))


async def list_accounts(request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    rows = await asyncio.to_thread(accounts.list_all, db)
    return JSONResponse({"accounts": [a.as_dict() for a in rows]})


async def create_account(request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    body = await read_body(request, {"username": 200, "password": passwords.MAX_LENGTH + 100})
    if isinstance(body, JSONResponse):
        return body
    role = (await request.json()).get("role", "user")
    try:
        account = await asyncio.to_thread(
            accounts.create, db, body["username"], body["password"], role
        )
    except AccountError as error:
        return _error(error)
    logger.info("account created", extra={"account_id": account.id, "by": request.state.account.id})
    return JSONResponse({"account": account.as_dict()}, status_code=201)


async def update_account(account_id: int, request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    try:
        data = await request.json()
    except ValueError:
        return problem(422, "the request is not JSON")
    if not isinstance(data, dict) or not data or set(data) - {"role", "disabled"}:
        return problem(422, "send role and/or disabled")
    try:
        result: Account | None = None
        if "role" in data:
            if not isinstance(data["role"], str):
                return problem(422, "role must be admin or user")
            result = await asyncio.to_thread(accounts.set_role, db, account_id, data["role"])
        if "disabled" in data:
            if not isinstance(data["disabled"], bool):
                return problem(422, "disabled must be true or false")
            result = await asyncio.to_thread(
                accounts.set_disabled, db, account_id, data["disabled"]
            )
    except AccountError as error:
        return _error(error)
    assert result is not None
    return JSONResponse({"account": result.as_dict()})


async def reset_password(account_id: int, request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    body = await read_body(request, {"password": passwords.MAX_LENGTH + 100})
    if isinstance(body, JSONResponse):
        return body
    try:
        await asyncio.to_thread(accounts.set_password, db, account_id, body["password"])
    except AccountError as error:
        return _error(error)
    return JSONResponse({"ok": True})


async def delete_account(account_id: int, request: Request) -> JSONResponse:
    db, failure = _db(request)
    if failure is not None:
        return failure
    try:
        await asyncio.to_thread(accounts.delete, db, account_id)
    except AccountError as error:
        return _error(error)
    return JSONResponse({"ok": True})


def register(app: FastAPI) -> None:
    app.add_api_route("/api/auth/login", login, methods=["POST"])
    app.add_api_route("/api/auth/logout", logout, methods=["POST"])
    app.add_api_route("/api/auth/me", me, methods=["GET"])
    app.add_api_route("/api/auth/password", change_password, methods=["POST"])
    app.add_api_route("/api/accounts", list_accounts, methods=["GET"])
    app.add_api_route("/api/accounts", create_account, methods=["POST"])
    app.add_api_route("/api/accounts/{account_id}", update_account, methods=["PATCH"])
    app.add_api_route("/api/accounts/{account_id}/password", reset_password, methods=["POST"])
    app.add_api_route("/api/accounts/{account_id}", delete_account, methods=["DELETE"])
