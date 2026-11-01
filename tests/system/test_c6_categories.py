"""setup_categories.py against a real qBittorrent (issue #6).

Starts the linuxserver image with a seeded login, publishes its Web UI on a free
localhost port, runs the script, and reads the result back through the real API. This is
what proves the category and preference names against the real program; the unit tests
use a fake API. The last step of #6, adding a real torrent with a category and seeing the
file land in the right folder, is manual: docs/host-setup.md, step 5d.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator

import pytest

from tests.helpers import docker
from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT

pytestmark = [pytest.mark.docker, pytest.mark.network, pytest.mark.slow]

SEED = REPO_ROOT / "scripts" / "qbittorrent" / "seed_config.py"
CATEGORIES = REPO_ROOT / "scripts" / "qbittorrent" / "setup_categories.py"
IMAGE = "lscr.io/linuxserver/qbittorrent:" + parse_env_file(ENV_EXAMPLE)["QBITTORRENT_TAG"]
USER = "tester"
PASSWORD = "a long test password"  # a test value, not a secret


def python(script, *args: str, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class Client:
    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        data = urllib.parse.urlencode({"username": USER, "password": PASSWORD}).encode()
        # 204 with no body on success (current qBittorrent), 200 "Ok." on older versions;
        # a wrong password raises HTTPError 401, which the caller's retry loop treats as "not up".
        with self.opener.open(f"{base}/api/v2/auth/login", data, timeout=20) as resp:
            assert resp.status == 204 or resp.read() == b"Ok."

    def get(self, path: str):
        return json.loads(self.opener.open(f"{self.base}/api/v2/{path}", timeout=20).read())


@pytest.fixture(scope="module")
def webui(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    config = tmp_path_factory.mktemp("qbit-config")
    seeded = python(
        SEED,
        "--user",
        USER,
        "--config",
        str(config / "qBittorrent" / "qBittorrent.conf"),
        "--password-stdin",
        stdin=PASSWORD + "\n",
    )
    assert seeded.returncode == 0, seeded.stderr
    name = docker.unique_name("qbit-categories")
    started = docker.run(
        [
            "run", "-d", "--name", name,
            "-e", "PUID=1000", "-e", "PGID=1000", "-e", "TZ=UTC", "-e", "WEBUI_PORT=8080",
            "-p", "127.0.0.1::8080",
            "-v", f"{config}:/config",
            IMAGE,
        ],
        timeout=600,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    try:
        port = docker.run(["port", name, "8080/tcp"]).stdout.strip().rsplit(":", 1)[-1]
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                Client(base)
                break
            except (OSError, AssertionError):
                time.sleep(2)
        else:
            pytest.fail(
                "qBittorrent did not accept the login\n" + docker.run(["logs", name]).stdout
            )
        yield base
    finally:
        docker.run(["rm", "-f", name])


def run_setup(base: str) -> subprocess.CompletedProcess[str]:
    return python(
        CATEGORIES, "--url", base, "--user", USER, "--password-stdin", stdin=PASSWORD + "\n"
    )


def test_the_script_sets_categories_and_paths_in_a_real_qbittorrent(webui):
    result = run_setup(webui)
    assert result.returncode == 0, result.stdout + result.stderr
    client = Client(webui)
    categories = client.get("torrents/categories")
    for name in ("movies", "series", "audiobooks", "music"):
        assert categories[name]["savePath"] == f"/data/downloads/{name}"
    prefs = client.get("app/preferences")
    assert prefs["save_path"].rstrip("/") == "/data/downloads"
    assert prefs["temp_path_enabled"] is True
    assert prefs["temp_path"].rstrip("/") == "/data/downloads/incomplete"
    assert prefs["auto_tmm_enabled"] is True


def test_a_second_run_finds_nothing_to_change(webui):
    run_setup(webui)
    result = run_setup(webui)
    assert result.returncode == 0
    assert "nothing to change" in result.stdout
