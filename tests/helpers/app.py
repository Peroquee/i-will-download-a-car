"""The unit tests' app: the media-folder settings and one client builder (issue #77).

The same six `MEDIA_DIR_*` values and the same `Settings.from_env` call were copied into about
twenty test modules. A new required setting (or a renamed media folder) now needs one edit here.

`make_client(tmp_path, **env)` builds the app in `tmp_path/data` (the folders exist, also
`downloads/incomplete`). Any keyword overrides an environment value, e.g.
`make_client(tmp_path, QBIT_URL=fake.url)`. It is not started: use it as a context manager
(`with make_client(...) as client:`) or take `started_client(...)` when a test needs one that is
already entered.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from ytp.config import Settings
from ytp.main import create_app

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


def make_client(tmp_path: Path, **env: str) -> TestClient:
    """The app, not started: use it as a context manager (`with make_client(...)`) or through
    `running` / `started_client`."""
    data = tmp_path / "data"
    for name in ENV.values():
        (data / name).mkdir(parents=True, exist_ok=True)
    (data / "downloads" / "incomplete").mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(data), "CONFIG_DIR": str(config), **env}
    return TestClient(create_app(Settings.from_env(values)))


def started_client(tmp_path: Path, **env: str) -> TestClient:
    """`make_client`, already entered. Close it with `client.__exit__()`."""
    client = make_client(tmp_path, **env)
    client.__enter__()
    return client
