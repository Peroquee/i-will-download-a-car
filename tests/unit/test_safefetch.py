"""Fetching from arbitrary hosts without reaching into the LAN (issue #34).

No test here touches the real network: names are "resolved" by a fake resolver, and the
connection goes to a throwaway HTTP server on localhost through a fake dialer, which also records
WHICH address the fetcher decided to connect to.
"""

# ruff: noqa: E501

from __future__ import annotations

import http.server
import ipaddress
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ytp import safefetch
from ytp.safefetch import (
    FetchError,
    SafeFetcher,
    check_url,
    content_type_ok,
    is_public_address,
    resolve_public,
)

PUBLIC = "93.184.216.34"


# --- the address syntax ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "host", "port", "path"),
    [
        ("https://example.org/feed.xml", "example.org", 443, "/feed.xml"),
        ("http://example.org", "example.org", 80, "/"),
        ("  https://Example.ORG/a?b=c#frag  ", "example.org", 443, "/a?b=c"),
        ("http://example.org:8080/x", "example.org", 8080, "/x"),
        ("https://example.org.:443/x", "example.org", 443, "/x"),
        ("https://[2606:4700::1111]/x", "2606:4700::1111", 443, "/x"),
        ("https://bücher.example/x", "xn--bcher-kva.example", 443, "/x"),
    ],
)
def test_good_addresses_are_normalised(url, host, port, path):
    target = check_url(url)
    assert (target.host, target.port, target.path) == (host, port, path)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://example.org/x",
        "file:///etc/passwd",
        "gopher://example.org/",
        "javascript:alert(1)",
        "//example.org/x",
        "example.org/x",
        "https://",
        "https:///x",
        "https://user:pass@example.org/x",
        "https://user@example.org/x",
        "https://example.org@evil.example/x",
        "https://exa mple.org/x",
        "https://example.org/x\n",  # stripped, fine; the next one is a real newline inside
        "https://exam\nple.org/x",
        "https://exa_mple.org/x",
        "https://-bad.example/",
        "https://bad..example/",
        "https://example.org:99999/x",
        "https://example.org:abc/x",
        "x" * 2001,
    ],
)
def test_bad_addresses_are_invalid(url):
    if url == "https://example.org/x\n":
        assert check_url(url).host == "example.org"
        return
    with pytest.raises(FetchError) as error:
        check_url(url)
    assert error.value.kind in {"invalid", "blocked"}


@pytest.mark.security
@pytest.mark.parametrize("port", [22, 25, 81, 3306, 5432, 6379, 8000, 8096, 8081, 9000])
def test_only_the_web_ports_are_allowed(port):
    with pytest.raises(FetchError) as error:
        check_url(f"https://example.org:{port}/x")
    assert error.value.kind == "blocked"


# --- which addresses count as public --------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.1.2.3", "0.0.0.0", "10.0.0.5", "172.16.0.1", "172.31.255.255",
        "192.168.178.1", "192.168.178.123", "192.168.178.108", "169.254.169.254",
        "100.64.0.1", "100.127.255.255", "224.0.0.1", "255.255.255.255", "240.0.0.1",
        "192.0.2.1", "198.51.100.7", "203.0.113.9", "198.18.0.1",
        "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "2001:db8::1",
        "::ffff:127.0.0.1", "::ffff:192.168.178.1", "::ffff:8.8.8.8",
        "64:ff9b::7f00:1", "2002:7f00:1::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "fe80::1%eth0", "not an address", "",
    ],
)  # fmt: skip
def test_private_local_and_odd_addresses_are_not_public(address):
    assert not is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "93.184.216.34",
        "8.8.8.8",
        "1.1.1.1",
        "151.101.1.69",
        "2606:4700:4700::1111",
        "2a00:1450:4001:81b::200e",
    ],
)
def test_public_addresses_are_public(address):
    assert is_public_address(address)


