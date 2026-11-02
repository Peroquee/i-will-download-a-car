"""A real browser against the real app (issue #43). Gated: needs Docker and internet.

    YTP_NETWORK_TESTS=1 pytest tests/system/test_c43_browser.py

The app runs in this process (uvicorn on 127.0.0.1, a temp data folder, a stand-in for the YouTube
lookup and for the download so nothing leaves the machine). Headless Chromium runs in the Playwright
container with the host's network and executes `browser/ui_checks.py`: the form makes a job, the
navigation and the VPN bar go where they say, a page without its scripts ends on the reload page,
and an update while a page is open gets a banner. Not part of the pipeline: the image is large; run
it on the VM. `YTP_PLAYWRIGHT_IMAGE` overrides the image (Python and the browsers); the matching
`playwright` package is installed with pip inside the container (`YTP_PLAYWRIGHT_VERSION`).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from tests.helpers import docker
from ytp.config import Settings
from ytp.main import create_app
from ytp.youtube import VideoInfo

pytestmark = [
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

# The image has the browsers, not the Python package: the same version is installed with pip
# inside the container first (the browsers and the package must match).
PLAYWRIGHT_VERSION = os.environ.get("YTP_PLAYWRIGHT_VERSION", "1.49.1")
IMAGE = os.environ.get(
    "YTP_PLAYWRIGHT_IMAGE", f"mcr.microsoft.com/playwright/python:v{PLAYWRIGHT_VERSION}-jammy"
)
CHECKS = Path(__file__).parent / "browser" / "ui_checks.py"
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
        raise RuntimeError("the browser test downloads nothing")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("browser-app")
    (root / "config").mkdir()
    (root / "downloads").mkdir()
    for name in ("Filme", "Serien", "Hörbücher", "Musik", "Podcasts", "Youtube"):
        (root / name).mkdir()
    settings = Settings.from_env(
        {**ENV, "DATA_ROOT": str(root), "CONFIG_DIR": str(root / "config")}
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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)


def test_the_page_works_in_a_real_browser(base_url):
    install_and_run = (
        "python -m pip install --quiet --disable-pip-version-check "
        f"playwright=={PLAYWRIGHT_VERSION}"
        ' && exec python /checks/ui_checks.py "$0"'
    )
    result = docker.run(
        [
            "run", "--rm", "--network", "host", "--ipc", "host",
            "-v", f"{CHECKS}:/checks/ui_checks.py:ro",
            IMAGE, "sh", "-c", install_and_run, base_url,
        ],
        timeout=900,
    )  # fmt: skip
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert output.count("ok  ") >= 10, output
