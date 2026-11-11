"""Small fakes several unit tests need (issue #77): VPN status, internet check and feeds."""

from __future__ import annotations

import threading
from typing import Any

from ytp.feeds import FeedClient

from .feedxml import feed


class FakeWatchdog:
    """A watchdog status the test sets (`set(...)` replaces it); `calls` counts the polls."""

    def __init__(self, status: dict[str, Any] | None = None, **extra: Any):
        self.status = {"state": "ok", **(status or {}), **extra}
        self.calls = 0

    def set(self, **status: Any) -> None:
        self.status = status

    def fetch_status(self) -> dict[str, Any]:
        self.calls += 1
        return dict(self.status)


class FakeInternet:
    """The internet checker's answer without a network round trip."""

    def __init__(self, state: str = "ok", checked_at: str = "2026-11-07T12:00:00Z"):
        self.state = state
        self.checked_at = checked_at

    def refresh(self) -> dict[str, str]:
        return {"state": self.state, "checked_at": self.checked_at}


class FakeFeeds(FeedClient):
    """A feed client without the network.

    `answers[channel_id]` is returned (or raised, when it is an exception); without an entry,
    `answer` is used, and otherwise a feed for that channel is generated. `fetched` records every
    poll; `gate` (an Event) makes a poll wait, for the overlap tests."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, bytes | Exception] = {}
        self.answer: bytes | Exception | None = None
        self.fetched: list[str] = []
        self.gate: threading.Event | None = None

    def fetch(self, channel_id: str) -> bytes:
        if self.gate is not None:
            self.gate.wait(5)
        self.fetched.append(channel_id)
        answer = self.answers.get(channel_id)
        if answer is None:
            answer = self.answer if self.answer is not None else feed(channel=channel_id)
        if isinstance(answer, Exception):
            raise answer
        return answer