def resolver_returning(*addresses):
    def resolve(host, port):
        return list(addresses)

    return resolve


@pytest.mark.security
def test_a_name_with_one_private_address_among_public_ones_is_refused():
    """The mixed answer is how DNS rebinding starts: the whole name is refused."""
    target = check_url("https://podcast.example/feed")
    with pytest.raises(FetchError) as error:
        resolve_public(target, resolver_returning(PUBLIC, "192.168.178.1"))
    assert error.value.kind == "blocked"
    assert "192.168" not in error.value.message


@pytest.mark.security
@pytest.mark.parametrize("host", ["localhost", "gluetun", "app", "qbittorrent", "nas.lan"])
def test_names_that_resolve_to_internal_addresses_are_refused(host):
    target = check_url(f"http://{host}/x")
    with pytest.raises(FetchError) as error:
        resolve_public(target, resolver_returning("172.18.0.5"))
    assert error.value.kind == "blocked"


@pytest.mark.security
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0x7f.1",
        "0177.0.0.1",
        "[::1]",
        "[::ffff:7f00:1]",
        "192.168.178.1",
        "0",
    ],
)
def test_ip_literals_in_any_spelling_are_judged_by_what_they_resolve_to(host):
    """The real resolver decides what `127.1` or `0x7f.1` mean; the answer is what is checked."""
    try:
        target = check_url(f"http://{host}/x")
    except FetchError:
        return  # refused already by the syntax rules: fine too
    with pytest.raises(FetchError) as error:
        resolve_public(target)  # the system resolver, no network needed for literals
    assert error.value.kind in {"blocked", "network"}


def test_an_unresolvable_name_is_a_network_error_not_a_crash():
    def broken(host, port):
        raise socket.gaierror("no such host")

    with pytest.raises(FetchError) as error:
        resolve_public(check_url("https://nope.example/x"), broken)
    assert error.value.kind == "network"


def test_ipv4_is_tried_before_ipv6():
    got = resolve_public(
        check_url("https://x.example/"), resolver_returning("2606:4700:4700::1111", PUBLIC)
    )
    assert got == [PUBLIC, "2606:4700:4700::1111"]


# --- content rules --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "content_type", "ok"),
    [
        ("feed", "application/rss+xml; charset=utf-8", True),
        ("feed", "text/xml", True),
        ("feed", "application/atom+xml", True),
        ("feed", "text/plain", True),
        ("feed", "application/octet-stream", True),
        ("feed", "", True),
        ("feed", "text/html", False),
        ("feed", "application/json", False),
        ("feed", "audio/mpeg", False),
        ("media", "audio/mpeg", True),
        ("media", "audio/x-m4a", True),
        ("media", "video/mp4", True),
        ("media", "application/octet-stream", True),
        ("media", "binary/octet-stream", True),
        ("media", "", True),
        ("media", "text/html; charset=utf-8", False),
        ("media", "application/json", False),
        ("media", "image/png", False),
        ("image", "image/jpeg", True),
        ("image", "image/png", True),
        ("image", "image/svg+xml", False),
        ("image", "text/html", False),
        ("unknown-kind", "audio/mpeg", False),
    ],
)
def test_content_types(kind, content_type, ok):
    assert content_type_ok(kind, content_type) is ok


@pytest.mark.parametrize(
    ("head", "markup"),
    [(b"<!DOCTYPE html>", True), (b"  <html>", True), (b'{"error": 1}', True), (b"[1,2]", True),
     (b"\xef\xbb\xbf<?xml", True), (b"ID3\x03\x00", False), (b"\xff\xfb\x90", False), (b"OggS", False)],
)  # fmt: skip
def test_markup_is_recognised_by_its_first_byte(head, markup):
    assert safefetch.looks_like_markup(head) is markup


# --- a throwaway server and a fetcher wired to it ---------------------------------------------


