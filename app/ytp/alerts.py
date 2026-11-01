"""Alerts for the VPN watchdog (issue #22): ntfy or a generic webhook, off by default.

`ALERT_URL` is the target; EMPTY MEANS NO ALERTS. There is deliberately no default service:
sending anything out of the LAN is the owner's decision (docs/watchdog.md, "Open question").
The URL itself is treated as a secret (an ntfy topic URL works like a password) and is never
logged; the log names only the host.

* `ALERT_FORMAT=ntfy` (default): a plain-text POST as ntfy expects, with `Title`, `Priority`
  and `Tags` headers (ASCII only, since HTTP headers are not UTF-8);
* `ALERT_FORMAT=webhook`: a JSON POST `{"event", "title", "message", "timestamp", ...}` for
  anything that takes a webhook (Home Assistant, Gotify bridges, a script).
* `ALERT_TOKEN` (optional secret) is sent as `Authorization: Bearer <token>`.

Sending never blocks the caller: `notify` puts the message on a queue and one worker thread
delivers them in order, retrying a few times with a growing pause and then giving up with an
error in the log. A dead alert target therefore cannot delay a VPN poll. The texts the watchdog
puts in a message are built from its own state (never from the VPN's addresses).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FORMATS = ("ntfy", "webhook")


@dataclass(frozen=True)
class Alert:
    kind: str  # "down", "recovered", "blind", "sighted" ...
    title: str  # short, ASCII
    message: str
    timestamp: str  # ISO 8601 UTC
    urgent: bool = False


class Alerter:
    def __init__(
        self,
        url: str,
        token: str = "",
        fmt: str = "ntfy",
        *,
        timeout: float = 5.0,
        retry_pauses: tuple[float, ...] = (2.0, 10.0, 30.0),
        sleep: Callable[[float], None] = time.sleep,
    ):
        if fmt not in FORMATS:
            raise ValueError(f"ALERT_FORMAT must be one of {', '.join(FORMATS)}, got {fmt!r}")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("ALERT_URL must be an http(s) address")
        self._parts = parts
        self._token = token
        self._format = fmt
        self._timeout = timeout
        self._retry_pauses = retry_pauses
        self._sleep = sleep
        self.host = parts.hostname  # the only part of the URL that is ever logged
        self.sent = 0
        self.dropped = 0
        self._queue: queue.Queue[Alert | None] = queue.Queue(maxsize=50)
        self._thread = threading.Thread(target=self._work, name="alerts", daemon=True)
        self._thread.start()

    def __repr__(self) -> str:
        return f"Alerter(host={self.host!r}, format={self._format!r})"

    enabled = True

    def notify(self, alert: Alert) -> None:
        """Queue an alert. Never raises, never waits."""
        try:
            self._queue.put_nowait(alert)
        except queue.Full:
            self.dropped += 1
            logger.error("alert queue is full, dropping an alert", extra={"kind": alert.kind})

    def close(self, wait: float = 3.0) -> None:
        """Deliver what is queued (up to `wait` seconds) and stop the worker."""
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(wait)

    # ------------------------------------------------------------------ worker

    def _work(self) -> None:
        while True:
            alert = self._queue.get()
            if alert is None:
                return
            self._deliver(alert)

    def _deliver(self, alert: Alert) -> None:
        attempts = 1 + len(self._retry_pauses)
        error = ""
        for attempt in range(attempts):
            try:
                self._post(alert)
            except ValueError as exc:
                error = type(exc).__name__  # its text can contain the URL path, which is a secret
            except (OSError, http.client.HTTPException) as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                self.sent += 1
                logger.info("alert sent", extra={"kind": alert.kind, "host": self.host})
                return
            if attempt < attempts - 1:
                self._sleep(self._retry_pauses[attempt])
        self.dropped += 1
        logger.error(
            "could not deliver an alert, giving up",
            extra={"kind": alert.kind, "host": self.host, "error": error},
        )

    def _post(self, alert: Alert) -> None:
        headers = {"User-Agent": "youtube-premium-watchdog"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._format == "ntfy":
            body = alert.message.encode("utf-8")
            headers |= {
                "Content-Type": "text/plain; charset=utf-8",
                "Title": alert.title.encode("ascii", "replace").decode(),
                "Priority": "urgent" if alert.urgent else "default",
                "Tags": "rotating_light" if alert.urgent else "white_check_mark",
            }
        else:
            body = json.dumps(
                {
                    "event": alert.kind,
                    "title": alert.title,
                    "message": alert.message,
                    "timestamp": alert.timestamp,
                    "urgent": alert.urgent,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        parts = self._parts
        conn_class = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_class(parts.hostname, parts.port, timeout=self._timeout)
        try:
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            conn.request("POST", target, body=body, headers=headers)
            response = conn.getresponse()
            response.read()
            if not 200 <= response.status < 300:
                raise http.client.HTTPException(f"HTTP {response.status}")
        finally:
            conn.close()
