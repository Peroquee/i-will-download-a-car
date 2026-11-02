"""Login, who may do what, and the brake on guessing (issue #44).

* **The login cookie** `ytp_session` holds 32 random bytes (URL-safe). It is `HttpOnly` (scripts
  cannot read it) and `SameSite=Strict` (a request from another site never carries it). It is not
  `Secure`: the app is plain HTTP on the LAN, a `Secure` cookie would never be sent back. The
  server stores only the SHA-256 of it (`ytp.accounts`).
* **Every request is classified before a handler runs** (`AuthMiddleware`): `POLICY` maps (method,
  route) to `public`, `user` or `admin`. A route with no entry is `admin`-only (a new route that
  nobody classified is closed, not open), a path that matches no route needs a login like a
  `user` route (so the answer does not tell a stranger which paths exist), and
  `tests/unit/test_auth_policy.py` fails if a route of the app has no entry.
* **No login at all means 401** (`{"detail": ...}`), a user on an admin route 403. The handler
  receives the account as `request.state.account`.
* **CSRF** is the existing same-origin check (`ytp.web.SecurityMiddleware`, outermost) plus
  `SameSite=Strict`.
* **Guessing is slowed down** (`LoginLimiter`): after 5 failures for a name or 30 for a client
  address within 15 minutes, further tries are refused for a time that doubles with every
  failure (at most 15 minutes), a right password included. The proxy has no rate limit.
* `TEST_ACCOUNT` is a hook for the tests only: nothing in the app sets it. When a test sets it,
  every request counts as that account. A test of the real thing sets it back to None.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
from collections.abc import Callable, MutableMapping
from http.cookies import SimpleCookie
from typing import Any

from starlette.routing import Match

from . import accounts
from .accounts import Account
from .db import Database

logger = logging.getLogger(__name__)

COOKIE = "ytp_session"
PUBLIC, USER, ADMIN = "public", "user", "admin"
TEST_ACCOUNT: Account | None = None

# (method, route as registered) -> who may call it. HEAD follows GET.
POLICY: dict[tuple[str, str], str] = {
    # the page, its files and the questions a page without a login must be able to ask
    ("GET", "/"): PUBLIC,
    ("GET", "/dashboard"): PUBLIC,
    ("GET", "/accounts"): PUBLIC,  # the page only; the data behind it is /api/accounts (admin)
    ("GET", "/health"): PUBLIC,
    ("GET", "/api/version"): PUBLIC,
    ("POST", "/reload-needed"): PUBLIC,
    ("*", "/static"): PUBLIC,
    ("POST", "/api/auth/login"): PUBLIC,
    ("GET", "/api/auth/me"): PUBLIC,  # answers 401 itself: "who am I?" is a question for anyone
    # any logged-in account
    ("POST", "/api/auth/logout"): USER,
    ("POST", "/api/auth/password"): USER,
    ("GET", "/api/vpn"): USER,
    ("GET", "/api/dashboard"): USER,
    ("POST", "/api/magnet"): USER,
    ("POST", "/api/youtube/probe"): USER,
    ("POST", "/api/youtube/download"): USER,
    ("GET", "/api/jobs"): USER,
    ("POST", "/api/jobs/{job_id}/retry"): USER,
    ("GET", "/api/subscriptions"): USER,
    ("POST", "/api/subscriptions"): USER,
    ("DELETE", "/api/subscriptions/{subscription_id}"): USER,
    ("POST", "/api/subscriptions/{subscription_id}/poll"): USER,
    ("POST", "/api/podcasts/probe"): USER,
    ("POST", "/api/podcasts/episode"): USER,
    ("GET", "/api/podcasts"): USER,
    ("POST", "/api/podcasts"): USER,
    ("DELETE", "/api/podcasts/{subscription_id}"): USER,
    ("POST", "/api/podcasts/{subscription_id}/poll"): USER,
    # admins only
    ("GET", "/api/alerts"): ADMIN,
    ("GET", "/api/alerts/{alert_id}"): ADMIN,
    ("POST", "/api/alerts/ack-all"): ADMIN,
    ("POST", "/api/alerts/{alert_id}/ack"): ADMIN,
    ("GET", "/api/accounts"): ADMIN,
    ("POST", "/api/accounts"): ADMIN,
    ("PATCH", "/api/accounts/{account_id}"): ADMIN,
    ("POST", "/api/accounts/{account_id}/password"): ADMIN,
    ("DELETE", "/api/accounts/{account_id}"): ADMIN,
}


def classify(routes: list[Any], scope: MutableMapping[str, Any]) -> str:
    """The policy for this request: the route's entry, `admin` if it has none, `user` if no route
    matches at all (so strangers learn nothing about which paths exist)."""
    method = "GET" if scope["method"] == "HEAD" else scope["method"]
    partial: str | None = None
    for route in routes:
        match, _ = route.matches(scope)
        if match == Match.NONE:
            continue
        template = route.path
        level = POLICY.get((method, template)) or POLICY.get(("*", template))
        if match == Match.FULL:
            return level or ADMIN
        # the path matches but not the method: the least a caller needs is a login
        known = [lvl for (m, t), lvl in POLICY.items() if t == template]
        partial = partial or (min(known, key=_rank) if known else ADMIN)
    return partial or USER


def _rank(level: str) -> int:
    return {PUBLIC: 0, USER: 1, ADMIN: 2}[level]


def session_cookie(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:  # a malformed Cookie header is just no cookie
                return None
            morsel = jar.get(COOKIE)
            return morsel.value if morsel else None
    return None


def client_address(scope: MutableMapping[str, Any]) -> str:
    """The client's address for the rate limit. Caddy puts the real one in X-Forwarded-For (it
    replaces what a client sent); it is only believed when the connection comes from a private or
    loopback address, i.e. from the proxy on the compose network."""
    peer = (scope.get("client") or ("", 0))[0] or "unknown"
    try:
        private = ipaddress.ip_address(peer).is_private
    except ValueError:
        private = False
    if private:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                last = value.decode("latin-1").split(",")[-1].strip()
                try:
                    return str(ipaddress.ip_address(last))
                except ValueError:
                    break
    return peer


class LoginLimiter:
    """Failures per key within a window; too many lock the key for a doubling time."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window: float = 900.0,
        max_user: int = 5,
        max_address: int = 30,
        base_lock: float = 30.0,
        max_lock: float = 900.0,
        max_keys: int = 10_000,
    ):
        self._clock = clock
        self.window = window
        self.limits = {"user": max_user, "address": max_address}
        self.base_lock = base_lock
        self.max_lock = max_lock
        self.max_keys = max_keys
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def keys(username: str, address: str) -> tuple[str, str]:
        return f"user:{username.strip().lower()[:64]}", f"address:{address}"

    def _limit(self, key: str) -> int:
        return self.limits[key.split(":", 1)[0]]

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def wait(self, *keys: str) -> float:
        """Seconds until a try is allowed again (0 = go ahead)."""
        now = self._clock()
        longest = 0.0
        with self._lock:
            for key in keys:
                recent = self._recent(key, now)
                over = len(recent) - self._limit(key)
                if over >= 0:
                    lock = min(self.max_lock, self.base_lock * (2**over))
                    longest = max(longest, recent[-1] + lock - now)
        return max(0.0, longest)

    def failed(self, *keys: str) -> None:
        now = self._clock()
        with self._lock:
            if (
                len(self._failures) > self.max_keys
            ):  # a flood of made-up names must not grow forever
                self._failures = {
                    k: v for k, v in self._failures.items() if now - v[-1] < self.window
                }
                if len(self._failures) > self.max_keys:
                    self._failures.clear()
            for key in keys:
                self._failures.setdefault(key, []).append(now)

    def succeeded(self, user_key: str) -> None:
        with self._lock:
            self._failures.pop(user_key, None)


