"""The FastAPI application (issue #8): configuration, logging and /health.

The database (#9) is opened at startup; later issues add the qBittorrent and Jellyfin clients
(#10, #11) and the scheduler (#12). Everything with a lifetime is created in `create_app` / the
lifespan and hung off `app.state`, so tests build the app with explicit settings and nothing
is global.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from . import __version__, accounts, api, auth, auth_api, jobs, youtube_cookies
from .auth import AuthMiddleware, LoginLimiter
from .channel_videos import ChannelLister
from .channels import ChannelResolver
from .clients.jellyfin import JellyfinClient, LibraryRefresher
from .clients.qbittorrent import QbitClient
from .clients.watchdog import WatchdogStatusClient
from .config import Settings
from .db import Database, DatabaseError
from .feeds import FeedClient
from .jobs_api import TorrentProgress
from .logging_setup import configure_logging
from .monitor import MONITOR_JOB_ID, MONITOR_SECONDS, AlertMonitor
from .netstatus import InternetChecker
from .organizer import FileOrganizer
from .podcast_backlog import FEEDER_JOB_ID as BACKLOG_JOB_ID
from .podcast_backlog import FEEDER_SECONDS as BACKLOG_SECONDS
from .podcast_backlog import BacklogFeeder
from .podcast_jobs import POLL_JOB_ID as PODCAST_POLL_JOB_ID
from .podcast_jobs import PodcastPoller, PodcastWorker
from .podcasts import PodcastFeedClient
from .priority import PRIORITY_JOB_ID, PRIORITY_SECONDS, PriorityManager
from .reconciler import RECONCILE_JOB_ID, RECONCILE_SECONDS, Reconciler
from .safefetch import SafeFetcher
from .scheduler import AppScheduler
from .storage import StorageSampler
from .subscription_poller import POLL_JOB_ID, POLL_JOB_SECONDS, SubscriptionPoller
from .web import SecurityMiddleware, register_ui
from .youtube import YoutubeProbe
from .youtube_download import YoutubeDownloader
from .youtube_jobs import YoutubeWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.started_at = time.monotonic()  # /api/vpn says "starting" for a minute (#43)
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
    if app.state.db is not None:
        _set_up_accounts(app.state.db, settings, app.state.test_account)
    if app.state.db is not None and app.state.qbit is None:
        # Without qBittorrent no magnet can transfer. An old hold state from before must not
        # block the normal YouTube/podcast queues (#70); no priority manager will clear it.
        jobs.set_magnet_transferring(app.state.db, False)
    # The dashboard's storage sizes (#82): a background walk after downloads finish (debounced)
    # and once at start when nothing is cached yet. Never while the dashboard polls.
    app.state.storage = None
    if app.state.db is not None:
        app.state.storage = StorageSampler(app.state.db, settings)
        app.state.storage.start()
        app.state.storage.ensure_cached()
    # YouTube downloads (#18): the database is the queue, one worker thread runs it.
    app.state.youtube_worker = None
    app.state.youtube_worker_thread = None
    worker_stop = threading.Event()
    if app.state.db is not None:
        app.state.youtube_worker = YoutubeWorker(
            app.state.db,
            settings,
            YoutubeDownloader(
                proxy=settings.ytdlp_proxy,
                max_height=settings.youtube_max_height,
                # The admin's YouTube login cookies, if one was stored (#46).
                cookie_file=youtube_cookies.cookie_path(settings),
            ),
            app.state.jellyfin,
            on_done=app.state.storage.request,
        )
        app.state.youtube_worker_thread = threading.Thread(
            target=app.state.youtube_worker.run, args=(worker_stop,), name="youtube", daemon=True
        )
        app.state.youtube_worker_thread.start()
    # Podcast episodes (#34): the same kind of queue and worker thread. Feeds and files are
    # fetched through SafeFetcher (public addresses only, through the VPN proxy).
    app.state.podcast_feeds = PodcastFeedClient(
        SafeFetcher(settings.podcast_proxy, blocked_networks=settings.fetch_blocked_networks)
    )
    app.state.podcast_worker = None
    app.state.podcast_worker_thread = None
    if app.state.db is not None:
        app.state.podcast_worker = PodcastWorker(
            app.state.db,
            settings,
            app.state.podcast_feeds,
            app.state.jellyfin,
            on_done=app.state.storage.request,
        )
        app.state.podcast_worker_thread = threading.Thread(
            target=app.state.podcast_worker.run, args=(worker_stop,), name="podcasts", daemon=True
        )
        app.state.podcast_worker_thread.start()
    # The scheduler needs the database (its jobs keep their state there).
    app.state.scheduler = None
    app.state.reconciler = None
    app.state.priority = None
    app.state.poller = None
    app.state.podcast_poller = None
    app.state.backlog_feeder = None
    app.state.monitor = None
    if app.state.db is not None:
        app.state.scheduler = AppScheduler(app.state.db)
        app.state.scheduler.start()
        if app.state.qbit is not None:
            # Turns finished torrents into organized files (#15); the organizer arrives with #16.
            app.state.reconciler = Reconciler(
                app.state.db,
                app.state.qbit,
                app.state.organizer,
                on_organized=app.state.storage.request,
                stall_seconds=settings.magnet_stall_days * 86400,
                downloads_dir=settings.downloads_dir,
            )
            app.state.scheduler.add_interval_job(
                app.state.reconciler.run_once,
                job_id=RECONCILE_JOB_ID,
                seconds=RECONCILE_SECONDS,
                run_immediately=True,
            )
            # Holds normal torrents back while a high-priority job downloads (#51). The
            # watchdog status keeps a VPN/egress pause intact (the watchdog's policy wins).
            app.state.priority = PriorityManager(
                app.state.db, app.state.qbit, app.state.watchdog_status
            )
            app.state.scheduler.add_interval_job(
                app.state.priority.tick,
                job_id=PRIORITY_JOB_ID,
                seconds=PRIORITY_SECONDS,
                run_immediately=True,
            )
        # The poller also serves the "check now" button, so it exists even when the schedule is off.
        app.state.poller = SubscriptionPoller(
            app.state.db,
            FeedClient(settings.ytdlp_proxy),
            app.state.youtube_worker,
            poll_minutes=settings.subscription_poll_minutes or 60,
            probe=app.state.youtube,  # lives, premieres and Shorts are left out (#39)
            # The list of a channel's newest videos comes from yt-dlp (the RSS feed is the
            # fallback: YouTube answers it with 404 for whole networks), through the same proxy.
            lister=ChannelLister(
                app.state.youtube, cookie_file=youtube_cookies.cookie_path(settings)
            ),
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
        # "All episodes" of a feed (#45): a few at a time into the queue, only while there is room.
        app.state.backlog_feeder = BacklogFeeder(app.state.db, app.state.podcast_worker, settings)
        app.state.scheduler.add_interval_job(
            app.state.backlog_feeder.tick, job_id=BACKLOG_JOB_ID, seconds=BACKLOG_SECONDS
        )
        if settings.podcast_poll_minutes > 0:
            app.state.scheduler.add_interval_job(
                app.state.podcast_poller.run_once,
                job_id=PODCAST_POLL_JOB_ID,
                seconds=POLL_JOB_SECONDS,
            )
        else:
            logger.info("podcast polling is off (PODCAST_POLL_MINUTES=0)")
        # Notices what a person should know about and writes the alert log (#38).
        app.state.monitor = AlertMonitor(
            app.state.db, app.state.watchdog_status, settings.data_root
        )
        app.state.scheduler.add_interval_job(
            app.state.monitor.run_once,
            job_id=MONITOR_JOB_ID,
            seconds=MONITOR_SECONDS,
        )
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
        if app.state.storage is not None:
            app.state.storage.stop()
        if app.state.db is not None:
            app.state.db.close()
        logger.info("stopped")


def _set_up_accounts(
    db: Database, settings: Settings, test_account: accounts.Account | None = None
) -> None:
    """The first admin from `.env` (see `accounts.bootstrap_admin`), or a warning if there is no
    account at all (nobody could log in). `test_account` is the test hook (#76)."""
    if test_account is not None:  # tests only: the account every request counts as
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO accounts (id, username, password_hash, role) "
                "VALUES (?, ?, 'test', ?)",
                (test_account.id, test_account.username, test_account.role),
            )
    if settings.admin_password_hash:
        result = accounts.bootstrap_admin(db, settings.admin_user, settings.admin_password_hash)
        logger.info("admin account", extra={"result": result, "admin_user": settings.admin_user})
    elif not accounts.any_exists(db):
        logger.error(
            "no account exists, so nobody can log in: set the admin password with "
            "`make admin-password` (it writes APP_ADMIN_USER and APP_ADMIN_PASSWORD_HASH to .env), "
            "then restart the app"
        )


