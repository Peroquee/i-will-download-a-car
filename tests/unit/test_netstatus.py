"""The dashboard's independent internet and watchdog signals (issue #63)."""

from __future__ import annotations

import socket
from datetime import UTC, datetime

import pytest

from tests.helpers.app import ENV
from tests.helpers.net import free_port
from ytp.netstatus import (
    WATCHDOG_STALE_SECONDS,
    InternetChecker,
    watchdog_view,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    try:
        yield server
    finally:
        server.close()


# --- the independent internet check ------------------------------------------------


def test_the_default_host_is_one_the_app_contacts_anyway():
    from ytp import netstatus

    assert netstatus.DEFAULT_HOST == "www.youtube.com"
    assert netstatus.DEFAULT_PORT == 443
    assert "pypi" not in netstatus.DEFAULT_HOST


def test_before_the_first_check_the_answer_is_unknown():
    checker = InternetChecker(host="127.0.0.1", port=free_port(), timeout=0.2)
    assert checker.status() == {"state": "unknown", "checked_at": None}


def test_a_reachable_endpoint_is_ok_and_gets_a_timestamp(tcp_server):
    checker = InternetChecker(
        host="127.0.0.1",
        port=tcp_server.getsockname()[1],
        timeout=0.5,
        wall_clock=lambda: NOW,
    )
    assert checker.refresh() == {"state": "ok", "checked_at": "2026-11-07T12:00:00+00:00"}
    assert checker.status() == {"state": "ok", "checked_at": "2026-11-07T12:00:00+00:00"}


def test_a_refused_connection_is_offline():
    checker = InternetChecker(host="127.0.0.1", port=free_port(), timeout=0.2)
    assert checker.refresh()["state"] == "offline"


def test_the_answer_is_cached_for_the_interval(tcp_server):
    ticks = [100.0]
    checker = InternetChecker(
        host="127.0.0.1",
        port=tcp_server.getsockname()[1],
        timeout=0.5,
        interval=30.0,
        clock=lambda: ticks[0],
    )
    assert checker.refresh()["state"] == "ok"
    tcp_server.close()
    ticks[0] += 29
    assert checker.refresh()["state"] == "ok", "a cached answer does not probe again"
    ticks[0] += 2
    assert checker.refresh()["state"] == "offline"


def test_an_unexpected_probe_error_is_unknown_not_a_crash():
    class Broken(InternetChecker):
        def _probe(self) -> str:
            raise RuntimeError("boom")

    checker = Broken(host="127.0.0.1", port=1, timeout=0.1)
    assert checker.refresh()["state"] == "unknown"


# --- the watchdog's own state -----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "unreachable"),
        ({}, "unreachable"),
        ({"state": "unknown"}, "unreachable"),
        ({"state": "starting"}, "starting"),
        ({"state": "blind"}, "blind"),
        ({"state": "ok", "last_poll": "2026-11-07T11:59:00+00:00"}, "ok"),
        ({"state": "down", "last_poll": "2026-11-07T11:59:00+00:00"}, "ok"),
        ({"state": "ok", "last_poll": "2026-11-07T11:00:00Z"}, "stale"),
        ({"state": "ok"}, "stale"),
        ({"state": "ok", "last_poll": "not a time"}, "stale"),
    ],
)
def test_watchdog_view(status, expected):
    assert watchdog_view(status, now=NOW)["state"] == expected


def test_the_stale_boundary_is_exact():
    fresh = {"state": "ok", "last_poll": "2026-11-07T11:55:00+00:00"}
    old = {"state": "ok", "last_poll": "2026-11-07T11:54:59+00:00"}
    assert WATCHDOG_STALE_SECONDS == 300
    assert watchdog_view(fresh, now=NOW)["state"] == "ok"
    assert watchdog_view(old, now=NOW)["state"] == "stale"


def test_watchdog_view_keeps_last_poll_and_understands_offsets():
    status = {"state": "ok", "last_poll": "2026-11-07T13:00:00+02:00"}  # 11:00 UTC
    assert watchdog_view(status, now=NOW) == {
        "state": "stale",
        "last_poll": "2026-11-07T13:00:00+02:00",
    }


def test_a_malformed_status_is_unreachable_never_ok():
    assert watchdog_view({"state": "exploding"}, now=NOW)["state"] == "unreachable"
    assert watchdog_view("nope", now=NOW)["state"] == "unreachable"  # type: ignore[arg-type]


def test_the_configured_host_and_port_are_the_ones_connected_to(monkeypatch):
    seen = []

    def connect(address, timeout=None):
        seen.append(address)
        raise OSError("no route")

    monkeypatch.setattr(socket, "create_connection", connect)
    checker = InternetChecker("example.org", 8443)
    assert checker.refresh()["state"] == "offline"
    assert seen == [("example.org", 8443)]


def test_without_a_host_the_check_is_off_and_never_connects(monkeypatch):
    def connect(*args, **kwargs):
        raise AssertionError("an empty host must not open a connection")

    monkeypatch.setattr(socket, "create_connection", connect)
    checker = InternetChecker("", 443, wall_clock=lambda: NOW)
    assert checker.enabled is False
    expected = {"state": "unknown", "checked_at": None, "reason": "disabled"}
    assert checker.status() == expected
    assert checker.refresh() == expected  # no "checked" time, and certainly never "ok"
    assert checker.refresh() == expected


def test_a_working_checker_has_no_reason_and_is_enabled(tcp_server):
    checker = InternetChecker("127.0.0.1", tcp_server.getsockname()[1], timeout=0.5)
    assert checker.enabled is True
    assert "reason" not in checker.status()
    assert "reason" not in checker.refresh()


def test_an_unexpected_error_is_unknown_with_a_reason_and_logs_only_the_host(monkeypatch, caplog):
    def explode(address, timeout=None):
        raise RuntimeError("secret detail 203.0.113.9")

    monkeypatch.setattr(socket, "create_connection", explode)
    checker = InternetChecker("example.org", 8443)
    with caplog.at_level("WARNING"):
        answer = checker.refresh()
    assert answer["state"] == "unknown"
    assert answer["reason"] == "error"
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "example.org" in text
    assert "203.0.113.9" not in text
    assert all("8443" not in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)


def test_the_app_builds_its_checker_from_the_settings(tmp_path):
    from ytp.config import Settings
    from ytp.main import create_app

    env = {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(tmp_path)}
    default = create_app(Settings.from_env(env)).state.internet
    assert (default._host, default._port) == ("www.youtube.com", 443)
    custom = create_app(
        Settings.from_env(
            {**env, "INTERNET_CHECK_HOST": "example.org", "INTERNET_CHECK_PORT": "80"}
        )
    ).state.internet
    assert (custom._host, custom._port) == ("example.org", 80)
    off = create_app(Settings.from_env({**env, "INTERNET_CHECK_HOST": ""})).state.internet
    assert off.enabled is False
