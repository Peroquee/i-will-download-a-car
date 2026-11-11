"""The unit tests share their fixtures (issue #77): a copied one fails this test."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent


def _sources():
    me = Path(__file__).name
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(HERE.glob("test_*.py"))
        if path.name != me
    ]


def test_the_media_folders_come_from_the_shared_helper():
    """A new MEDIA_DIR_* setting needs one edit in tests/helpers/app.py, not twenty."""
    copies = [name for name, source in _sources() if '"MEDIA_DIR_MOVIES": "Filme"' in source]
    assert copies == []


def test_the_small_fakes_and_clients_are_not_copied_again():
    """free_port, the clocks, the watchdog/internet/feed fakes and db_of live in tests/helpers."""
    markers = (
        "def free_port(",
        "class Clock:",
        "class DatetimeClock:",
        "class FakeWatchdog:",
        "class FakeInternet:",
        "class FakeFeeds(",
        "def db_of(",
    )
    offenders = [
        (name, marker) for name, source in _sources() for marker in markers if marker in source
    ]
    assert offenders == []


def test_the_download_worker_guard_stops_the_real_threads(monkeypatch):
    """The dismissal/ownership tests create queued jobs; the app's real worker thread could claim
    and fail one (the !109 flake). The guard makes both workers claim nothing."""
    from types import SimpleNamespace

    from tests.helpers.app import stop_download_workers
    from ytp.podcast_jobs import PodcastWorker
    from ytp.youtube_jobs import YoutubeWorker

    stop_download_workers(monkeypatch)
    assert YoutubeWorker.run_one(SimpleNamespace()) is False
    assert PodcastWorker.run_one(SimpleNamespace()) is False
