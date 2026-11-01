"""Gluetun's HTTP control server, read-only (issue #21).

Only one question is asked of it: `GET /v1/vpn/status`, which answers `{"status": "running"}`
while the VPN is up (`stopped`, `crashed` and the like otherwise). Gluetun keeps every route of
the control server private, so the watchdog gets its own API key with a role that may do
exactly that one GET (`scripts/watchdog/setup_key.py` writes `config/gluetun/auth/config.toml`).
The key goes in the `X-API-Key` header and is never logged.

Failures are told apart on purpose, because the watchdog acts differently on them:

* `GluetunUnreachable`: refused, timeout, DNS, a 5xx, a garbled answer. Gluetun is down or busy:
  that counts as a VPN failure.
* `GluetunAuthError`: 401/403/404. The watchdog is not set up right (no config file, wrong key,
  an older gluetun without the route). That is a configuration problem to shout about, not
  evidence that the VPN is down.
"""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from urllib.parse import urlsplit


class GluetunError(Exception):
    """Base class."""


class GluetunUnreachable(GluetunError):
    """Cannot reach the control server, or it answered nonsense or a server error."""


class GluetunAuthError(GluetunError):
    """The control server refused the key or does not have the route: a setup problem."""


class GluetunControl:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 2.0):
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
        return f"GluetunControl({self.base_url!r})"

    def _get_json(self, path: str) -> object:
        """GET a route of the control server with the key; typed errors as described above."""
        conn_class = (
            http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_class(self._host, self._port, timeout=self._timeout)
        try:
            conn.request("GET", path, headers={"X-API-Key": self._api_key})
            response = conn.getresponse()
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            raise GluetunUnreachable(f"cannot reach gluetun at {self.base_url}: {exc}") from exc
        finally:
            conn.close()
        if status in (401, 403, 404):
            raise GluetunAuthError(
                f"gluetun's control server answered {status} for {path}: the watchdog key or "
                "config/gluetun/auth/config.toml is missing, wrong or out of date "
                "(make watchdog-key, then recreate gluetun)"
            )
        if status != 200:
            raise GluetunUnreachable(f"gluetun's control server answered {status}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise GluetunUnreachable(
                "gluetun's control server returned an unreadable answer"
            ) from exc

    def vpn_status(self) -> str:
        """The VPN's status word ("running", "stopped", ...)."""
        data = self._get_json("/v1/vpn/status")
        value = data.get("status") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value:
            raise GluetunUnreachable("gluetun's control server returned an unreadable status")
        return value.lower()

    def public_ip(self) -> PublicIP:
        """The address gluetun sees as its own, and where it is (`GET /v1/publicip/ip`)."""
        data = self._get_json("/v1/publicip/ip")
        ip = data.get("public_ip") if isinstance(data, dict) else None
        if not isinstance(ip, str) or not ip:
            raise GluetunUnreachable("gluetun has no public address (the tunnel may be restarting)")
        assert isinstance(data, dict)
        return PublicIP(
            ip=ip,
            country=str(data.get("country") or ""),
            region=str(data.get("region") or ""),
            city=str(data.get("city") or ""),
            organization=str(data.get("organization") or ""),
        )


@dataclass(frozen=True)
class PublicIP:
    """What gluetun reports about the VPN's public address (never log `ip` in full)."""

    ip: str
    country: str = ""
    region: str = ""
    city: str = ""
    organization: str = ""

    def __repr__(self) -> str:
        return f"PublicIP(country={self.country!r}, organization={self.organization!r})"
