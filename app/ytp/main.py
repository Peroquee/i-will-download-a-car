"""The FastAPI application (issue #8): configuration, logging and /health.

Later issues add the database (#9), the qBittorrent and Jellyfin clients (#10, #11) and the
scheduler (#12). Everything with a lifetime is created in `create_app` / the lifespan and
hung off `app.state`, so tests build the app with explicit settings and nothing is global.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from . import __version__
from .config import Settings
from .logging_setup import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    logger.info("starting", extra={"version": __version__, **settings.summary()})
    if not settings.jellyfin_configured:
        logger.warning("jellyfin is not configured: library refreshes are disabled")
    yield
    logger.info("stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. With no argument the settings come from the environment."""
    resolved = settings if settings is not None else Settings.from_env()
    # No /docs, /redoc or /openapi.json: nothing needs them and the app is LAN-only anyway.
    app = FastAPI(
        title="youtube-premium",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved

    @app.get("/health")
    async def health(response: Response) -> dict[str, object]:
        """Is the app itself fine? Deliberately does not call qBittorrent or Jellyfin.

        Both sit behind the VPN or depend on it; a check that reached for them would go red
        every time the tunnel reconnects and say nothing about this service (the VPN has its
        own watchdog, #18/#22). A degraded app answers 503, so Docker's HEALTHCHECK sees it.
        """
        data_root_ok = resolved.data_root.is_dir()
        healthy = data_root_ok
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "checks": {"data_root": "ok" if data_root_ok else "missing"},
        }

    return app
