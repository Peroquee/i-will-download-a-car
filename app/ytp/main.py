"""The FastAPI application (issue #8): configuration, logging and /health.

The database (#9) is opened at startup; later issues add the qBittorrent and Jellyfin clients
(#10, #11) and the scheduler (#12). Everything with a lifetime is created in `create_app` / the
lifespan and hung off `app.state`, so tests build the app with explicit settings and nothing
is global.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from . import __version__, api
from .channels import ChannelResolver
from .clients.jellyfin import JellyfinClient, LibraryRefresher
from .clients.qbittorrent import QbitClient
from .clients.watchdog import WatchdogStatusClient
from .config import Settings
from .db import Database, DatabaseError
from .feeds import FeedClient
from .jobs_api import TorrentProgress
from .logging_setup import configure_logging
from .organizer import FileOrganizer
from .podcast_jobs import POLL_JOB_ID as PODCAST_POLL_JOB_ID
from .podcast_jobs import PodcastPoller, PodcastWorker
from .podcasts import PodcastFeedClient
from .reconciler import RECONCILE_JOB_ID, RECONCILE_SECONDS, Reconciler
from .safefetch import SafeFetcher
from .scheduler import AppScheduler
from .subscription_poller import POLL_JOB_ID, POLL_JOB_SECONDS, SubscriptionPoller
from .web import SecurityMiddleware, register_ui
from .youtube import YoutubeProbe
from .youtube_download import YoutubeDownloader
from .youtube_jobs import YoutubeWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    logger.info("starting", extra={"version": __version__, **settings.summary()})
    if not settings.jellyfin_configured:
        logger.warning("jellyfin is not configured: library refreshes are disabled")
    # The qBittorrent client does not touch the network until it is used (no login at startup:
    # the VPN may be down, and a failed login counts towards qBittorrent's ban).
    app.state.qbit = QbitClient.from_settings(settings) if settings.qbit_configured else None
    app.state.torrent_progress = TorrentProgress(app.state.qbit)
    if app.state.qbit is None:
        logger.warning("qbittorrent is not configured (QBIT_PASSWORD is empty): downloads disabled")
    app.state.watchdog_status = WatchdogStatusClient(settings.watchdog_status_url)
    app.state.youtube = YoutubeProbe(
        proxy=settings.ytdlp_proxy, timeout=settings.youtube_probe_timeout
    )
    app.state.channels = ChannelResolver(app.state.youtube)
    # Jellyfin: with no URL or key the refresher does nothing (the warning above says so).
    app.state.jellyfin = LibraryRefresher(
        JellyfinClient.from_settings(settings) if settings.jellyfin_configured else None
    )
    # Hardlinks finished downloads into the library (#16); the reconciler (#15) calls it.
    app.state.organizer = FileOrganizer(settings, app.state.jellyfin)
    # A database that cannot be opened must not hide behind a crash loop: the app stays up
    # "degraded", logs the reason and the fix, and /health answers 503 until it is restarted.
    app.state.db = None
    app.state.db_error = None
    try:
        app.state.db = Database.open(settings.database_path)
        logger.info("database ready", extra={"path": str(settings.database_path)})
    except DatabaseError as exc:
        app.state.db_error = str(exc)
        logger.error("database unavailable", extra={"error": str(exc), "hint": exc.hint})
    # YouTube downloads (#18): the database is the queue, one worker thread runs it.
    app.state.youtube_worker = None
    worker_stop = threading.Event()
    if app.state.db is not None:
        app.state.youtube_worker = YoutubeWorker(
            app.state.db,
            settings,
            YoutubeDownloader(proxy=settings.ytdlp_proxy, max_height=settings.youtube_max_height),
            app.state.jellyfin,
        )
        threading.Thread(
            target=app.state.youtube_worker.run, args=(worker_stop,), name="youtube", daemon=True
        ).start()
    # Podcast episodes (#34): the same kind of queue and worker thread. Feeds and files are
    # fetched through SafeFetcher (public addresses only, through the VPN proxy).
    app.state.podcast_feeds = PodcastFeedClient(SafeFetcher(settings.podcast_proxy))
    app.state.podcast_worker = None
    if app.state.db is not None:
        app.state.podcast_worker = PodcastWorker(
            app.state.db, settings, app.state.podcast_feeds, app.state.jellyfin
        )
        threading.Thread(
            target=app.state.podcast_worker.run, args=(worker_stop,), name="podcasts", daemon=True
        ).start()
    # The scheduler needs the database (its jobs keep their state there).
    app.state.scheduler = None
    app.state.reconciler = None
    app.state.poller = None
    app.state.podcast_poller = None
    if app.state.db is not None:
        app.state.scheduler = AppScheduler(app.state.db)
        app.state.scheduler.start()
        if app.state.qbit is not None:
            # Turns finished torrents into organized files (#15); the organizer arrives with #16.
            app.state.reconciler = Reconciler(app.state.db, app.state.qbit, app.state.organizer)
            app.state.scheduler.add_interval_job(
                app.state.reconciler.run_once,
                job_id=RECONCILE_JOB_ID,
                seconds=RECONCILE_SECONDS,
                run_immediately=True,
            )
        # The poller also serves the "check now" button, so it exists even when the schedule is off.
        app.state.poller = SubscriptionPoller(
            app.state.db,
            FeedClient(settings.ytdlp_proxy),
            app.state.youtube_worker,
            poll_minutes=settings.subscription_poll_minutes or 60,
        )
        if settings.subscription_poll_minutes > 0:
            # New videos of subscribed channels go into the YouTube queue (#20). The feeds are
            # fetched through the same proxy as yt-dlp.
            app.state.scheduler.add_interval_job(
                app.state.poller.run_once, job_id=POLL_JOB_ID, seconds=POLL_JOB_SECONDS
            )
        else:
            logger.info("channel polling is off (SUBSCRIPTION_POLL_MINUTES=0)")
        # Podcast feeds, the same way (#34); slower, and the "check now" button works regardless.
        app.state.podcast_poller = PodcastPoller(
            app.state.db,
            app.state.podcast_feeds,
            app.state.podcast_worker,
            poll_minutes=settings.podcast_poll_minutes or 240,
        )
        if settings.podcast_poll_minutes > 0:
            app.state.scheduler.add_interval_job(
                app.state.podcast_poller.run_once,
                job_id=PODCAST_POLL_JOB_ID,
                seconds=POLL_JOB_SECONDS,
            )
        else:
            logger.info("podcast polling is off (PODCAST_POLL_MINUTES=0)")
        if app.state.jellyfin.enabled:
            # Sends the debounced library refresh once things have been quiet for a while.
            app.state.scheduler.add_interval_job(
                app.state.jellyfin.flush_due, job_id="jellyfin-refresh", seconds=5
            )
    try:
        yield
    finally:
        worker_stop.set()
        if app.state.poller is not None:
            app.state.poller.stop.set()  # a run in progress ends after its current channel
        if app.state.podcast_poller is not None:
            app.state.podcast_poller.stop.set()
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown()
        if app.state.db is not None:
            app.state.db.close()
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
    app.state.db = None
    app.state.db_error = None
    app.state.qbit = None
    app.state.torrent_progress = TorrentProgress(None)
    app.state.scheduler = None
    app.state.jellyfin = LibraryRefresher(None)
    app.state.reconciler = None
    app.state.poller = None
    app.state.podcast_feeds = None
    app.state.podcast_worker = None
    app.state.podcast_poller = None
    app.state.watchdog_status = None
    app.state.youtube = None
    app.state.channels = None
    app.state.youtube_worker = None
    app.state.organizer = None  # set by #16: organize(job, torrent) -> None
    api.register(app)
    register_ui(app)
    app.add_middleware(SecurityMiddleware)

    @app.get("/health")
    async def health(response: Response) -> dict[str, object]:
        """Is the app itself fine? Deliberately does not call qBittorrent or Jellyfin.

        Both sit behind the VPN or depend on it; a check that reached for them would go red
        every time the tunnel reconnects and say nothing about this service (the VPN has its
        own watchdog, #18/#22). A degraded app answers 503, so Docker's HEALTHCHECK sees it.
        """
        data_root_ok = resolved.data_root.is_dir()
        db: Database | None = app.state.db
        if db is None:
            db_state = f"unavailable: {app.state.db_error or 'not opened'}"
        else:
            problem = db.check()
            db_state = "ok" if problem is None else f"unavailable: {problem}"
        scheduler: AppScheduler | None = app.state.scheduler
        if scheduler is None:
            scheduler_state = "disabled: no database"
        else:
            scheduler_problem = scheduler.check()
            scheduler_state = "ok" if scheduler_problem is None else f"stalled: {scheduler_problem}"
        # "disabled" is already reported through the database; only a scheduler that should work
        # and does not turns the app unhealthy on its own.
        healthy = data_root_ok and db_state == "ok" and scheduler_state in {"ok"}
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "checks": {
                "data_root": "ok" if data_root_ok else "missing",
                "database": db_state,
                "scheduler": scheduler_state,
            },
        }

    return app
