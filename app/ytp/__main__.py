"""Entry point: `python -m ytp` (what the container runs)."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .config import ConfigError, Settings
from .logging_setup import configure_logging
from .main import create_app


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        configure_logging("INFO")
        logging.getLogger("ytp").error("invalid configuration", extra={"problems": exc.problems})
        return 2
    # Folders the organizer creates in the library follow the stack's UMASK (002: group-writable).
    os.umask(settings.umask)
    # log_config=None: our handler (configured at startup) formats uvicorn's lines too.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