class Server:
    def __init__(self):
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.seen: list[str] = []
        self.request_headers: list[dict[str, str]] = []
        self.chunked: set[str] = set()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                outer.seen.append(f"{self.headers.get('Host')} {self.path}")
                outer.request_headers.append(dict(self.headers))
                status, headers, body = outer.routes.get(self.path, (404, {}, b"no"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if self.path in outer.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for i in range(0, len(body), 1000):
                        piece = body[i : i + 1000]
                        self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        class QuietServer(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):  # clients that hang up are normal here
                pass

        self.httpd = QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True
        ).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[Server]:
    srv = Server()
    yield srv
    srv.close()


class World:
    """names -> addresses for the fake resolver, and a log of what the fetcher connected to."""

    def __init__(self, server: Server):
        self.server = server
        self.names: dict[str, list[str]] = {
            "podcast.example": [PUBLIC],
            "cdn.example": ["151.101.1.69"],
        }
        self.dialled: list[tuple[str, str, int]] = []

    def resolve(self, host, port):
        if host in self.names:
            return self.names[host]
        raise socket.gaierror(host)

    def dial(self, target, ip):
        self.dialled.append((target.host, ip, target.port))
        return socket.create_connection(("127.0.0.1", self.server.port))

    def fetcher(self, **kwargs) -> SafeFetcher:
        return SafeFetcher(resolver=self.resolve, dialer=self.dial, **kwargs)


@pytest.fixture
def world(server) -> World:
    return World(server)


def test_a_feed_is_fetched_from_the_checked_address(world, server):
    server.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"}, b"<rss/>")
    got = world.fetcher().fetch_bytes(
        "https://podcast.example/feed.xml", kind="feed", max_bytes=1000
    )
    assert got.body == b"<rss/>"
    assert got.url == "https://podcast.example/feed.xml"
    # connected to the IP that was checked, not to a name; the real name is only the Host header
    assert world.dialled == [("podcast.example", PUBLIC, 443)]
    assert server.seen == ["podcast.example /feed.xml"]


@pytest.mark.security
def test_a_redirect_to_a_private_address_is_refused_and_never_requested(world, server):
    world.names["internal.example"] = ["192.168.178.1"]
    server.routes["/go"] = (302, {"Location": "http://internal.example/admin"}, b"")
    server.routes["/admin"] = (200, {"Content-Type": "text/plain"}, b"secret")
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/go", kind="feed", max_bytes=1000)
    assert error.value.kind == "blocked"
    assert server.seen == ["podcast.example /go"]
    assert [d[0] for d in world.dialled] == ["podcast.example"]


@pytest.mark.security
@pytest.mark.parametrize(
    "location",
    ["http://127.0.0.1/x", "http://localhost/x", "http://[::1]/x", "http://192.168.178.1:8080/x",
     "http://169.254.169.254/latest/meta-data/", "ftp://podcast.example/x", "file:///etc/passwd",
     "http://user:pw@cdn.example/x", "http://cdn.example:6379/x"],
)  # fmt: skip
def test_redirects_to_forbidden_places_are_refused(world, server, location):
    world.names["localhost"] = ["127.0.0.1"]
    server.routes["/go"] = (301, {"Location": location}, b"")
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/go", kind="feed", max_bytes=1000)
    assert error.value.kind in {"blocked", "invalid"}
    assert server.seen == ["podcast.example /go"]


def test_redirects_to_another_public_host_are_followed(world, server):
    server.routes["/a"] = (301, {"Location": "http://cdn.example/b"}, b"")
    server.routes["/b"] = (302, {"Location": "/c"}, b"")
    server.routes["/c"] = (200, {"Content-Type": "application/xml"}, b"<ok/>")
    got = world.fetcher().fetch_bytes("http://podcast.example/a", kind="feed", max_bytes=100)
    assert got.body == b"<ok/>"
    assert got.url == "http://cdn.example/c"
    assert [d[0] for d in world.dialled] == ["podcast.example", "cdn.example", "cdn.example"]


def test_a_redirect_loop_ends_after_five_hops(world, server):
    server.routes["/loop"] = (302, {"Location": "/loop"}, b"")
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/loop", kind="feed", max_bytes=100)
    assert error.value.kind == "bad_response"
    assert len(server.seen) == safefetch.MAX_REDIRECTS + 1


def test_a_redirect_without_a_destination_is_an_error(world, server):
    server.routes["/x"] = (302, {}, b"")
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=100)
    assert error.value.kind == "bad_response"


