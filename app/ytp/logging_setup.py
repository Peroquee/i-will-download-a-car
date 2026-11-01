"""Structured logging (issue #8).

One JSON object per line on stdout, where Docker collects it. Fields passed as `extra=` end
up in the JSON. Anything that looks like a secret (key, token, password, secret) is replaced
by "***" so a careless log call cannot leak the Jellyfin key or a login.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

# Attributes every LogRecord has; anything else was passed by the caller as `extra=`.
_STANDARD_FIELDS = frozenset(
    [
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    ]
)
# uvicorn adds a terminal-colour copy of its message as extra; not wanted in JSON.
_NOISE = frozenset({"color_message"})
_SECRET_NAME = re.compile(r"key|token|passw|secret|authorization|cookie", re.IGNORECASE)
_HEALTH_ACCESS = re.compile(r'"(GET|HEAD) /health[ ?]')


def redact(name: str, value: object) -> object:
    return "***" if _SECRET_NAME.search(name) else value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and key not in _NOISE and not key.startswith("_"):
                payload[key] = redact(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # default=str: a stray Path in extra= must not break the log call that reports a problem.
        return json.dumps(payload, default=str, ensure_ascii=False)


class DropHealthChecks(logging.Filter):
    """The container health check hits /health every few seconds; keep it out of the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _HEALTH_ACCESS.search(record.getMessage())


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger. Safe to call twice."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn brings its own handlers; route its lines through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.filters.clear()
    access.addFilter(DropHealthChecks())
