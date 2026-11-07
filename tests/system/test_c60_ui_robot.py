"""Robot Framework + SeleniumLibrary against the real app (issue #60).

    YTP_UI_TESTS=1 pytest tests/system/test_c60_ui_robot.py

This is a system test without Docker: the app runs in this process on a free localhost port
(with stand-ins for YouTube and downloads, and a fake qBittorrent for the magnet form), and
Robot drives Debian Chromium through chromedriver. The `test:ui` job installs both; locally
they must be on PATH.

Reports: `reports/robot/output.xml`, `log.html`, `report.html`, `junit.xml` (CI artifacts).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from tests.helpers.accounts_app import ADMIN, PASSWORD, fast_hash
from tests.helpers.fake_qbittorrent import PASSWORD as QBIT_PASSWORD
from tests.helpers.fake_qbittorrent import USER as QBIT_USER
from tests.helpers.fake_qbittorrent import FakeQbittorrent
from ytp.config import Settings
from ytp.main import create_app
from ytp.youtube import VideoInfo

pytestmark = [
    pytest.mark.system,
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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    assert server.started, "the app did not start"
    app.state.youtube = StandInProbe()
    app.state.youtube_worker.downloader = NoDownload()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        qbit.stop()


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
def test_the_robot_ui_suite(base_url):
    chrome, driver = _browser_tools()
    REPORTS.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "robot",
        "--outputdir",
        str(REPORTS),
        "--xunit",
        str(REPORTS.parent / "junit-ui.xml"),
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
        str(SUITES),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1200, check=False)
    output = result.stdout + "\n" + result.stderr
    print(output)
    assert result.returncode == 0, output
