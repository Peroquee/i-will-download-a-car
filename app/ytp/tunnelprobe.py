"""A reachability probe through the VPN tunnel (issue #81).

gluetun's control server says `running` while its OpenVPN process loops through failed handshakes,
and its last public address can be stale after a tunnel dies, so neither proves that traffic gets
out. This does: it asks gluetun's own HTTP proxy (`HTTPPROXY=on`, inside the stack's network only,
the proxy the app's yt-dlp already uses) to open a TCP connection to a well-known public address.
The proxy runs inside gluetun's network namespace, whose firewall lets nothing leave outside the
tunnel, so the connection can only succeed over the VPN. Only the `CONNECT` handshake is done:
no byte of payload is sent, nothing is read, the connection is closed at once.

Several targets are tried in turn and ONE success is enough: a single blocked address or a
provider that drops one of them must not look like a dead tunnel. The targets are plain
`host:port` entries from `TUNNEL_PROBE_TARGETS` (default two public DNS resolvers on 443; IP
addresses, so no DNS lookup is part of the probe).
"""

from __future__ import annotations

import http.client
import ipaddress
import re
from collections.abc import Callable
from urllib.parse import urlsplit

DEFAULT_TARGETS = "1.1.1.1:443,8.8.8.8:443"
_TARGET = re.compile(r"[A-Za-z0-9.\-:\[\]]+:[0-9]{1,5}")


def parse_targets(text: str) -> tuple[tuple[str, int], ...]:
    """`1.1.1.1:443,8.8.8.8:443` as ((host, port), ...). ValueError names the bad entry's number."""
    targets: list[tuple[str, int]] = []
    for index, item in enumerate(text.replace(",", " ").split(), start=1):
        if not _TARGET.fullmatch(item):
            raise ValueError(f"entry {index} is not host:port")
        host, _, port = item.rpartition(":")
        if not 1 <= int(port) <= 65535:
            raise ValueError(f"entry {index} has a port outside 1 to 65535")
        host = host.strip("[]")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None  # a host name: cannot be checked here
        if address is not None and not address.is_global:
            # LAN addresses are reachable without the tunnel (FIREWALL_OUTBOUND_SUBNETS): the
            # probe would pass while the tunnel is dead.
            raise ValueError(f"entry {index} is not a public address")
        targets.append((host, int(port)))
    if not targets:
        raise ValueError("at least one host:port is needed")
    return tuple(targets)


def parse_proxy(url: str) -> tuple[str, int]:
    """`http://gluetun:8888` as (host, port); anything but a plain http proxy is refused."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname or parts.path.strip("/") or parts.username:
        raise ValueError("must be http://host:port (no path, no login)")
    return parts.hostname, parts.port or 80


Connector = Callable[[str, int, str, int, float], None]


def connect_through_proxy(
    proxy_host: str, proxy_port: int, host: str, port: int, timeout: float
) -> None:
    """Open (and at once close) a CONNECT tunnel to host:port through the proxy; OSError if not."""
    conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout)
    conn.set_tunnel(host, port)
    try:
        conn.connect()  # sends CONNECT and reads the status line: non-200 raises OSError
    finally:
        conn.close()


class TunnelProbe:
    """`check()` is True when at least one target can be reached through the tunnel."""

    def __init__(
        self,
        proxy_url: str,
        targets: tuple[tuple[str, int], ...],
        *,
        timeout: float = 4.0,
        connector: Connector = connect_through_proxy,
    ):
        self._proxy = parse_proxy(proxy_url)
        self._targets = targets
        self._timeout = timeout
        self._connector = connector
        self.last_error = ""

    def check(self) -> bool:
        errors: list[str] = []
        for host, port in self._targets:
            try:
                self._connector(self._proxy[0], self._proxy[1], host, port, self._timeout)
            except (OSError, http.client.HTTPException) as exc:
                errors.append(type(exc).__name__)
                continue
            self.last_error = ""
            return True
        self.last_error = "no target reachable through the tunnel (" + ", ".join(errors) + ")"
        return False
