"""The app's view of the VPN watchdog (issue #22): one read-only GET of its status.

The watchdog is a separate container and stays independent of the app; the app only LOOKS at
it. `fetch_status` never raises: a watchdog that is down, slow or answers nonsense is reported
as `{"state": "unknown"}`, which the UI shows as a grey badge. The status holds no secrets and
no addresses.
"""

from __future__ import annotations

import http.client
import json
from typing import Any
from urllib.parse import urlsplit

KNOWN_STATES = ("ok", "down", "blind", "starting")


class WatchdogStatusClient:
    def __init__(self, base_url: str, *, timeout: float = 2.0):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname or parts.path.strip("/"):
            raise ValueError(f"base_url must be http://host[:port] without a path: {base_url!r}")
        self._host = parts.hostname
        self._port = parts.port or 80
        self.base_url = f"http://{parts.netloc}"
        self._timeout = timeout

    def fetch_status(self) -> dict[str, Any]:
        unknown: dict[str, Any] = {"state": "unknown", "reason": "the watchdog does not answer"}
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request("GET", "/status", headers={"Accept": "application/json"})
            response = conn.getresponse()
            body = response.read(65536)
            if response.status != 200:
                return unknown
            data = json.loads(body)
        except (OSError, http.client.HTTPException, ValueError):
            return unknown
        finally:
            conn.close()
        if not isinstance(data, dict) or data.get("state") not in KNOWN_STATES:
            return {"state": "unknown", "reason": "the watchdog returned an unreadable status"}
        # Pass on only what the badge needs, whatever else the watchdog may send.
        keys = (
            "state", "since", "reason", "paused", "last_poll", "auto_resume",
            "egress", "egress_checked", "egress_country",
            "cause", "tunnel", "tunnel_since", "probe", "restart", "restarts",  # #81
        )  # fmt: skip
        return {key: data[key] for key in keys if key in data}
