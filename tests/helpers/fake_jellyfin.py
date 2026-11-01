"""A small fake Jellyfin API on localhost: library refresh, virtual folders, public info."""

from __future__ import annotations

import http.server
import json
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

API_KEY = "test-api-key-value"  # a test value, not a secret
MOVIES_ID = "a" * 32
SERIES_ID = "b" * 32
BOOKS_ID = "c" * 32


def default_libraries() -> list[dict]:
    return [
        {"Name": "Filme", "ItemId": MOVIES_ID, "CollectionType": "movies",
         "Locations": ["/media/jellyfin/Filme"]},
        {"Name": "Serien", "ItemId": SERIES_ID, "CollectionType": "tvshows",
         "Locations": ["/media/jellyfin/Serien"]},
        {"Name": "Hörbücher", "ItemId": BOOKS_ID, "CollectionType": "books",
         "Locations": ["/media/jellyfin/Hörbücher/", "/other/place"]},
    ]  # fmt: skip


@dataclass
class Seen:
    method: str
    path: str
    query: str
    headers: dict[str, str]


@dataclass
class FakeJellyfin:
    libraries: list[dict] = field(default_factory=default_libraries)
    status_override: int | None = None  # answer every authenticated call with this status
    seen: list[Seen] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.server: http.server.ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def calls(self, path: str) -> list[Seen]:
        return [s for s in self.seen if s.path == path]

    def start(self, port: int = 0) -> FakeJellyfin:
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code: int, body: str = ""):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method: str):
                parts = urlsplit(self.path)
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.seen.append(Seen(method, parts.path, parts.query, headers))
                if parts.path == "/System/Info/Public":
                    return self.reply(200, json.dumps({"Version": "10.10.7", "ServerName": "jf"}))
                if fake.status_override is not None:
                    return self.reply(fake.status_override)
                if headers.get("authorization") != f'MediaBrowser Token="{API_KEY}"':
                    return self.reply(401, "Unauthorized")
                if parts.path == "/Library/VirtualFolders" and method == "GET":
                    return self.reply(200, json.dumps(fake.libraries))
                if parts.path == "/Library/Refresh" and method == "POST":
                    return self.reply(204)
                if parts.path.startswith("/Items/") and parts.path.endswith("/Refresh"):
                    item = parts.path.split("/")[2]
                    known = {lib["ItemId"] for lib in fake.libraries}
                    return self.reply(204 if item in known else 404)
                self.reply(404)

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