def _json_response(status: int, detail: str) -> tuple[list[dict[str, Any]], int]:
    body = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    return [
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    ], status


class AuthMiddleware:
    """Resolves the session cookie to an account and refuses what the policy does not allow."""

    def __init__(
        self, app: Any, *, routes: Callable[[], list[Any]], database: Callable[[], Database | None]
    ):
        self.app = app
        self._routes = routes
        self._database = database

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        level = classify(self._routes(), scope)
        account: Account | None = TEST_ACCOUNT
        token = session_cookie(scope.get("headers", []))
        # Public routes do not need to know who asks (static files, the page); "who am I?" does.
        needs_account = level != PUBLIC or scope["path"] == "/api/auth/me"
        if account is None and token and needs_account:
            db = self._database()
            if db is not None:
                try:
                    account = accounts.session_account(db, token)
                except Exception:
                    logger.exception("could not look up the session")
                    account = None
        state = scope.setdefault("state", {})
        state["account"] = account
        state["session_token"] = token
        if level != PUBLIC:
            if account is None:
                await self._refuse(send, 401, "login required")
                return
            if level == ADMIN and not account.is_admin:
                await self._refuse(send, 403, "this needs an admin account")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(send: Any, status: int, detail: str) -> None:
        messages, _ = _json_response(status, detail)
        for message in messages:
            await send(message)
