"""QbitClient against a real qBittorrent (issue #10).

Starts the linuxserver image with a seeded login and drives it with the app's client. This is
what proves the details the fake in the unit tests only imitates: the 204 login, the 403 for a
logged-out session, torrents/stop vs pause, the state filter names, and how a magnet with a
category ends up listed. Needs a Docker daemon; run it on the VM with `make test-system`.

The container's Web UI port is published under the SAME number inside and outside, because
qBittorrent answers 401 when the Host header's port differs from its own.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from tests.helpers import docker
from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT
from tests.helpers.net import free_port
from ytp.clients.qbittorrent import QbitAuthError, QbitClient, QbitUnavailable

pytestmark = [pytest.mark.docker, pytest.mark.network, pytest.mark.slow]

SEED = REPO_ROOT / "scripts" / "qbittorrent" / "seed_config.py"
CATEGORIES = REPO_ROOT / "scripts" / "qbittorrent" / "setup_categories.py"
IMAGE = "lscr.io/linuxserver/qbittorrent:" + parse_env_file(ENV_EXAMPLE)["QBITTORRENT_TAG"]
USER = "tester"
PASSWORD = "a long test password"  # a test value, not a secret
HASH = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"  # arbitrary: no peers will ever have it
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=ytp-test"


def python(script, *args: str, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    config = tmp_path_factory.mktemp("qbit-client-config")
    port = free_port()
    seeded = python(
        SEED,
        "--user",
        USER,
        "--config",
        str(config / "qBittorrent" / "qBittorrent.conf"),
        "--port",
        str(port),
        "--password-stdin",
        stdin=PASSWORD + "\n",
    )
    assert seeded.returncode == 0, seeded.stderr
    name = docker.unique_name("qbit-client")
    started = docker.run(
        [
            "run", "-d", "--name", name,
            "-e", "PUID=1000", "-e", "PGID=1000", "-e", "TZ=UTC", "-e", f"WEBUI_PORT={port}",
            "-p", f"127.0.0.1:{port}:{port}",
            "-v", f"{config}:/config",
            IMAGE,
        ],
        timeout=600,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                QbitClient(url, USER, PASSWORD, timeout=5).version()
                break
            except (QbitUnavailable, QbitAuthError):
                time.sleep(2)
        else:
            pytest.fail(
                "qBittorrent did not accept the login\n" + docker.run(["logs", name]).stdout
            )
        categories = python(
            CATEGORIES, "--url", url, "--user", USER, "--password-stdin", stdin=PASSWORD + "\n"
        )
        assert categories.returncode == 0, categories.stdout + categories.stderr
        yield url
    finally:
        docker.run(["rm", "-f", name])


@pytest.fixture
def client(base_url) -> QbitClient:
    return QbitClient(base_url, USER, PASSWORD, timeout=10)


def delete_all(client: QbitClient) -> None:
    client._request("POST", "torrents/delete", form={"hashes": "all", "deleteFiles": "true"})


def test_login_and_version_against_the_real_thing(client):
    assert client.version().startswith("v")


def test_a_wrong_password_is_an_auth_error_and_not_retried(base_url):
    client = QbitClient(base_url, USER, "definitely the wrong one", timeout=10)
    with pytest.raises(QbitAuthError):
        client.version()
    with pytest.raises(QbitAuthError):
        client.version()  # answered from memory: qBittorrent saw one failed login, not two


def test_a_logged_out_session_is_renewed_once(client):
    client.version()
    client._request("POST", "auth/logout")
    assert client.version().startswith("v"), "403 for the dead cookie, one login, then fine"


def test_add_list_filter_and_pause_a_magnet(client):
    try:
        assert client.add_magnet(MAGNET, "movies") == HASH
        deadline = time.monotonic() + 30
        found = []
        while time.monotonic() < deadline and not found:
            found = client.list_torrents(hashes=[HASH])
            time.sleep(1)
        (torrent,) = found
        assert torrent.hash == HASH
        assert torrent.category == "movies"
        assert torrent.save_path.rstrip("/") == "/data/downloads/movies", (
            "the category path applies"
        )
        assert [t.hash for t in client.list_torrents(category="movies")] == [HASH]
        assert client.list_torrents(category="music") == []
        assert [t.hash for t in client.list_torrents("all")] == [HASH]

        client.pause_all()
        time.sleep(2)
        # Whichever generation the image is, both names of the state work through the client.
        assert [t.hash for t in client.list_torrents("paused")] == [HASH]
        assert [t.hash for t in client.list_torrents("stopped")] == [HASH]
        # The "downloading" filter is qBittorrent's isDownloading(): it ALSO matches stoppedDL,
        # queuedDL and stalledDL, so a paused torrent is still listed. The state decides.
        (paused,) = client.list_torrents(hashes=[HASH])
        assert paused.state in {"stoppedDL", "pausedDL"}
        assert paused.paused
        assert [t.hash for t in client.list_torrents("downloading")] == [HASH]

        client.resume_all()
    finally:
        delete_all(client)
