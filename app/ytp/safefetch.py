"""Fetching from hosts the owner does not control, without becoming a way into the LAN (issue #34).

A podcast feed and the episode files it names live on arbitrary hosts, and a feed can name any
address it likes in its `<enclosure url=...>`, including `http://192.168.178.1/` or
`http://localhost:8000/`. Fetching those from inside the home network would let a hostile feed
probe and read LAN services (SSRF). Everything that fetches from such a host goes through this
module, which enforces:

* **Address syntax.** http or https only, no login in the address (`user:pass@`), a port from a
  short list (80, 443, 8080, 8443), a plain host name or an IP address, at most 2000 characters.
* **Public addresses only.** The host name is resolved HERE and every address it resolves to must
  be a global one. Loopback, private (10/8, 172.16/12, 192.168/16, so the home LAN), link-local,
  carrier-grade NAT (100.64/10), multicast, unspecified, reserved and documentation ranges are
  refused, as are IPv6 forms that wrap an IPv4 address (mapped, NAT64, 6to4, Teredo). If ANY
  address of a name is not public the name is refused (a mixed answer is how rebinding starts).
  IP literals in any spelling (`127.1`, `2130706433`, `0x7f.1`, `[::1]`) are resolved by the same
  system call and so judged by the same rule.
* **The checked address is the one connected to.** The connection goes to the IP that was
  checked, with the real host name only in the TLS handshake (SNI and certificate check) and the
  `Host` header. A second DNS lookup by someone else cannot change where the request goes.
  Through an HTTP proxy this matters even more: gluetun's proxy can reach the LAN
  (`FIREWALL_OUTBOUND_SUBNETS`), and a proxy that resolved the name itself could be sent there by a
  name that answers a public address first and a private one later. So the proxy is given the IP
  (`CONNECT 93.184.216.34:443`, or an absolute-URI request to the IP), never the name.
* **Every redirect is checked again** from scratch, at most 5 of them.
* **Limits.** Connect and read timeouts, an overall deadline for a download, a size limit
  (checked against `Content-Length` and counted while reading), no compressed transfer
  (`Accept-Encoding: identity`; a compressed answer is refused), and a content-type check per
  kind of thing (feed, media, image) plus a look at the first bytes (an HTML or JSON page
  behind an "mp3" address is refused instead of being filed as an episode).

Residual risk, also in docs/podcasts.md: the host name is looked up by the app's own resolver,
outside the VPN, so the home DNS server sees which podcast hosts are used (not what is fetched);
and a public host the feed points at is trusted to serve what it says. Only the bytes are filed,
never executed, and file names come from our own metadata, never from the server.

Failures are `FetchError(kind, message)`: `invalid` (not a usable address), `blocked` (refused by
the rules above), `network`, `not_found` (404/410), `temporary` (429/5xx), `bad_response`,
`too_large`. The message is safe to show to people.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urljoin, urlsplit

from . import __version__

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 2000
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
MAX_REDIRECTS = 5
MAX_ADDRESSES_TRIED = 3
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 20.0
CHUNK = 64 * 1024
HOST_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")

# IPv6 ranges that carry an IPv4 address inside: refused whatever they wrap.
_WRAPPED_V6 = tuple(
    ipaddress.ip_network(net)
    for net in ("::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)


class FetchError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Target:
    """A checked address: where to connect (after resolving) and what to ask for."""

    scheme: str
    host: str  # ASCII, lower case; an IP literal without brackets
    port: int
    path: str  # path and query, at least "/"
    url: str  # the normalised address without a fragment

    @property
    def default_port(self) -> bool:
        return self.port == (443 if self.scheme == "https" else 80)

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.default_port else f"{host}:{self.port}"


def check_url(url: str) -> Target:
    """Syntax rules only (no network). Raises FetchError("invalid" or "blocked")."""
    if not isinstance(url, str):
        raise FetchError("invalid", "the address must be text")
    value = url.strip()
    if not value:
        raise FetchError("invalid", "the address is empty")
    if len(value) > MAX_URL_LENGTH:
        raise FetchError("invalid", f"the address is longer than {MAX_URL_LENGTH} characters")
    if not value.isprintable() or any(ch.isspace() for ch in value):
        raise FetchError("invalid", "the address must be a single line without spaces")
    try:
        parts: SplitResult = urlsplit(value)
        port = parts.port
    except ValueError:
        raise FetchError("invalid", "the address cannot be read") from None
    if parts.scheme not in {"http", "https"}:
        raise FetchError("invalid", "the address must start with http:// or https://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FetchError("invalid", "the address must not contain a login")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError("invalid", "the address has no host name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise FetchError("invalid", "the host name is not valid") from None
        # Anything but letters, digits, hyphens and dots (a spelled-out IP like 0x7f.1 passes
        # this and is judged after resolving, below).
        if not HOST_NAME.fullmatch(host) or len(host) > 253:
            raise FetchError("invalid", "the host name is not valid") from None
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise FetchError("blocked", f"port {port} is not allowed (only 80, 443, 8080, 8443)")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    default = port == (443 if parts.scheme == "https" else 80)
    shown_host = f"[{host}]" if ":" in host else host
    normalised = f"{parts.scheme}://{shown_host}{'' if default else f':{port}'}{path}"
    return Target(parts.scheme, host, port, path, normalised)


def is_public_address(text: str) -> bool:
    """True only for an address on the public internet (see the module description)."""
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and any(address in net for net in _WRAPPED_V6):
        return False
    return address.is_global and not address.is_multicast


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


Resolver = Callable[[str, int], list[str]]
Dialer = Callable[[Target, str], socket.socket]


def resolve_public(target: Target, resolver: Resolver = system_resolver) -> list[str]:
    """The addresses to connect to, IPv4 first; raises FetchError("blocked"/"network")."""
    try:
        ipaddress.ip_address(target.host)
        found = [target.host]  # already an address: nothing to look up
    except ValueError:
        found = None
    try:
        if found is None:
            found = resolver(target.host, target.port)
    except OSError:
        raise FetchError(
            "network", f"the name {target.host} could not be found (no such host?)"
        ) from None
    if not found:
        raise FetchError("network", f"the name {target.host} could not be found")
    if not all(is_public_address(address) for address in found):
        # Not echoed: this may be a private address of the home network.
        logger.warning("refused a non-public address", extra={"host": target.host})
        raise FetchError(
            "blocked", "this address points into a private or local network and is refused"
        )
    return sorted(found, key=lambda a: ":" in a)


# ------------------------------------------------------------------------------ connecting


def _connect_through_proxy(proxy_host: str, proxy_port: int, ip: str, port: int) -> socket.socket:
    """An HTTP proxy tunnel to ip:port (the IP, not the name). Raises OSError on any failure."""
    sock = socket.create_connection((proxy_host, proxy_port), timeout=CONNECT_TIMEOUT)
    try:
        sock.settimeout(READ_TIMEOUT)
        shown = f"[{ip}]" if ":" in ip else ip
        sock.sendall(
            f"CONNECT {shown}:{port} HTTP/1.1\r\nHost: {shown}:{port}\r\n"
            f"User-Agent: youtube-premium/{__version__}\r\n\r\n".encode("ascii")
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk or len(head) > 8192:
                raise OSError("the proxy closed the connection")
            head += chunk
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if not re.match(r"HTTP/1\.[01] 200\b", status_line):
            raise OSError(f"the proxy refused the tunnel: {status_line[:80]}")
        return sock
    except BaseException:
        sock.close()
        raise


def make_dialer(proxy: str = "", ssl_context: ssl.SSLContext | None = None) -> Dialer:
    """The real way to reach a checked address: directly, or through an HTTP proxy tunnel."""
    proxy_parts = urlsplit(proxy) if proxy else None

    def dial(target: Target, ip: str) -> socket.socket:
        if proxy_parts is not None and target.scheme == "https":
            raw = _connect_through_proxy(
                proxy_parts.hostname or "", proxy_parts.port or 8888, ip, target.port
            )
        elif proxy_parts is not None:
            # Plain http through the proxy: a normal connection to the proxy; the request itself
            # names the IP (see SafeFetcher._request).
            raw = socket.create_connection(
                (proxy_parts.hostname or "", proxy_parts.port or 8888), timeout=CONNECT_TIMEOUT
            )
        else:
            raw = socket.create_connection((ip, target.port), timeout=CONNECT_TIMEOUT)
        raw.settimeout(READ_TIMEOUT)
        if target.scheme != "https":
            return raw
        context = ssl_context or ssl.create_default_context()
        try:
            return context.wrap_socket(raw, server_hostname=target.host)
        except BaseException:
            raw.close()
            raise

    return dial


# ------------------------------------------------------------------------------ content rules

_FEED_OK = re.compile(r"(xml|rss|atom)$")
_GENERIC_BINARY = {"application/octet-stream", "binary/octet-stream", "application/binary"}
_MEDIA_EXTRA = {"application/ogg"}
_IMAGE_OK = {"image/jpeg", "image/jpg", "image/png"}


def content_type_ok(kind: str, content_type: str) -> bool:
    """Is this Content-Type plausible for a feed, a media file or an image? Empty = unknown = ok
    (the first bytes are looked at as well)."""
    main = content_type.split(";", 1)[0].strip().lower()
    if not main:
        return True
    if kind == "feed":
        return bool(_FEED_OK.search(main)) or main == "text/plain" or main in _GENERIC_BINARY
    if kind == "media":
        return (
            main.startswith(("audio/", "video/")) or main in _MEDIA_EXTRA or main in _GENERIC_BINARY
        )
    if kind == "image":
        return main in _IMAGE_OK or main in _GENERIC_BINARY
    return False


def looks_like_markup(head: bytes) -> bool:
    """The start of an HTML, XML or JSON document: never a media file."""
    start = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    return start in {b"<", b"{", b"["}


def is_jpeg(head: bytes) -> bool:
    return head.startswith(b"\xff\xd8\xff")


def is_png(head: bytes) -> bool:
    return head.startswith(b"\x89PNG\r\n\x1a\n")


# ------------------------------------------------------------------------------ the fetcher


@dataclass(frozen=True)
class Fetched:
    url: str  # the address of the final answer (after redirects)
    content_type: str
    body: bytes = b""
    size: int = 0


class SafeFetcher:
    """GET with the rules of this module. `proxy` EMPTY = connect directly."""

    def __init__(
        self,
        proxy: str = "",
        *,
        resolver: Resolver = system_resolver,
        dialer: Dialer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.proxy = proxy.strip()
        self._resolver = resolver
        self._dial = dialer or make_dialer(self.proxy)
        self._clock = clock

    def __repr__(self) -> str:
        return f"SafeFetcher(proxy={'set' if self.proxy else 'none'})"

    # -- one request, following redirects --------------------------------------------------

    def _request(
        self, target: Target, ip: str
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        sock = self._dial(target, ip)
        conn = http.client.HTTPConnection(target.host, target.port, timeout=READ_TIMEOUT)
        conn.sock = sock  # already connected (and TLS-wrapped for https) to the checked address
        headers = {
            "Host": target.host_header,
            "User-Agent": f"youtube-premium/{__version__} (podcast fetcher)",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        # Plain http through a proxy is an absolute-URI request, to the IP: the proxy never
        # looks the name up. (https goes through a CONNECT tunnel to the IP, see make_dialer.)
        if self.proxy and target.scheme == "http":
            shown = f"[{ip}]" if ":" in ip else ip
            request_target = f"http://{shown}:{target.port}{target.path}"
        else:
            request_target = target.path
        try:
            conn.request("GET", request_target, headers=headers)
            return conn, conn.getresponse()
        except BaseException:
            conn.close()
            raise

    def _open(self, url: str, kind: str):
        """Follow redirects (each hop fully checked); returns (conn, response, final Target)."""
        target = check_url(url)
        for _hop in range(MAX_REDIRECTS + 1):
            addresses = resolve_public(target, self._resolver)
            conn = response = None
            last_error: Exception | None = None
            for ip in addresses[:MAX_ADDRESSES_TRIED]:
                try:
                    conn, response = self._request(target, ip)
                    break
                except (OSError, http.client.HTTPException, ssl.SSLError) as error:
                    last_error = error
            if response is None or conn is None:
                logger.info(
                    "fetch failed",
                    extra={"host": target.host, "error": type(last_error).__name__},
                )
                raise FetchError("network", f"{target.host} could not be reached") from None
            status = response.status
            if status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location") or ""
                conn.close()
                if not location:
                    raise FetchError("bad_response", "a redirect without a destination")
                target = check_url(urljoin(target.url, location))
                continue
            if status in {404, 410}:
                conn.close()
                raise FetchError("not_found", f"{target.host} answered: not found (HTTP {status})")
            if status == 429 or status >= 500:
                conn.close()
                raise FetchError("temporary", f"{target.host} answered HTTP {status}; try later")
            if status != 200:
                conn.close()
                raise FetchError("bad_response", f"{target.host} answered HTTP {status}")
            encoding = (response.getheader("Content-Encoding") or "identity").lower()
            if encoding != "identity":
                conn.close()
                raise FetchError("bad_response", "the server sent a compressed answer")
            content_type = response.getheader("Content-Type") or ""
            if not content_type_ok(kind, content_type):
                conn.close()
                raise FetchError(
                    "bad_response",
                    f"unexpected content type {content_type.split(';')[0][:60]!r}",
                )
            return conn, response, target, content_type
        raise FetchError("bad_response", f"more than {MAX_REDIRECTS} redirects")

    @staticmethod
    def _declared_size(response: http.client.HTTPResponse, max_bytes: int) -> None:
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise FetchError("too_large", f"the file is larger than {max_bytes // 1024**2} MiB")

    # -- the two public ways to use it -------------------------------------------------------

    def fetch_bytes(self, url: str, *, kind: str, max_bytes: int) -> Fetched:
        """A small answer (feed, cover) in memory."""
        conn, response, target, content_type = self._open(url, kind)
        try:
            self._declared_size(response, max_bytes)
            try:
                body = response.read(max_bytes + 1)
            except (OSError, http.client.HTTPException):
                raise FetchError("network", f"{target.host} stopped answering") from None
        finally:
            conn.close()
        if len(body) > max_bytes:
            raise FetchError("too_large", f"the answer is larger than {max_bytes // 1024} KiB")
        return Fetched(target.url, content_type, body, len(body))

    def download(
        self,
        url: str,
        destination: Path,
        *,
        kind: str,
        max_bytes: int,
        deadline_seconds: float = 3600.0,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Fetched:
        """Stream a big answer into `destination` (which must not exist yet)."""
        conn, response, target, content_type = self._open(url, kind)
        started = self._clock()
        total_text = response.getheader("Content-Length")
        total = int(total_text) if total_text and total_text.isdigit() else None
        written = 0
        try:
            self._declared_size(response, max_bytes)
            out = destination.open("xb")  # FileExistsError here: nothing of ours to clean up
        except BaseException:
            conn.close()
            raise
        try:
            with out:
                while True:
                    if self._clock() - started > deadline_seconds:
                        raise FetchError("network", "the download took too long and was stopped")
                    try:
                        chunk = response.read(CHUNK)
                    except (OSError, http.client.HTTPException):
                        raise FetchError(
                            "network", f"{target.host} stopped answering during the download"
                        ) from None
                    if not chunk:
                        break
                    if written == 0 and looks_like_markup(chunk[:64]):
                        raise FetchError(
                            "bad_response", "the server sent a web page instead of the file"
                        )
                    written += len(chunk)
                    if written > max_bytes:
                        raise FetchError(
                            "too_large", f"the file is larger than {max_bytes // 1024**2} MiB"
                        )
                    out.write(chunk)
                    if progress is not None:
                        progress(written, total)
            if total is not None and written < total:
                raise FetchError("network", "the download ended early")
            if written == 0:
                raise FetchError("bad_response", "the server sent an empty file")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            conn.close()
        return Fetched(target.url, content_type, b"", written)