@pytest.mark.parametrize(
    ("status", "kind"),
    [(404, "not_found"), (410, "not_found"), (429, "temporary"), (500, "temporary"), (503, "temporary"),
     (401, "bad_response"), (403, "bad_response"), (204, "bad_response"), (418, "bad_response")],
)  # fmt: skip
def test_http_errors_become_kinds(world, server, status, kind):
    server.routes["/x"] = (status, {"Content-Type": "text/plain"}, b"")
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=100)
    assert error.value.kind == kind


def test_an_unreachable_host_is_a_network_error(world):
    def refuse(target, ip):
        raise ConnectionRefusedError

    fetcher = SafeFetcher(resolver=world.resolve, dialer=refuse)
    with pytest.raises(FetchError) as error:
        fetcher.fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=100)
    assert error.value.kind == "network"
    assert "podcast.example" in error.value.message


def test_the_next_address_is_tried_when_the_first_does_not_connect(world, server):
    world.names["podcast.example"] = [PUBLIC, "151.101.1.69"]
    server.routes["/x"] = (200, {"Content-Type": "text/xml"}, b"ok")
    tried = []

    def dial(target, ip):
        tried.append(ip)
        if ip == PUBLIC:
            raise TimeoutError
        return socket.create_connection(("127.0.0.1", server.port))

    fetcher = SafeFetcher(resolver=world.resolve, dialer=dial)
    assert fetcher.fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=10).body == b"ok"
    assert tried == [PUBLIC, "151.101.1.69"]


def test_a_wrong_content_type_is_refused(world, server):
    server.routes["/x"] = (200, {"Content-Type": "text/html"}, b"<html>login</html>")
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=1000)
    assert error.value.kind == "bad_response"


def test_a_compressed_answer_is_refused(world, server):
    server.routes["/x"] = (
        200,
        {"Content-Type": "text/xml", "Content-Encoding": "gzip"},
        b"\x1f\x8b",
    )
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=1000)
    assert error.value.kind == "bad_response"


def test_the_request_asks_for_no_compression_and_closes(world, server):
    server.routes["/x"] = (200, {"Content-Type": "text/xml"}, b"ok")
    world.fetcher().fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=10)
    headers = server.request_headers[0]
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Connection"] == "close"
    assert headers["User-Agent"].startswith("youtube-premium/")


@pytest.mark.security
def test_a_declared_size_over_the_limit_is_refused_before_reading(world, server):
    server.routes["/big"] = (200, {"Content-Type": "text/xml"}, b"x" * 5000)
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/big", kind="feed", max_bytes=1000)
    assert error.value.kind == "too_large"


@pytest.mark.security
def test_a_chunked_answer_without_a_size_is_cut_at_the_limit(world, server):
    server.routes["/big"] = (200, {"Content-Type": "text/xml"}, b"x" * 5000)
    server.chunked.add("/big")
    with pytest.raises(FetchError) as error:
        world.fetcher().fetch_bytes("http://podcast.example/big", kind="feed", max_bytes=1000)
    assert error.value.kind == "too_large"


# --- download to a file ---------------------------------------------------------------------------


