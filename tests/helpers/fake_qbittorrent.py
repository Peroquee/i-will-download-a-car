"""A small fake qBittorrent Web API on localhost, with the quirks learned on the real VM.

Used by tests of the client in app/ytp/clients/qbittorrent.py. Records every request so
tests can assert what was sent (and that a password never appears where it must not).
"""

from __future__ import annotations

import http.server
import json
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

USER = "tester"
PASSWORD = "a long test password"  # a test value, not a secret


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, list[str]]
    form: dict[str, list[str]]
    headers: dict[str, str]


@dataclass
class FakeQbittorrent:
    # modern: 204 / 401 (current qBittorrent); legacy: 200 "Ok." / 200 "Fails."; banned: 403;
    # weird: 200 with an HTML body; nocookie: 204 but no Set-Cookie.
    login_style: str = "modern"
    # "5": torrents/stop + start exist, pause/resume answer 404. "4": the reverse. "none": neither.
    api_generation: str = "5"
    # Reject (401) a Host whose port differs from the real one, like qBittorrent does.
    check_host: bool = True
    torrents: list[dict] = field(default_factory=list)
    add_result: str = "Ok."
    preferences: dict = field(default_factory=lambda: {"current_network_interface": "tun0"})
    list_status: int = 200
    fail_status: int | None = None  # answer every authenticated call with this status
    reject_sessions: bool = False  # even a fresh login does not help: every API call is 403
    delete_status: int = 200  # torrents/delete answers this (anything else: nothing is removed)
    bad_filters: set[str] = field(default_factory=set)  # torrents/info?filter=<these> -> 400
    seen: list[Seen] = field(default_factory=list)
    sid: str = "SID-0001"
    # qBittorrent 5.2+ names the cookie QBT_SID_<port>; older versions SID. None = the 5.2 name.
    cookie_name: str | None = None
    logins: int = 0
    _generation: int = 1

    def __post_init__(self) -> None:
        self.server: http.server.ThreadingHTTPServer | None = None

    # -- control -------------------------------------------------------------------
    def expire_session(self) -> None:
        """The cookie the client holds stops working (restart, timeout): calls answer 403."""
        self._generation += 1
        self.sid = f"SID-{self._generation:04d}"
        self._valid_sids = set()

    _valid_sids: set = field(default_factory=set)

    @property
    def cookie(self) -> str:
        """The Cookie header value a client must send for the current session."""
        return f"{self.cookie_name or f'QBT_SID_{self.port}'}={self.sid}"

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.server_address[1]

    def calls(self, path: str) -> list[Seen]:
        return [s for s in self.seen if s.path == path]

    # -- server --------------------------------------------------------------------
    def start(self, port: int = 0) -> FakeQbittorrent:
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code: int, body: str = "", headers: dict[str, str] | None = None):
                data = body.encode()
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method: str):
                parts = urlsplit(self.path)
                size = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(size).decode()) if size else {}
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.seen.append(
                    Seen(
                        method,
                        parts.path.removeprefix("/api/v2/"),
                        parse_qs(parts.query),
                        form,
                        headers,
                    )
                )
                path = parts.path.removeprefix("/api/v2/")
                host = headers.get("host", "")
                if fake.check_host and host.rpartition(":")[2] != str(fake.port):
                    return self.reply(401, "Unauthorized")
                for key in ("origin", "referer"):
                    if key in headers and urlsplit(headers[key]).netloc != host:
                        return self.reply(401, "Unauthorized")
                if path == "auth/login":
                    return self.login(form)
                cookie = headers.get("cookie", "")
                if (
                    fake.reject_sessions
                    or cookie != fake.cookie
                    or fake.sid not in fake._valid_sids
                ):
                    return self.reply(403, "Forbidden")
                if fake.fail_status is not None:
                    return self.reply(fake.fail_status, "failure")
                self.api(method, path, parse_qs(parts.query), form)

            def login(self, form):
                fake.logins += 1
                if fake.login_style == "banned":
                    return self.reply(403, "Your IP address has been banned")
                if fake.login_style == "weird":
                    return self.reply(200, "<html>captive portal</html>")
                if fake.login_style == "nocookie":
                    return self.reply(204)
                good = form.get("username") == [USER] and form.get("password") == [PASSWORD]
                if fake.login_style == "legacy":
                    if not good:
                        return self.reply(200, "Fails.")
                    fake._valid_sids.add(fake.sid)
                    return self.reply(
                        200, "Ok.", {"Set-Cookie": f"{fake.cookie}; HttpOnly; path=/"}
                    )
                if not good:
                    return self.reply(401, "Unauthorized")
                fake._valid_sids.add(fake.sid)
                self.reply(
                    204, "", {"Set-Cookie": f"{fake.cookie}; HttpOnly; SameSite=Lax; path=/"}
                )

            def api(self, method, path, query, form):
                if path == "app/version":
                    return self.reply(200, "v5.0.4")
                if path == "app/preferences":
                    return self.reply(200, json.dumps(fake.preferences))
                if path == "torrents/add":
                    return self.reply(200, fake.add_result)
                if path == "torrents/info":
                    if fake.list_status != 200:
                        return self.reply(fake.list_status, "bad")
                    items = fake.torrents
                    if "category" in query:
                        items = [t for t in items if t.get("category") == query["category"][0]]
                    if "hashes" in query:
                        wanted = query["hashes"][0].split("|")
                        items = [t for t in items if t["hash"] in wanted]
                    if "filter" in query and query["filter"][0] in fake.bad_filters:
                        return self.reply(400, "Bad Request")
                    if "filter" in query:
                        items = [t for t in items if query["filter"][0] in t.get("_filters", [])]
                    return self.reply(200, json.dumps(items), {"Content-Type": "application/json"})
                if path == "torrents/delete":
                    if fake.delete_status != 200:
                        return self.reply(fake.delete_status, "bad")
                    gone = form.get("hashes", [""])[0].split("|")
                    fake.torrents = [t for t in fake.torrents if t["hash"] not in gone]
                    return self.reply(200, "")
                names_5, names_4 = {"stop", "start"}, {"pause", "resume"}
                leaf = path.removeprefix("torrents/")
                if path.startswith("torrents/") and leaf in names_5 | names_4:
                    known = {"5": names_5, "4": names_4}.get(fake.api_generation, set())
                    return self.reply(
                        200 if leaf in known else 404, "" if leaf in known else "Not Found"
                    )
                self.reply(404, "Not Found")

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
