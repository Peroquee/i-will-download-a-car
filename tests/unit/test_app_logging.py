"""app/ytp/logging_setup.py: one JSON object per line, secrets masked."""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest

from ytp.logging_setup import DropHealthChecks, JsonFormatter, configure_logging, redact


def record(msg="hello", level=logging.INFO, **extra) -> logging.LogRecord:
    rec = logging.LogRecord("ytp.test", level, __file__, 1, msg, None, None)
    rec.__dict__.update(extra)
    return rec


def line(**kwargs) -> dict:
    return json.loads(JsonFormatter().format(record(**kwargs)))


def test_a_line_is_json_with_time_level_logger_and_message():
    out = line(msg="job %s done", level=logging.WARNING)
    assert out["level"] == "WARNING"
    assert out["logger"] == "ytp.test"
    assert out["msg"] == "job %s done"
    assert out["ts"].endswith("+00:00")


def test_extra_fields_are_part_of_the_json_and_umlauts_stay_readable():
    text = JsonFormatter().format(record(folder="Hörbücher", job_id=41))
    assert "Hörbücher" in text
    assert json.loads(text)["job_id"] == 41


def test_unserialisable_values_degrade_to_strings_instead_of_breaking_the_call():
    assert line(path=Path("/data"))["path"] == "/data"


def test_exceptions_are_included():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(rec))["exc"]


@pytest.mark.security
@pytest.mark.parametrize(
    "name",
    ["api_key", "jellyfin_api_key", "password", "token", "Authorization", "secret", "cookie"],
)
def test_secret_looking_fields_are_masked(name):
    assert line(**{name: "top-secret-value"})[name] == "***"
    assert redact(name, "x") == "***"


def test_harmless_fields_are_not_masked():
    assert redact("folder", "Filme") == "Filme"
    assert (
        redact("keyboard_layout", "de") == "***"
    )  # a known false positive: better safe than sorry


def test_uvicorn_colour_copies_are_left_out():
    assert "color_message" not in line(color_message="\x1b[1mx\x1b[0m")


def test_health_checks_are_dropped_from_the_access_log_but_other_requests_are_not():
    flt = DropHealthChecks()
    health = record('127.0.0.1:1 - "GET /health HTTP/1.1" 200')
    other = record('127.0.0.1:1 - "GET /jobs HTTP/1.1" 200')
    assert not flt.filter(health)
    assert flt.filter(other)


def test_configure_logging_writes_json_to_stdout_and_is_safe_to_repeat(monkeypatch):
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging("DEBUG")
        configure_logging("INFO")
        assert len(root.handlers) == 1
        assert root.level == logging.INFO
        logging.getLogger("ytp.x").info("started", extra={"api_key": "abc"})
        out = json.loads(buffer.getvalue().strip())
        assert out["msg"] == "started"
        assert out["api_key"] == "***"
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
