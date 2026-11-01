"""A fake ntfy / webhook target on localhost that records what it receives."""

from __future__ import annotations

import http.server
import json
import threading
from dataclasses import dataclass, field


@dataclass
class FakeAlertTarget:
    status: int = 200
    fail_first: int = 0  # answer 500 this many times first
    delay: float = 0.0
    received: list[dict] = field(default_factory=list)
    attempts: int = 0

    def __post_init__(self) -> None:
        self.server: http.server.ThreadingHTTPServer | None = None
        self._event = threading.Condition()

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}/vpn-alerts-secret-topic"

    def wait_for(self, count: int, timeout: float = 5.0) -> bool:
        with self._event:
            return self._event.wait_for(lambda: len(self.received) >= count, timeout)

    def start(self) -> FakeAlertTarget:
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                import time

                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size)
                with fake._event:
                    fake.attempts += 1
                if fake.delay:
                    time.sleep(fake.delay)
                code = 500 if fake.attempts <= fake.fail_first else fake.status
                if 200 <= code < 300:
                    with fake._event:
                        fake.received.append(
                            {
                                "path": self.path,
                                "headers": {k.lower(): v for k, v in self.headers.items()},
                                "body": body,
                            }
                        )
                        fake._event.notify_all()
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def json_bodies(self) -> list[dict]:
        return [json.loads(r["body"]) for r in self.received]
