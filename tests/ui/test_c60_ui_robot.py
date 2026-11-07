"""Robot Framework + SeleniumLibrary against the real app (issue #60).

    YTP_UI_TESTS=1 pytest tests/ui/test_c60_ui_robot.py

The app runs in this process on a free localhost port
(with stand-ins for YouTube and downloads, and a fake qBittorrent for the magnet form), and
Robot drives Debian Chromium through chromedriver. The `test:ui` job installs both; locally
they must be on PATH.

Reports: `reports/robot/output.xml`, `log.html`, `report.html`, `junit.xml` (CI artifacts).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from tests.helpers.accounts_app import ADMIN, PASSWORD, fast_hash
from tests.helpers.fake_qbittorrent import PASSWORD as QBIT_PASSWORD
from tests.helpers.fake_qbittorrent import USER as QBIT_USER
from tests.helpers.fake_qbittorrent import FakeQbittorrent
from ytp import accounts, alertlog, subscriptions
from ytp import jobs as job_store
from ytp.channels import ChannelResolver
from ytp.config import Settings
from ytp.main import create_app
from ytp.podcast_subscriptions import add as add_podcast_subscription
from ytp.podcasts import PodcastFeedClient
from ytp.safefetch import Fetched
from ytp.youtube import VideoInfo

pytestmark = [
    pytest.mark.slow,
    pytest.mark.real_auth,  # the browser must see the real sign-in, not the test admin
    pytest.mark.skipif(
        os.environ.get("YTP_UI_TESTS") != "1",
        reason="set YTP_UI_TESTS=1 (needs chromium, chromedriver and robotframework)",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
SUITES = Path(__file__).parent / "robot"
REPORTS = ROOT / "reports" / "robot"
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
    "SUBSCRIPTION_POLL_MINUTES": "0",
    "PODCAST_POLL_MINUTES": "0",
}


class StandInProbe:
    timeout = 1.0
    socket_timeout = 1.0
    proxy = None

    async def probe(self, ref):
        return VideoInfo(
            video_id=ref.video_id,
            url=ref.url,
            title="Big Buck Bunny",
            channel="Blender Foundation",
            channel_id="UCSMOQeBJ2RAnuFungnQOxLg",
            duration=635,
            thumbnail="",
        )


class NoDownload:
    def download(self, *args, **kwargs):
        raise RuntimeError("the UI test downloads nothing")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class OkWatchdog:
    def fetch_status(self):
        return {
            "state": "ok",
            "egress": "ok",
            "egress_country": "Switzerland",
            "last_poll": _now(),
            "since": "2026-11-07T09:00:00Z",
            "paused": False,
        }


class DownWatchdog:
    def fetch_status(self):
        return {
            "state": "down",
            "paused": True,
            "reason": "seeded by the UI test",
            "last_poll": _now(),
        }


class OkInternet:
    def refresh(self):
        return {"state": "ok", "checked_at": _now()}


class OfflineInternet:
    def refresh(self):
        return {"state": "offline", "checked_at": _now()}


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Seeded Podcast</title>
<item><title>Episode One</title><guid>one</guid>
<pubDate>Tue, 03 Nov 2026 10:00:00 +0000</pubDate>
<enclosure url="https://example.invalid/one.mp3" type="audio/mpeg" length="12345"/></item>
<item><title>Episode Two</title><guid>two</guid>
<pubDate>Mon, 02 Nov 2026 10:00:00 +0000</pubDate>
<enclosure url="https://example.invalid/two.mp3" type="audio/mpeg" length="23456"/></item>
</channel></rss>"""


