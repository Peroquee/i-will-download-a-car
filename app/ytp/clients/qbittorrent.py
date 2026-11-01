"""qBittorrent Web API client (issue #10).

Standard library only (`http.client`): the app needs five calls, the client is small enough
to read in full, and no dependency has to be pinned into the image. It is synchronous; async
code calls it through `asyncio.to_thread(...)`. One instance is thread-safe.

What this wrapper absorbs, each learned on the real VM:

* **Login.** Current qBittorrent answers a good login with 204 and an empty body, a bad one
  with 401; older versions answer 200 "Ok." / 200 "Fails.". Both are accepted. The session is
  the session cookie, kept here by hand (never logged): `SID` up to qBittorrent 5.1, and
  `QBT_SID_<webui port>` since 5.2 (any of these names is accepted and sent back unchanged).
* **Expired session.** An API call without a valid session cookie gets 403. The client logs in
  again ONCE and repeats the call ONCE; a second 403 is an error, never a loop.
* **Banned address.** Five failed logins ban the source address for an hour (403 on login).
  A refused login is therefore never retried automatically: the client remembers the refusal
  and raises `QbitAuthError` without touching qBittorrent (wrong credentials: until the app is
  restarted with the right ones; a ban: for an hour).
* **Host and CSRF checks.** qBittorrent answers 401 when the Host header's port is not its own
  Web UI port and compares Origin/Referer with Host. The client talks to the URL it is given
  (`http://gluetun:8080`: Host and port match) and sends that URL as Origin and Referer.
* **Unreachable.** qBittorrent lives behind the VPN and is gone whenever the tunnel is down or
  restarting. Every network problem (refused, reset, DNS, timeout, a 5xx, a garbled answer)
  becomes `QbitUnavailable`; no `OSError` escapes. Callers degrade and try again later.
* **qBittorrent 5.** `torrents/pause` and `resume` became `stop` and `start` (and the state
  filters `paused`/`resumed` became `stopped`/`running`). Both generations are supported.

Credentials come from QBIT_USER / QBIT_PASSWORD (see `Settings`). The password and the session
cookie are never written to a log line or an exception message.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..magnet import infohash_from_magnet

logger = logging.getLogger(__name__)

# The categories scripts/qbittorrent/setup_categories.py creates; each has a save path under
# /data/downloads. A torrent without one would land in the default folder, so none is refused.
CATEGORIES = ("movies", "series", "audiobooks", "music")

# `filter` values of torrents/info. paused/resumed are the 4.x names of stopped/running (5.x).
STATE_FILTERS = frozenset(
    {
        "all",
        "downloading",
        "seeding",
        "completed",
        "paused",
        "stopped",
        "active",
        "inactive",
        "resumed",
        "running",
        "stalled",
        "stalled_uploading",
        "stalled_downloading",
        "checking",
        "moving",
        "errored",
    }
)
_STATE_ALIASES = {
    "paused": "stopped",
    "stopped": "paused",
    "resumed": "running",
    "running": "resumed",
}

BAN_SECONDS = 3600.0  # qBittorrent's default ban time after too many failed logins


class QbitError(Exception):
    """Base class: everything this client reports is a QbitError (or a ValueError for bad input)."""


class QbitUnavailable(QbitError):
    """qBittorrent cannot be reached or did not answer properly. Expected while the VPN is down."""


class QbitAuthError(QbitError):
    """No usable login: credentials missing or wrong, address banned, or the session is refused."""


class QbitRequestError(QbitError):
    """qBittorrent answered but refused the request (a rejected magnet, an unknown endpoint)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Torrent:
    """The fields of torrents/info the app uses; everything else is ignored."""

    hash: str
    name: str
    state: str
    progress: float
    category: str = ""
    save_path: str = ""
    content_path: str = ""
    size: int = 0
    amount_left: int = 0
    added_on: int = 0
    completion_on: int = 0

    @property
    def finished(self) -> bool:
        return self.progress >= 1.0

    @classmethod
    def from_api(cls, item: Mapping[str, Any]) -> Torrent:
        try:
            return cls(
                hash=str(item["hash"]).lower(),
                name=str(item.get("name", "")),
                state=str(item.get("state", "")),
                progress=float(item.get("progress", 0.0)),
                category=str(item.get("category", "")),
                save_path=str(item.get("save_path", "")),
                content_path=str(item.get("content_path", "")),
                size=int(item.get("size", 0)),
                amount_left=int(item.get("amount_left", 0)),
                added_on=int(item.get("added_on", 0)),
                completion_on=int(item.get("completion_on", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QbitUnavailable(
                f"qBittorrent returned an unexpected torrent entry: {exc!r}"
            ) from exc


class QbitClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path.strip("/"):
            raise ValueError(f"base_url must be http(s)://host[:port] without a path: {base_url!r}")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        # What a browser would send as Host/Origin: the port only when it is not the default.
        default_port = 443 if parts.scheme == "https" else 80
        netloc = (
            f"{parts.hostname}:{parts.port}"
            if parts.port not in (None, default_port)
            else parts.hostname
        )
        self.base_url = f"{parts.scheme}://{netloc}"
        self._username = username
        self._password = password
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._cookie: str | None = None  # "NAME=value", sent as the Cookie header
        self._login_blocked_until = 0.0
        self._login_block_reason = ""

    def __repr__(self) -> str:
        return f"QbitClient({self.base_url!r}, user={self._username!r})"

    @classmethod
    def from_settings(cls, settings: Any) -> QbitClient:
        return cls(settings.qbit_url, settings.qbit_user, settings.qbit_password)

    # ------------------------------------------------------------------ transport

    def _send(
        self, method: str, path: str, *, query: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None, cookie: str | None = None,
    ) -> tuple[int, str, http.client.HTTPMessage]:  # fmt: skip
        """One HTTP round trip on a fresh connection. Network trouble becomes QbitUnavailable."""
        target = f"/api/v2/{path}" + (f"?{urlencode(query)}" if query else "")
        headers = {
            "Host": self._host_header(),
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "Accept": "*/*",
            "User-Agent": "youtube-premium",
        }
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = cookie
        conn_class = (
            http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_class(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(method, target, body=body, headers=headers)
            response = conn.getresponse()
            text = response.read().decode("utf-8", errors="replace")
            return response.status, text, response.msg
        except (OSError, http.client.HTTPException) as exc:
            # OSError covers refused, reset, DNS failure and timeouts (socket.timeout is one).
            raise QbitUnavailable(f"cannot reach qBittorrent at {self.base_url}: {exc}") from exc
        finally:
            conn.close()

    def _host_header(self) -> str:
        default_port = 443 if self._scheme == "https" else 80
        return self._host if self._port == default_port else f"{self._host}:{self._port}"

    # ------------------------------------------------------------------ session

    def _block_login(self, seconds: float, reason: str) -> None:
        self._login_blocked_until = self._clock() + seconds
        self._login_block_reason = reason

    def reset_login_block(self) -> None:
        """Allow a login attempt again (after the credentials were fixed)."""
        with self._lock:
            self._login_blocked_until = 0.0
            self._login_block_reason = ""

    def login(self) -> None:
        """Log in and keep the session cookie. Raises QbitAuthError / QbitUnavailable."""
        with self._lock:
            self._login_locked()

    def _login_locked(self) -> None:
        if not self._password:
            raise QbitAuthError("QBIT_PASSWORD is not set: the qBittorrent login is unknown")
        if self._clock() < self._login_blocked_until:
            raise QbitAuthError(self._login_block_reason)
        status, body, headers = self._send(
            "POST", "auth/login", form={"username": self._username, "password": self._password}
        )
        if status == 403:
            reason = (
                "qBittorrent refused the login with 403: this address is banned after too many "
                "failed logins (an hour). Not retrying."
            )
            self._block_login(BAN_SECONDS, reason)
            raise QbitAuthError(reason)
        if status == 401 or (status == 200 and body.strip() == "Fails."):
            reason = (
                "qBittorrent refused the login: wrong user or password in QBIT_USER / "
                "QBIT_PASSWORD (it also answers 401 when the port in the URL is not its own Web UI "
                "port). Not retrying: five failures ban the address for an hour. "
                "Fix .env and restart the app."
            )
            self._block_login(float("inf"), reason)
            raise QbitAuthError(reason)
        if status >= 500:
            raise QbitUnavailable(f"qBittorrent answered {status} to the login")
        if not (status == 204 or (status == 200 and body.strip() in {"Ok.", ""})):
            raise QbitUnavailable(f"unexpected answer to the login: HTTP {status}")
        cookie = _session_cookie_from(headers)
        if not cookie:
            raise QbitUnavailable("qBittorrent accepted the login but sent no session cookie")
        self._cookie = cookie
        logger.info("qbittorrent login ok", extra={"url": self.base_url})

    # ------------------------------------------------------------------ requests

    def _request(
        self, method: str, path: str, *, query: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:  # fmt: skip
        """An authenticated call: logs in if needed, re-logs in ONCE on 403 and repeats ONCE."""
        with self._lock:
            if self._cookie is None:
                self._login_locked()
            for attempt in (1, 2):
                status, body, _ = self._send(
                    method, path, query=query, form=form, cookie=self._cookie
                )
                if status != 403:
                    break
                if attempt == 2:
                    raise QbitAuthError("qBittorrent rejects the session even after a new login")
                logger.warning("qbittorrent session expired, logging in again")
                self._cookie = None
                self._login_locked()
            if status == 401:
                raise QbitAuthError(
                    "qBittorrent answered 401: the Host/port or Origin does not match its Web UI "
                    f"(is QBIT_URL {self.base_url} its own address and port?)"
                )
            if status >= 500:
                raise QbitUnavailable(f"qBittorrent answered {status} for {path}")
            return status, body

    # ------------------------------------------------------------------ the API

    def version(self) -> str:
        """qBittorrent's version, e.g. "v5.0.4"; a cheap proof that login and network work."""
        status, body = self._request("GET", "app/version")
        if status != 200:
            raise QbitRequestError(f"app/version answered {status}", status)
        return body.strip()

    def add_magnet(self, magnet: str, category: str) -> str:
        """Queue a magnet link in `category`; returns its infohash (lower-case hex).

        Raises InvalidMagnet / ValueError for input that is not acceptable (nothing is sent),
        QbitRequestError if qBittorrent refuses it. The link travels as a form field value and
        is never put into a URL or a command.
        """
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}, got {category!r}")
        infohash = infohash_from_magnet(magnet)
        status, body = self._request(
            "POST",
            "torrents/add",
            # autoTMM makes the category's save path apply, whatever the global default is.
            form={"urls": magnet, "category": category, "autoTMM": "true"},
        )
        if status != 200 or body.strip() == "Fails.":
            raise QbitRequestError(
                f"qBittorrent did not accept torrent {infohash} (HTTP {status}: {body.strip()!r})",
                status,
            )
        return infohash

    def list_torrents(
        self, state: str | None = None, category: str | None = None, hashes: list[str] | None = None
    ) -> list[Torrent]:
        """Torrents, optionally filtered by state ("downloading", ...), category or hashes."""
        if state is not None and state not in STATE_FILTERS:
            raise ValueError(f"unknown state filter {state!r}")
        query: dict[str, str] = {}
        if category is not None:
            query["category"] = category
        if hashes:
            query["hashes"] = "|".join(h.lower() for h in hashes)
        if state is None or state == "all":
            status, body = self._request("GET", "torrents/info", query=query)
        else:
            status, body = self._request("GET", "torrents/info", query={**query, "filter": state})
            if status == 400 and state in _STATE_ALIASES:
                # qBittorrent 4 vs 5 name these states differently.
                status, body = self._request(
                    "GET", "torrents/info", query={**query, "filter": _STATE_ALIASES[state]}
                )
        if status != 200:
            raise QbitRequestError(f"torrents/info answered {status}", status)
        try:
            items = json.loads(body)
        except ValueError as exc:
            raise QbitUnavailable("qBittorrent returned a list that is not JSON") from exc
        if not isinstance(items, list):
            raise QbitUnavailable("qBittorrent returned an unexpected torrent list")
        return [Torrent.from_api(item) for item in items if isinstance(item, dict)]

    def pause_all(self) -> None:
        """Pause every torrent: `torrents/stop` (5.x), falling back to `torrents/pause` (4.x)."""
        self._all_torrents("stop", "pause")

    def resume_all(self) -> None:
        """Resume every torrent: `torrents/start` (5.x), falling back to `torrents/resume` (4.x)."""
        self._all_torrents("start", "resume")

    def _all_torrents(self, new_name: str, old_name: str) -> None:
        status, _ = self._request("POST", f"torrents/{new_name}", form={"hashes": "all"})
        if status == 404:
            status, _ = self._request("POST", f"torrents/{old_name}", form={"hashes": "all"})
        if status != 200:
            raise QbitRequestError(f"torrents/{new_name} answered {status}", status)


def _session_cookie_from(headers: http.client.HTTPMessage) -> str | None:
    """ "NAME=value" of the session cookie in a login answer: `SID` (qBittorrent up to 5.1) or
    `QBT_SID_<port>` (5.2 and later). Other cookies are ignored."""
    for raw in headers.get_all("Set-Cookie") or []:
        try:
            cookie = SimpleCookie(raw)
        except CookieError:
            continue
        for name, morsel in cookie.items():
            if (name == "SID" or name.startswith("QBT_SID_")) and morsel.value:
                return f"{name}={morsel.value}"
    return None
