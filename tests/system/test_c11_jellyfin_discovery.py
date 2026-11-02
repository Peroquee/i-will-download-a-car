"""A new file really shows up in the real Jellyfin after the app's refresh (issue #11 follow-up).

    YTP_NETWORK_TESTS=1 YTP_JELLYFIN_URL=http://192.168.178.108:8096 YTP_JELLYFIN_API_KEY=... \
      YTP_JELLYFIN_TEST_FOLDER=/data/Podcasts pytest tests/system/test_c11_jellyfin_discovery.py

`YTP_JELLYFIN_TEST_FOLDER` is a library folder on THIS machine that Jellyfin also sees (its
location in Jellyfin is found by the name of the folder, like the app does). The test drops a small
valid mp3 into a new subfolder, lets the app's `LibraryRefresher` ask Jellyfin to refresh, waits
until Jellyfin lists the file, and removes everything again (and refreshes once more so the item
goes away). The per-library refresh this replaced (`POST /Items/{id}/Refresh`) never found the file:
on Jellyfin 10.11.11 a library stayed at 0 items with 21 new files on disk. The key is never
printed.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ytp.clients.jellyfin import JellyfinClient, LibraryRefresher

URL = os.environ.get("YTP_JELLYFIN_URL", "")
KEY = os.environ.get("YTP_JELLYFIN_API_KEY", "")
FOLDER = os.environ.get("YTP_JELLYFIN_TEST_FOLDER", "")
WAIT_SECONDS = float(os.environ.get("YTP_JELLYFIN_WAIT", "240"))

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs a real Jellyfin)",
    ),
    pytest.mark.skipif(
        not (URL and KEY and FOLDER),
        reason="set YTP_JELLYFIN_URL, YTP_JELLYFIN_API_KEY and YTP_JELLYFIN_TEST_FOLDER",
    ),
]

# One MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz, 417 bytes) of silence, repeated: a file that
# ffprobe and Jellyfin accept as audio of about a second.
MP3 = (b"\xff\xfb\x90\x00" + b"\x00" * 413) * 40


def items_below(library_id: str) -> list[dict]:
    parts = urlsplit(URL)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=20)
    try:
        conn.request(
            "GET",
            f"/Items?ParentId={library_id}&Recursive=true&Fields=Path&Limit=2000",
            headers={"Authorization": f'MediaBrowser Token="{KEY}"', "Accept": "application/json"},
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        assert response.status == 200, f"Items answered {response.status}"
    finally:
        conn.close()
    return json.loads(body).get("Items", [])


def listed(library_id: str, file_name: str) -> bool:
    return any(str(item.get("Path", "")).endswith(file_name) for item in items_below(library_id))


def wait_until(check, seconds: float, step: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return check()


def test_a_new_file_appears_in_jellyfin_after_the_apps_refresh():
    client = JellyfinClient(URL, KEY)
    assert client.ping()
    folder = Path(FOLDER)
    assert folder.is_dir(), f"{FOLDER} is not a folder on this machine"
    libraries = [lib for lib in client.list_libraries() if lib.has_folder(folder.name)]
    assert libraries, f"no Jellyfin library has a location ending in {folder.name!r}"
    library = libraries[0]

    token = uuid.uuid4().hex[:10]
    drop = folder / f"ytp-discovery-test-{token}"
    file_name = f"ytp-discovery-{token}.mp3"
    refresher = LibraryRefresher(client, quiet_seconds=0)
    try:
        drop.mkdir(mode=0o755)
        (drop / file_name).write_bytes(MP3)
        drop.chmod(0o755)
        (drop / file_name).chmod(0o644)  # Jellyfin reads through the "other" bits
        assert not listed(library.item_id, file_name), "the file was listed before any refresh"
        refresher.request_refresh(folder.name)
        assert refresher.flush_due(), "the refresher did not send the refresh"
        assert wait_until(lambda: listed(library.item_id, file_name), WAIT_SECONDS), (
            f"Jellyfin did not list {file_name} within {WAIT_SECONDS:.0f} s of the refresh"
        )
    finally:
        shutil.rmtree(drop, ignore_errors=True)
        # let Jellyfin forget the item again; best effort, the test result does not depend on it
        refresher.request_refresh(folder.name)
        refresher.flush_due()
