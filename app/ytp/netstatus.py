"""The dashboard's two independent system signals (issue #63).

`egress.py` answers "does traffic leave through the expected VPN", and the watchdog's `state`
is the VPN's condition. Neither answers "can this host reach the internet at all" or "is the
watchdog's last poll fresh". This module does both, without changing the watchdog, its client
or the egress check:

* `InternetChecker` does a small DNS + TCP check on the app's normal egress path. It is cached
  for a short time because the page polls the status endpoint every few seconds.
* `watchdog_view` reduces the watchdog's answer to whether it is answering and its poll is
  fresh. It never raises: anything unreadable is `unreachable`, never `ok`.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .feeds import parse_time

LOGGER = logging.getLogger(__name__)

# A host the app contacts anyway (yt-dlp, the feeds); INTERNET_CHECK_HOST / INTERNET_CHECK_PORT
# change it, an empty host switches the check off.
DEFAULT_HOST = "www.youtube.com"
DEFAULT_PORT = 443
DEFAULT_TIMEOUT = 3.0
DEFAULT_INTERVAL = 30.0

# The watchdog polls every WATCHDOG_INTERVAL (1 s by default, at most 60 s). Five minutes is
# unambiguously old for every accepted setting, so it cannot make a healthy watchdog look stale.
WATCHDOG_STALE_SECONDS = 300.0


class InternetChecker:
    """A cached DNS + TCP reachability check that never raises into an API answer.

    The check happens on the app's normal egress path (a direct socket, not the VPN proxy):
    it answers "the host can reach the internet", not "the VPN is working". A successful
    connection is `ok`; a refused or timed-out one is `offline`; before the first check, or
    after an unexpected error, it is `unknown`. With an EMPTY host the check is switched off: it
    never opens a connection and always answers `unknown` with `reason: "disabled"` (never `ok`).
    The answer carries a `reason` only when it is `unknown` for a known cause (`disabled`,
    `error`). Only the host name is ever logged, never the port or an address it resolved to.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        interval: float = DEFAULT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._interval = interval
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._state = "unknown"
        self._reason: str | None = "disabled" if not host else None
        self._checked_at: str | None = None
        self._checked_monotonic: float | None = None

    def status(self) -> dict[str, Any]:
        """The last answer, without touching the network."""
        with self._lock:
            return self._answer()

    def refresh(self) -> dict[str, Any]:
        """Return a fresh answer, checking at most once per interval.

        The check runs while holding the lock, so concurrent dashboard requests reuse one
        network round trip instead of starting a check each.
        """
        with self._lock:
            if not self._host:
                return self._answer()  # switched off: no connection, no "checked" time
            now = self._clock()
            if (
                self._checked_monotonic is not None
                and now - self._checked_monotonic < self._interval
            ):
                return self._answer()
            self._state = self._probe_safely()
            self._checked_at = self._wall_clock().isoformat(timespec="seconds")
            self._checked_monotonic = self._clock()
            return self._answer()

    @property
    def enabled(self) -> bool:
        return bool(self._host)

    def _answer(self) -> dict[str, Any]:
        answer: dict[str, Any] = {"state": self._state, "checked_at": self._checked_at}
        if self._state == "unknown" and self._reason:
            answer["reason"] = self._reason
        return answer

    def _probe_safely(self) -> str:
        try:
            state = self._probe()
        except Exception as exc:  # a status check must never take the API down
            LOGGER.warning(
                "the internet reachability check failed",
                extra={"host": self._host, "error": type(exc).__name__},
            )
            self._reason = "error"
            return "unknown"
        self._reason = None
        return state

    def _probe(self) -> str:
        try:
            with socket.create_connection((self._host, self._port), timeout=self._timeout):
                return "ok"
        except OSError:
            return "offline"


def watchdog_view(status: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Whether the watchdog is answering and its last poll is fresh.

    `state` is ok | blind | stale | unreachable | starting. The VPN's own state is deliberately
    not interpreted here: a watchdog that answers "down" is healthy, only its freshness matters.
    """
    data = status if isinstance(status, dict) else {}
    raw_last_poll = data.get("last_poll")
    last_poll = raw_last_poll if isinstance(raw_last_poll, str) else None
    state = data.get("state")
    if state not in ("ok", "down"):
        if state == "starting":
            view = "starting"
        elif state == "blind":
            view = "blind"
        else:
            view = "unreachable"
    else:
        moment = parse_time(last_poll) if last_poll else None
        if moment is None:
            view = "stale"
        else:
            age = ((now or datetime.now(UTC)) - moment).total_seconds()
            view = "ok" if age <= WATCHDOG_STALE_SECONDS else "stale"
    return {"state": view, "last_poll": last_poll}