class StubFeedFetcher:
    def fetch_bytes(self, url, *, kind, max_bytes):
        return Fetched(str(url), "application/rss+xml", RSS, len(RSS))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    assert server.started, "the app did not start"
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("robot-app")
    (root / "config").mkdir()
    (root / "downloads").mkdir()
    for name in ("Filme", "Serien", "Hörbücher", "Musik", "Podcasts", "Youtube"):
        (root / name).mkdir()
    qbit = FakeQbittorrent().start()
    settings = Settings.from_env(
        {
            **ENV,
            "DATA_ROOT": str(root),
            "CONFIG_DIR": str(root / "config"),
            "APP_ADMIN_USER": ADMIN,
            "APP_ADMIN_PASSWORD_HASH": fast_hash(),
            "QBIT_URL": qbit.url,
            "QBIT_USER": QBIT_USER,
            "QBIT_PASSWORD": QBIT_PASSWORD,
        }
    )
    app = create_app(settings)
    server, thread, url = start_server(app)
    app.state.youtube = StandInProbe()
    app.state.channels = ChannelResolver(app.state.youtube)
    app.state.youtube_worker.downloader = NoDownload()
    app.state.watchdog_status = OkWatchdog()
    app.state.internet = OkInternet()
    app.state.podcast_feeds = PodcastFeedClient(StubFeedFetcher())
    if app.state.podcast_worker is not None:
        app.state.podcast_worker.feeds = app.state.podcast_feeds
    if app.state.podcast_poller is not None:
        app.state.podcast_poller.feeds = app.state.podcast_feeds

    db = app.state.db
    accounts.create_with_hash(db, "alice", fast_hash(), "user")
    admin = accounts.find(db, ADMIN)[0]
    alice = accounts.find(db, "alice")[0]
    channel, _ = subscriptions.add(db, "UC-SEEDED", "Seeded Channel")
    accounts.follow(db, "channel", channel.id, admin.id)
    accounts.follow(db, "channel", channel.id, alice.id)
    podcast, _ = add_podcast_subscription(db, "https://example.org/seed.xml", "Seeded Podcast")
    accounts.follow(db, "podcast", podcast.id, admin.id)
    for number in range(1, 13):
        done, _ = job_store.submit_job(
            db,
            "youtube",
            f"seed-done-{number}",
            json.dumps({"type": "youtube", "channel": "Seeder", "title": f"Done {number}"}),
        )
        job_store.transition(db, done.id, "done", expected=("queued",))
    seeded_video, _ = job_store.submit_job(
        db,
        "youtube",
        "seed-video",
        json.dumps({"type": "youtube", "channel": "Seeder", "title": "Seeded Video"}),
    )
    job_store.mark_failed(db, seeded_video.id, "seeded failure")
    seeded_magnet, _ = job_store.submit_magnet_job(
        db, "b" * 40, json.dumps({"type": "movies", "title": "Seeded Magnet", "year": 2000})
    )
    job_store.mark_failed(db, seeded_magnet.id, "seeded failure")
    qbit.torrents.append(
        {
            "hash": "b" * 40,
            "name": "Seeded Magnet",
            "state": "downloading",
            "progress": 0.4,
            "size": 1000,
            "amount_left": 600,
            "save_path": str(root / "downloads"),
        }
    )
    for free in (4, 3):
        alertlog.add(db, "disk_low", "warning", "", "", {"free_percent": free, "free_gb": free})
    alertlog.add(db, "vpn_down", "error", "Seed VPN down", "The VPN is down.")
    alertlog.record_vpn_event(db, "ok", False, "")
    alertlog.record_vpn_event(db, "down", True, "seed")
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        qbit.stop()


@pytest.fixture(scope="module")
def down_base_url(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("robot-down-app")
    (root / "config").mkdir()
    (root / "downloads").mkdir()
    for name in ("Filme", "Serien", "Hörbücher", "Musik", "Podcasts", "Youtube"):
        (root / name).mkdir()
    settings = Settings.from_env(
        {
            **ENV,
            "DATA_ROOT": str(root),
            "CONFIG_DIR": str(root / "config"),
            "APP_ADMIN_USER": ADMIN,
            "APP_ADMIN_PASSWORD_HASH": fast_hash(),
        }
    )
    app = create_app(settings)
    server, thread, url = start_server(app)
    app.state.watchdog_status = DownWatchdog()
    app.state.internet = OfflineInternet()
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _browser_tools() -> tuple[str, str]:
    if importlib.util.find_spec("robot") is None:
        pytest.fail(
            "robotframework is not installed; install requirements-ui.txt "
            "or run this through the test:ui job"
        )
    chrome = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    driver = shutil.which("chromedriver")
    if chrome is None or driver is None:
        pytest.fail(
            "chromium and chromedriver are needed for the UI tests "
            "(Debian: apt install chromium chromium-driver)"
        )
    return chrome, driver


@pytest.mark.timeout(1200)
def run_robot(base_url, suites, output_dir, junit):
    chrome, driver = _browser_tools()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "robot",
        "--outputdir",
        str(output_dir),
        "--xunit",
        str(junit),
        "--variable",
        f"BASE_URL:{base_url}",
        "--variable",
        f"ADMIN_USER:{ADMIN}",
        "--variable",
        f"ADMIN_PASSWORD:{PASSWORD}",
        "--variable",
        f"CHROME_BINARY:{chrome}",
        "--variable",
        f"CHROMEDRIVER:{driver}",
        str(suites),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1200, check=False)
    output = result.stdout + "\n" + result.stderr
    (output_dir / "console.log").write_text(output, encoding="utf-8")
    print(output)
    assert result.returncode == 0, output


@pytest.mark.timeout(1200)
def test_the_robot_ui_suite(base_url):
    run_robot(base_url, SUITES, REPORTS, REPORTS.parent / "junit-ui.xml")


@pytest.mark.timeout(600)
def test_the_vpn_down_state_in_the_ui(down_base_url):
    run_robot(
        down_base_url,
        Path(__file__).parent / "robot_down" / "09_vpn_down.robot",
        REPORTS / "vpn-down",
        REPORTS.parent / "junit-ui-vpn-down.xml",
    )