def _workers_state(state) -> str:
    """Whether the download worker threads are alive (for /health, #69).

    A worker whose thread died would leave the API accepting jobs (201) that nobody ever runs;
    the app must say so instead of looking healthy.
    """
    workers = (
        ("youtube", state.youtube_worker, state.youtube_worker_thread),
        ("podcasts", state.podcast_worker, state.podcast_worker_thread),
    )
    dead = [
        name
        for name, worker, thread in workers
        if worker is not None and (thread is None or not thread.is_alive())
    ]
    if dead:
        return "not running: " + ", ".join(dead)
    if all(worker is None for _, worker, _ in workers):
        return "disabled: no database"
    return "ok"


def create_app(
    settings: Settings | None = None, *, test_account: accounts.Account | None = None
) -> FastAPI:
    """Build the app. With no argument the settings come from the environment.

    `test_account` is the test hook (#76): when given, every request counts as that account and
    its row is put into the database. Only tests pass it; `python -m ytp` never does.
    """
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
    app.state.started_at = time.monotonic()
    app.state.db = None
    app.state.db_error = None
    app.state.qbit = None
    app.state.torrent_progress = TorrentProgress(None)
    app.state.scheduler = None
    app.state.jellyfin = LibraryRefresher(None)
    app.state.reconciler = None
    app.state.priority = None
    app.state.poller = None
    app.state.podcast_feeds = None
    app.state.podcast_worker = None
    app.state.podcast_poller = None
    app.state.backlog_feeder = None
    app.state.monitor = None
    app.state.watchdog_status = None
    app.state.internet = InternetChecker(  # independent of the watchdog (#63)
        resolved.internet_check_host, resolved.internet_check_port
    )
    app.state.youtube = None
    app.state.channels = None
    app.state.youtube_worker = None
    app.state.youtube_worker_thread = None
    app.state.podcast_worker_thread = None
    app.state.organizer = None  # set by #16: organize(job, torrent) -> None
    app.state.storage = None  # the dashboard's cached category sizes (#82)
    app.state.limiter = LoginLimiter()
    app.state.test_account = test_account  # tests only (#76)
    app.state.trusted_proxies = auth.TrustedProxies(resolved.trusted_proxy)
    api.register(app)
    auth_api.register(app)
    register_ui(app)
    # The last middleware added is the outermost: the same-origin check and the security headers
    # see every request first, the login check (#44) runs inside them.
    app.add_middleware(
        AuthMiddleware,
        routes=lambda: app.routes,
        database=lambda: app.state.db,
        test_account=lambda: app.state.test_account,
    )
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
        workers_state = _workers_state(app.state)
        # "disabled" is already reported through the database; only a scheduler or a worker
        # that should work and does not turns the app unhealthy on its own (#69).
        healthy = (
            data_root_ok and db_state == "ok" and scheduler_state == "ok" and workers_state == "ok"
        )
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "checks": {
                "data_root": "ok" if data_root_ok else "missing",
                "database": db_state,
                "scheduler": scheduler_state,
                "workers": workers_state,
            },
        }

    return app
