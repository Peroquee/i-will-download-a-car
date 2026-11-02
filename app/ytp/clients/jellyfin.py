"""Jellyfin API client and the debounced library refresh (issue #11).

Jellyfin is external (docs/jellyfin.md): its own LXC, reached at JELLYFIN_URL with an API key.
The app only ever asks it to look for new files; nothing about a download depends on the
answer, so everything here **fails soft**: `LibraryRefresher.request_refresh` never raises, a
Jellyfin that is down or refuses the key is logged and retried a few times, and with no URL or
key configured it does nothing at all.

* `JellyfinClient` (standard library, synchronous, like the qBittorrent client): `ping`,
  `list_libraries`, `refresh_library` (scan everything). The key goes in the `Authorization`
  header only, never in a URL, a log line or an error message.
* `LibraryRefresher` adds the debounce. A batch of downloads finishing together (a season
  pack, ten files) calls `request_refresh` ten times; Jellyfin gets ONE `POST /Library/Refresh`,
  sent after things have been quiet for `quiet_seconds` (but never later than
  `max_wait_seconds` after the first request, so a steady trickle cannot postpone it for ever).
  The scheduler calls `flush_due()` every few seconds.

**Why always the full scan.** The cheaper per-library call, `POST /Items/{libraryId}/Refresh`,
only re-reads the items Jellyfin already knows: on a real Jellyfin 10.11.11 a library stayed at 0
items 75 s after it, with 21 new files on disk, while `POST /Library/Refresh` found all 21 within
two minutes. Only the library scan discovers NEW files, so that is what is sent. It is
incremental (Jellyfin skips what has not changed), so for a home library it costs little. The
alternative, `POST /Library/Media/Updated` with the new file paths, would need the paths under
Jellyfin's prefix (`/media/jellyfin/...` there, `/data/...` here) and a change in every caller;
it is the way to go if a very large library ever makes the full scan too slow.

The folders named in `request_refresh` are only logged: the app and Jellyfin see the same
folders under different prefixes and no path is ever translated.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class JellyfinError(Exception):
    """Base class: everything this client reports."""


class JellyfinUnavailable(JellyfinError):
    """Cannot reach Jellyfin, it did not answer in time, or answered 5xx."""


class JellyfinAuthError(JellyfinError):
    """No usable API key (missing, revoked or wrong): Jellyfin answered 401 or 403."""


class JellyfinRequestError(JellyfinError):
    """Jellyfin answered but refused the request (an unknown item, a bad answer)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Library:
    name: str
    item_id: str
    locations: tuple[str, ...]
    collection_type: str = ""

    def has_folder(self, folder: str) -> bool:
        """True if one of the library's locations ends in the folder `folder`."""
        wanted = unicodedata.normalize("NFC", folder).casefold()
        for location in self.locations:
            last = unicodedata.normalize("NFC", location.replace("\\", "/").rstrip("/")).rsplit(
                "/", 1
            )[-1]
            if last.casefold() == wanted:
                return True
        return False


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 10.0):
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path.strip("/"):
            raise ValueError(f"base_url must be http(s)://host[:port] without a path: {base_url!r}")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self.base_url = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
        self._api_key = api_key
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"JellyfinClient({self.base_url!r})"

    @classmethod
    def from_settings(cls, settings: Any) -> JellyfinClient:
        return cls(settings.jellyfin_url, settings.jellyfin_api_key)

    # ------------------------------------------------------------------ transport

    def _request(
        self, method: str, path: str, *, query: Mapping[str, str] | None = None, auth: bool = True
    ) -> tuple[int, str]:
        if auth and not self._api_key:
            raise JellyfinAuthError("JELLYFIN_API_KEY is not set")
        target = path + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")
        headers = {"Accept": "application/json", "User-Agent": "youtube-premium"}
        if auth:
            # MediaBrowser scheme: the documented way to send an API key without a URL parameter.
            headers["Authorization"] = f'MediaBrowser Token="{self._api_key}"'
        conn_class = (
            http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_class(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(method, target, headers=headers)
            response = conn.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise JellyfinUnavailable(f"cannot reach Jellyfin at {self.base_url}: {exc}") from exc
        finally:
            conn.close()
        if status in (401, 403):
            raise JellyfinAuthError(
                f"Jellyfin refused the API key (HTTP {status}): was it revoked? "
                "Create a new one in Dashboard > API Keys and put it into .env"
            )
        if status >= 500:
            raise JellyfinUnavailable(f"Jellyfin answered {status} for {path}")
        return status, body

    # ------------------------------------------------------------------ the API

    def ping(self) -> str:
        """Jellyfin's version, from the public info endpoint (no key needed): is it up at all?"""
        status, body = self._request("GET", "/System/Info/Public", auth=False)
        if status != 200:
            raise JellyfinRequestError(f"System/Info/Public answered {status}", status)
        try:
            return str(json.loads(body).get("Version", ""))
        except (ValueError, AttributeError) as exc:
            raise JellyfinUnavailable("Jellyfin returned an unreadable answer") from exc

    def list_libraries(self) -> list[Library]:
        status, body = self._request("GET", "/Library/VirtualFolders")
        if status != 200:
            raise JellyfinRequestError(f"Library/VirtualFolders answered {status}", status)
        try:
            items = json.loads(body)
            return [
                Library(
                    name=str(item["Name"]),
                    item_id=str(item["ItemId"]),
                    locations=tuple(str(x) for x in item.get("Locations", [])),
                    collection_type=str(item.get("CollectionType", "")),
                )
                for item in items
            ]
        except (ValueError, TypeError, KeyError) as exc:
            raise JellyfinUnavailable("Jellyfin returned an unexpected library list") from exc

    def refresh_library(self) -> None:
        """Scan ALL libraries for new and changed files. This is the call that finds NEW files."""
        status, _ = self._request("POST", "/Library/Refresh")
        if status not in (200, 204):
            raise JellyfinRequestError(f"Library/Refresh answered {status}", status)


@dataclass
class _Pending:
    folders: set[str] = field(default_factory=set)  # only for the log
    first_at: float = 0.0
    last_at: float = 0.0
    failures: int = 0
    retry_at: float = 0.0


class LibraryRefresher:
    """Collects refresh requests and sends one refresh per library after a quiet period."""

    def __init__(
        self,
        client: JellyfinClient | None,
        *,
        quiet_seconds: float = 30.0,
        max_wait_seconds: float = 300.0,
        retry_seconds: float = 60.0,
        max_failures: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.quiet_seconds = quiet_seconds
        self.max_wait_seconds = max_wait_seconds
        self.retry_seconds = retry_seconds
        self.max_failures = max_failures
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: _Pending | None = None
        self.refreshes_sent = 0  # for tests and diagnostics

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    def request_refresh(self, folder: str | None = None) -> None:
        """Note that new files arrived in the library folder `folder` (None: somewhere).

        Never raises and never waits for Jellyfin: safe to call from a download job.
        """
        if self.client is None:
            return
        now = self._clock()
        with self._lock:
            pending = self._pending or _Pending(first_at=now)
            pending.last_at = now
            if folder:
                pending.folders.add(unicodedata.normalize("NFC", folder))
            self._pending = pending

    def flush_due(self) -> bool:
        """Send the pending refresh if it is due. Called by the scheduler every few seconds.

        Due = quiet for `quiet_seconds`, or `max_wait_seconds` since the first request, and not
        waiting out a retry delay. Returns True if a refresh went out.
        """
        client = self.client
        if client is None:
            return False
        now = self._clock()
        with self._lock:
            pending = self._pending
            if pending is None or now < pending.retry_at:
                return False
            quiet = now - pending.last_at >= self.quiet_seconds
            overdue = now - pending.first_at >= self.max_wait_seconds
            if not (quiet or overdue):
                return False
            # Work on a snapshot; requests arriving meanwhile start a new pending batch.
            self._pending = None
        try:
            self._send(client, pending)
        except JellyfinAuthError as exc:
            # A missing or revoked key does not fix itself in a minute: say so once, drop the batch.
            logger.error("jellyfin: library refresh dropped", extra={"error": str(exc)})
            return False
        except JellyfinError as exc:
            self._retry_later(pending, exc)
            return False
        self.refreshes_sent += 1
        return True

    def _send(self, client: JellyfinClient, pending: _Pending) -> None:
        client.refresh_library()
        logger.info(
            "jellyfin: library refresh requested", extra={"folders": sorted(pending.folders)}
        )

    def _retry_later(self, pending: _Pending, exc: JellyfinError) -> None:
        pending.failures += 1
        if pending.failures >= self.max_failures:
            logger.error(
                "jellyfin: giving up on a library refresh",
                extra={"error": str(exc), "attempts": pending.failures},
            )
            return
        logger.warning(
            "jellyfin: library refresh failed, will retry",
            extra={"error": str(exc), "attempt": pending.failures},
        )
        pending.retry_at = self._clock() + self.retry_seconds
        with self._lock:
            current = self._pending
            if current is None:
                self._pending = pending
            else:  # new requests came in meanwhile: merge, keep the failure count
                current.folders |= pending.folders
                current.first_at = min(current.first_at, pending.first_at)
                current.failures = pending.failures
                current.retry_at = pending.retry_at