def test_a_download_is_streamed_into_the_file(world, server, tmp_path):
    body = b"ID3\x04\x00" + bytes(range(256)) * 1000
    server.routes["/e.mp3"] = (200, {"Content-Type": "audio/mpeg"}, body)
    target = tmp_path / "e.mp3"
    seen = []
    got = world.fetcher().download(
        "https://podcast.example/e.mp3", target, kind="media", max_bytes=10**7,
        progress=lambda done, total: seen.append((done, total)),
    )  # fmt: skip
    assert target.read_bytes() == body
    assert got.size == len(body)
    assert seen[-1] == (len(body), len(body))


@pytest.mark.security
def test_a_download_over_the_limit_leaves_no_file(world, server, tmp_path):
    server.routes["/e.mp3"] = (200, {"Content-Type": "audio/mpeg"}, b"ID3" + b"x" * 5000)
    server.chunked.add("/e.mp3")
    target = tmp_path / "e.mp3"
    with pytest.raises(FetchError) as error:
        world.fetcher().download(
            "http://podcast.example/e.mp3", target, kind="media", max_bytes=2000
        )
    assert error.value.kind == "too_large"
    assert not target.exists()


@pytest.mark.security
@pytest.mark.parametrize(
    "page",
    [b"<!doctype html><html>sign in</html>", b'{"error":"gone"}', b"  <?xml version='1.0'?>"],
)
def test_a_web_page_behind_an_audio_address_is_not_filed(world, server, tmp_path, page):
    server.routes["/e.mp3"] = (200, {"Content-Type": "audio/mpeg"}, page)
    target = tmp_path / "e.mp3"
    with pytest.raises(FetchError) as error:
        world.fetcher().download(
            "http://podcast.example/e.mp3", target, kind="media", max_bytes=10**6
        )
    assert error.value.kind == "bad_response"
    assert not target.exists()


def test_an_empty_download_is_an_error(world, server, tmp_path):
    server.routes["/e.mp3"] = (200, {"Content-Type": "audio/mpeg"}, b"")
    with pytest.raises(FetchError) as error:
        world.fetcher().download(
            "http://podcast.example/e.mp3", tmp_path / "e.mp3", kind="media", max_bytes=10
        )
    assert error.value.kind == "bad_response"
    assert not (tmp_path / "e.mp3").exists()


def test_a_download_that_takes_too_long_is_stopped(world, server, tmp_path):
    server.routes["/e.mp3"] = (200, {"Content-Type": "audio/mpeg"}, b"ID3" + b"x" * 200_000)
    ticks = iter(range(0, 10_000, 100))
    fetcher = world.fetcher(clock=lambda: next(ticks))
    target = tmp_path / "e.mp3"
    with pytest.raises(FetchError) as error:
        fetcher.download(
            "http://podcast.example/e.mp3",
            target,
            kind="media",
            max_bytes=10**7,
            deadline_seconds=150,
        )
    assert error.value.kind == "network"
    assert "too long" in error.value.message
    assert not target.exists()


