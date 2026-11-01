"""A fake gluetun control server: GET /v1/vpn/status behind an X-API-Key."""

from __future__ import annotations

import http.server
import json
import threading
from dataclasses import dataclass, field

API_KEY = "watchdog-test-key-0123456789"  # a test value, not a secret


@dataclass
class FakeGluetun:
    status: str = "running"
    http_status: int = 200  # forced answer for the status route, e.g. 500
    body: str | None = None  # raw body override
    hits: list[dict] = field(default_factory=list)
    public: dict | None = field(
        default_factory=lambda: {
            "public_ip": "185.107.12.34",
            "country": "Germany",
            "region": "Hesse",
            "city": "Frankfurt",
            "organization": "AS64500 Example Hosting",
        }
    )
    public_http_status: int = 200

    def __post_init__(self) -> None:
        self.server: http.server.ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.server_address[1]

    def start(self, port: int = 0) -> FakeGluetun:
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

            def do_GET(self):
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.hits.append({"path": self.path, "headers": headers})
                if self.path not in {"/v1/vpn/status", "/v1/publicip/ip"}:
                    return self.reply(404)
                if headers.get("x-api-key") != API_KEY:
                    return self.reply(401, "unauthorized")
                if self.path == "/v1/publicip/ip":
                    if fake.public_http_status != 200:
                        return self.reply(fake.public_http_status, "boom")
                    return self.reply(
                        200, json.dumps(fake.public if fake.public is not None else {})
                    )
                if fake.http_status != 200:
                    return self.reply(fake.http_status, "boom")
                self.reply(
                    200, fake.body if fake.body is not None else json.dumps({"status": fake.status})
                )

            def do_PUT(self):
                self.reply(405)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
