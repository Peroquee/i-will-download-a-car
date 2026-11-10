"""Gluetun's HTTP control server (issues #21, #81).

The watchdog asks it three things: `GET /v1/vpn/status`, which answers `{"status": "running"}`
while gluetun's VPN *process loop* runs (`stopped`, `crashed` and the like otherwise; "running"
does NOT mean the tunnel is connected: it also says running while OpenVPN fails its handshake
again and again, #81), `GET /v1/publicip/ip` (empty while there is no tunnel), and, only when
the owner allows the watchdog to restart a stuck VPN, `PUT /v1/vpn/status`. Gluetun keeps every
route of the control server private, so the watchdog gets its own API key with a role for exactly
these routes (`scripts/watchdog/setup_key.py` writes `config/gluetun/auth/config.toml`). The key
goes in the `X-API-Key` header and is never logged.

Failures are told apart on purpose, because the watchdog acts differently on them:

* `GluetunUnreachable`: refused, timeout, DNS, a 5xx, a garbled answer. Gluetun is down or busy:
  that counts as a VPN failure.
* `GluetunNoAddress` (a kind of `GluetunUnreachable`): gluetun answered, but has no public
  address, i.e. no tunnel (#81). The watchdog tells this apart from "cannot ask".
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


class GluetunNoAddress(GluetunUnreachable):
    """Gluetun answered but has no public address: the tunnel is not connected."""


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

    def _request(
        self, method: str, path: str, payload: object | None = None, *, json_answer: bool = True
    ) -> object:
        """Call a route of the control server with the key; typed errors as described above."""
        conn_class = (
            http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_class(self._host, self._port, timeout=self._timeout)
        headers = {"X-API-Key": self._api_key}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            text = response.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            raise GluetunUnreachable(f"cannot reach gluetun at {self.base_url}: {exc}") from exc
        finally:
            conn.close()
        if status in (401, 403, 404):
            raise GluetunAuthError(
                f"gluetun's control server answered {status} for {method} {path}: the watchdog "
                "key or config/gluetun/auth/config.toml is missing, wrong or out of date "
                "(make watchdog-key, then recreate gluetun)"
            )
        if status != 200:
            raise GluetunUnreachable(f"gluetun's control server answered {status}")
        if not json_answer:
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise GluetunUnreachable(
                "gluetun's control server returned an unreadable answer"
            ) from exc

    def _get_json(self, path: str) -> object:
        return self._request("GET", path)

    def vpn_status(self) -> str:
        """The VPN's status word ("running", "stopped", ...)."""
        data = self._get_json("/v1/vpn/status")
        value = data.get("status") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value:
            raise GluetunUnreachable("gluetun's control server returned an unreadable status")
        return value.lower()

    def set_vpn_status(self, status: str) -> None:
        """`PUT /v1/vpn/status {"status": "stopped" | "running"}`: stop or start the VPN.

        Needs the extra route in the watchdog's role (setup_key.py); without it this raises
        `GluetunAuthError`. Used only for the watchdog's restart of a stuck VPN (#81)."""
        if status not in {"running", "stopped"}:
            raise ValueError("status must be 'running' or 'stopped'")
        self._request("PUT", "/v1/vpn/status", {"status": status}, json_answer=False)

    def public_ip(self) -> PublicIP:
        """The address gluetun sees as its own, and where it is (`GET /v1/publicip/ip`)."""
        data = self._get_json("/v1/publicip/ip")
        ip = data.get("public_ip") if isinstance(data, dict) else None
        if not isinstance(ip, str) or not ip:
            raise GluetunNoAddress("gluetun has no public address (the tunnel may be restarting)")
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