def test_a_download_cut_off_early_is_an_error(tmp_path):
    """The server promises 1000 bytes and sends 100: a half file must not be kept."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 1000\r\n\r\nID3"
            + b"x" * 97
        )
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    fetcher = SafeFetcher(
        resolver=lambda h, p: [PUBLIC],
        dialer=lambda target, ip: socket.create_connection(("127.0.0.1", port)),
    )
    target = tmp_path / "e.mp3"
    with pytest.raises(FetchError) as error:
        fetcher.download("http://podcast.example/e.mp3", target, kind="media", max_bytes=10**6)
    listener.close()
    assert error.value.kind == "network"
    assert not target.exists()


# --- the proxy ------------------------------------------------------------------------------------


class FakeProxy:
    """Records what an HTTP proxy is asked; answers CONNECT with `connect_answer`, GET by serving."""

    def __init__(self, connect_answer=b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self.requests: list[str] = []
        self.connect_answer = connect_answer
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.requests.append(data.decode("latin-1"))
            if data.startswith(b"CONNECT"):
                conn.sendall(self.connect_answer)
            else:
                body = b"<rss/>"
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\n"
                    b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
                )
            conn.close()

    def close(self):
        self.listener.close()


@pytest.fixture
def proxy() -> Iterator[FakeProxy]:
    fake = FakeProxy()
    yield fake
    fake.close()


@pytest.mark.security
def test_plain_http_through_the_proxy_names_the_ip_not_the_host(proxy):
    fetcher = SafeFetcher(f"http://127.0.0.1:{proxy.port}", resolver=lambda h, p: [PUBLIC])
    got = fetcher.fetch_bytes("http://podcast.example/feed.xml?x=1", kind="feed", max_bytes=100)
    assert got.body == b"<rss/>"
    request = proxy.requests[0]
    assert request.startswith(f"GET http://{PUBLIC}:80/feed.xml?x=1 HTTP/1.1")
    assert "Host: podcast.example" in request


@pytest.mark.security
def test_https_through_the_proxy_tunnels_to_the_ip_not_the_host(proxy):
    # The tunnel is set up with the IP; the fake proxy then closes, so the TLS step fails,
    # which is a network error. What matters here is what the proxy was asked to connect to.
    fetcher = SafeFetcher(f"http://127.0.0.1:{proxy.port}", resolver=lambda h, p: [PUBLIC])
    with pytest.raises(FetchError) as error:
        fetcher.fetch_bytes("https://podcast.example/feed.xml", kind="feed", max_bytes=100)
    assert error.value.kind == "network"
    request = proxy.requests[0]
    assert request.startswith(f"CONNECT {PUBLIC}:443 HTTP/1.1")
    assert "podcast.example" not in request


@pytest.mark.security
def test_an_ipv6_address_is_bracketed_for_the_proxy(proxy):
    safefetch._connect_through_proxy("127.0.0.1", proxy.port, "2606:4700:4700::1111", 443).close()
    assert proxy.requests[0].startswith("CONNECT [2606:4700:4700::1111]:443 HTTP/1.1")


def test_a_proxy_that_refuses_the_tunnel_is_a_network_error():
    refusing = FakeProxy(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    try:
        fetcher = SafeFetcher(f"http://127.0.0.1:{refusing.port}", resolver=lambda h, p: [PUBLIC])
        with pytest.raises(FetchError) as error:
            fetcher.fetch_bytes("https://podcast.example/x", kind="feed", max_bytes=100)
        assert error.value.kind == "network"
    finally:
        refusing.close()


def test_a_dead_proxy_is_a_network_error():
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    fetcher = SafeFetcher(f"http://127.0.0.1:{port}", resolver=lambda h, p: [PUBLIC])
    with pytest.raises(FetchError) as error:
        fetcher.fetch_bytes("https://podcast.example/x", kind="feed", max_bytes=100)
    assert error.value.kind == "network"


@pytest.mark.security
def test_the_proxy_is_not_asked_when_the_name_is_private():
    proxy = FakeProxy()
    try:
        fetcher = SafeFetcher(
            f"http://127.0.0.1:{proxy.port}", resolver=lambda h, p: ["192.168.178.1"]
        )
        with pytest.raises(FetchError) as error:
            fetcher.fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=100)
        assert error.value.kind == "blocked"
        assert proxy.requests == []
    finally:
        proxy.close()


def test_the_fetcher_does_not_show_its_proxy_address():
    assert "gluetun" not in repr(SafeFetcher("http://gluetun:8888"))


def test_destination_is_never_overwritten(world, server, tmp_path):
    server.routes["/e.mp3"] = (200, {"Content-Type": "audio/mpeg"}, b"ID3xx")
    target = Path(tmp_path / "e.mp3")
    target.write_bytes(b"mine")
    with pytest.raises(FileExistsError):
        world.fetcher().download(
            "http://podcast.example/e.mp3", target, kind="media", max_bytes=100
        )
    assert target.read_bytes() == b"mine"


@pytest.mark.security
def test_headers_that_trickle_in_are_cut_off_by_the_deadline(monkeypatch):
    """A server that sends its headers a byte at a time must not hold the thread (slow loris)."""
    monkeypatch.setattr(safefetch, "HEADER_DEADLINE", 0.3)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    done = threading.Event()

    def serve():
        conn, _ = listener.accept()
        conn.recv(4096)
        try:
            for byte in b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nX-Slow: " + b"a" * 200:
                conn.sendall(bytes([byte]))
                if done.wait(0.1):
                    break
        except OSError:
            pass
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    fetcher = SafeFetcher(
        resolver=lambda h, p: [PUBLIC],
        dialer=lambda target, ip: socket.create_connection(("127.0.0.1", port)),
    )
    started = time.monotonic()
    with pytest.raises(FetchError) as error:
        fetcher.fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=100)
    done.set()
    listener.close()
    assert error.value.kind == "network"
    assert time.monotonic() - started < 5


@pytest.mark.security
def test_a_body_that_trickles_in_is_cut_off_by_the_deadline(world, server):
    server.routes["/x"] = (200, {"Content-Type": "text/xml"}, b"x" * 300_000)
    ticks = iter(range(0, 10_000, 100))
    fetcher = world.fetcher(clock=lambda: next(ticks))
    with pytest.raises(FetchError) as error:
        fetcher.fetch_bytes(
            "http://podcast.example/x", kind="feed", max_bytes=10**6, deadline_seconds=150
        )
    assert error.value.kind == "network"
    assert "too slowly" in error.value.message


# --- the home network's own global addresses (DS-Lite: native global IPv6 on the LAN) ----------

HOME_V6 = ipaddress.ip_network("2003:abcd:1200::/56")


@pytest.mark.security
@pytest.mark.parametrize(
    "address",
    ["2003:abcd:1200::1", "2003:abcd:1200:ff:ffff::5", "2003:abcd:1200:12:9abc:def0:1:2"],
)
def test_addresses_inside_a_blocked_network_are_refused(address):
    assert is_public_address(address)  # global, so public by the general rule
    assert not is_public_address(address, (HOME_V6,))


@pytest.mark.parametrize(
    "address", ["2003:abcd:1300::1", "2003:abce:1200::1", "2606:4700:4700::1111"]
)
def test_addresses_outside_the_blocked_networks_stay_allowed(address):
    assert is_public_address(address, (HOME_V6,))


def test_a_blocked_ipv4_address_is_refused_and_networks_of_the_other_family_do_not_interfere():
    home_v4 = ipaddress.ip_network("93.184.216.34/32")
    assert not is_public_address("93.184.216.34", (home_v4, HOME_V6))
    assert is_public_address("93.184.216.35", (home_v4, HOME_V6))
    assert is_public_address("2606:4700:4700::1111", (home_v4,))


@pytest.mark.security
def test_a_name_that_resolves_into_the_home_prefix_is_refused():
    target = check_url("http://podcast.example/x")
    with pytest.raises(FetchError) as error:
        resolve_public(target, resolver_returning("2003:abcd:1200::7"), (HOME_V6,))
    assert error.value.kind == "blocked"
    assert "2003" not in error.value.message


@pytest.mark.security
def test_an_ipv6_literal_in_the_home_prefix_is_refused():
    target = check_url("http://[2003:abcd:1200::1]/feed")
    with pytest.raises(FetchError) as error:
        resolve_public(target, resolver_returning(), (HOME_V6,))
    assert error.value.kind == "blocked"


def proc_line(address, prefix, scope="00", name="eth0"):
    digits = ipaddress.IPv6Address(address).exploded.replace(":", "")
    return f"{digits} 02 {prefix:02x} {scope} 00 {name}"


IF_INET6 = "\n".join(
    [
        proc_line("2003:abcd:1200::1a", 64),
        proc_line("2003:abcd:1200::1b", 64),  # the same network twice: once
        proc_line("fe80::a00:27ff:fe12:3456", 64, "20"),  # link-local
        proc_line("fd12:3456:789a::1", 64, "00"),  # unique local
        proc_line("::1", 128, "10", "lo"),
        proc_line("2003:abcd:5555:5555::5555", 128, "00", "eth1"),
        proc_line("2a02:aaaa:bb00::cc01", 56, "00", "wlan0"),
        proc_line("ff02::1", 128),  # multicast
        "this line is broken",
        "zz03abcd120000000000000000000001 02 40 00 00 bad",
    ]
)


def test_the_hosts_own_global_networks_are_read_from_proc(tmp_path):
    path = tmp_path / "if_inet6"
    path.write_text(IF_INET6)
    got = safefetch.local_networks(path)
    assert got == (
        ipaddress.ip_network("2003:abcd:1200::/64"),
        ipaddress.ip_network("2003:abcd:5555:5555::/64"),  # a /128 address says little: /64
        ipaddress.ip_network("2a02:aaaa:bb00::/56"),  # a wider prefix than /64 is kept
    )


@pytest.mark.parametrize("content", ["", "\n\n", "garbage"])
def test_an_empty_or_odd_proc_file_means_no_networks(tmp_path, content):
    path = tmp_path / "if_inet6"
    path.write_text(content)
    assert safefetch.local_networks(path) == ()


def test_no_proc_file_means_no_networks(tmp_path):
    assert safefetch.local_networks(tmp_path / "missing") == ()


def test_the_real_proc_file_can_be_read_here():
    """Whatever this machine has, the result is a tuple of global networks (maybe none)."""
    for network in safefetch.local_networks():
        assert network.version == 6
        assert network.prefixlen <= 64
        assert network.network_address.is_global


@pytest.mark.security
def test_the_fetcher_refuses_configured_and_own_networks_without_connecting():
    dialled = []

    def dial(target, ip):
        dialled.append(ip)
        raise AssertionError("must not connect")

    own = ipaddress.ip_network("2a02:aaaa:bb00::/56")
    fetcher = SafeFetcher(
        resolver=lambda host, port: {
            "home.example": ["2003:abcd:1200::5"],
            "vm.example": ["2a02:aaaa:bb00::9"],
            "fine.example": [PUBLIC],
        }[host],
        dialer=dial,
        blocked_networks=(HOME_V6,),
        own_networks=lambda: (own,),
    )
    for host in ("home.example", "vm.example"):
        with pytest.raises(FetchError) as error:
            fetcher.fetch_bytes(f"http://{host}/feed", kind="feed", max_bytes=100)
        assert error.value.kind == "blocked", host
    assert dialled == []


def test_the_hosts_own_networks_are_re_read_after_a_while():
    """A DS-Lite provider can renumber: a new prefix is noticed without a restart."""
    answers = iter([(), (ipaddress.ip_network("2003:abcd:9900::/64"),)])
    now = [0.0]
    fetcher = SafeFetcher(
        resolver=lambda h, p: ["2003:abcd:9900::5"],
        dialer=lambda target, ip: (_ for _ in ()).throw(ConnectionRefusedError()),
        own_networks=lambda: next(answers),
        clock=lambda: now[0],
    )
    with pytest.raises(FetchError) as before:
        fetcher.fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=10)
    assert before.value.kind == "network"  # allowed then: it got as far as connecting
    now[0] = 10.0
    with pytest.raises(FetchError) as soon:
        fetcher.fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=10)
    assert soon.value.kind == "network"  # still the cached (empty) answer
    now[0] = safefetch.LOCAL_REFRESH_SECONDS + 1
    with pytest.raises(FetchError) as later:
        fetcher.fetch_bytes("http://podcast.example/x", kind="feed", max_bytes=10)
    assert later.value.kind == "blocked"
