"""The dashboard's storage sizes (#82): hardlinks once, the cache and the sampler."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from ytp import storage
from ytp.config import Settings
from ytp.db import Database
from ytp.scheduler import set_state

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
CATEGORIES = ("movies", "series", "audiobooks", "music", "podcasts", "youtube")


@pytest.fixture
def world(tmp_path):
    data = tmp_path / "data"
    for name in ENV.values():
        (data / name).mkdir(parents=True)
    (data / "downloads").mkdir()
    return SimpleNamespace(data=data, settings=Settings.from_env({**ENV, "DATA_ROOT": str(data)}))


@pytest.fixture
def db(tmp_path) -> Iterator[Database]:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


def usage(total: int = 10_000, used: int = 0, free: int = 10_000):
    return lambda path: SimpleNamespace(total=total, used=used, free=free)


def wait_for(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


# --- the walk ---------------------------------------------------------------------------------


def test_a_hardlink_is_counted_once_in_its_library_category(world):
    movie = world.data / "downloads" / "Big.mkv"
    movie.write_bytes(b"x" * 1000)
    os.link(movie, world.data / "Filme" / "Big.mkv")
    (world.data / "downloads" / "pending.mkv").write_bytes(b"y" * 300)

    sizes = storage.compute_sizes(world.settings, usage=usage(used=1700))

    assert sizes["categories"]["movies"] == 1000, "the library copy is not counted again"
    assert set(sizes["categories"]) == set(CATEGORIES)
    assert sizes["downloads"] == 300, "only the file that exists only in downloads"
    assert sizes["other"] == 400, "the rest of the used bytes"
    assert sizes["at"].endswith("Z")


def test_a_file_linked_into_two_libraries_is_counted_once(world):
    source = world.data / "downloads" / "a.bin"
    source.write_bytes(b"x" * 500)
    os.link(source, world.data / "Filme" / "a.bin")
    os.link(source, world.data / "Serien" / "a.bin")

    sizes = storage.compute_sizes(world.settings, usage=usage())

    assert sizes["categories"]["movies"] == 500, "the first library in the list owns it"
    assert sizes["categories"]["series"] == 0
    assert sizes["downloads"] == 0


def test_empty_and_missing_library_folders_are_zero(world):
    (world.data / "Filme").rmdir()
    sizes = storage.compute_sizes(world.settings, usage=usage())
    assert sizes["categories"]["movies"] == 0
    assert all(value == 0 for value in sizes["categories"].values())
    assert sizes["downloads"] == 0
    assert sizes["other"] == 0


def test_a_symlink_is_not_followed(world, tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"z" * 900)
    (world.data / "Filme" / "link.bin").symlink_to(outside)

    sizes = storage.compute_sizes(world.settings, usage=usage())

    assert sum(sizes["categories"].values()) == 0


def test_an_unreadable_folder_is_skipped_and_logged(world, monkeypatch, caplog):
    (world.data / "Filme" / "ok.bin").write_bytes(b"a" * 100)
    real_scandir = os.scandir

    def broken(folder):
        if str(folder).endswith("Filme"):
            raise PermissionError(13, "Permission denied", str(folder))
        return real_scandir(folder)

    monkeypatch.setattr(storage.os, "scandir", broken)
    with caplog.at_level(logging.WARNING, logger="ytp.storage"):
        sizes = storage.compute_sizes(world.settings, usage=usage())

    assert sizes["categories"]["movies"] == 0
    assert "cannot read a folder" in caplog.text


# --- the dashboard view -----------------------------------------------------------------------


def test_the_disk_view_keeps_free_live_and_has_no_sizes_without_a_cache(world, db):
    view = storage.disk_view(db, world.settings, usage=usage(total=1000, used=750, free=250))
    assert view is not None
    assert view["free_percent"] == 25
    assert (view["free_gb"], view["total_gb"]) == (0, 0), "the old fields are unchanged"
    assert (view["total_bytes"], view["free_bytes"]) == (1000, 250)
    assert view["categories"] is None
    assert (view["downloads"], view["other"], view["at"]) == (None, None, None)


def test_the_disk_view_adds_the_cached_sizes(world, db):
    set_state(
        db,
        storage.CACHE_KEY,
        json.dumps(
            {
                "at": "2026-11-10T14:05:00.000Z",
                "categories": {"movies": 10},
                "downloads": 3,
                "other": 4,
            }
        ),
    )
    view = storage.disk_view(db, world.settings, usage=usage(total=100, used=50, free=50))
    assert view["categories"] == {"movies": 10}
    assert (view["downloads"], view["other"]) == (3, 4)
    assert view["at"] == "2026-11-10T14:05:00.000Z"


@pytest.mark.parametrize("raw", ["not json", "[]", '{"categories": "no"}'])
def test_a_broken_cache_counts_as_no_cache(world, db, raw):
    set_state(db, storage.CACHE_KEY, raw)
    assert storage.cached(db) is None
    assert storage.disk_view(db, world.settings, usage=usage())["categories"] is None


def test_an_unreadable_disk_is_null(world, db):
    def broken(path):
        raise OSError("no such disk")

    assert storage.disk_view(db, world.settings, usage=broken) is None


# --- the sampler ------------------------------------------------------------------------------


def sizes() -> dict:
    return {
        "at": "2026-11-10T14:05:00.000Z",
        "categories": {kind: 0 for kind in CATEGORIES},
        "downloads": 0,
        "other": 0,
    }


def test_a_burst_of_requests_is_one_walk(world, db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        storage, "compute_sizes", lambda *args, **kwargs: calls.append(1) or sizes()
    )
    sampler = storage.StorageSampler(db, world.settings, debounce_seconds=0.05)
    for _ in range(5):
        sampler.request()
    sampler.start()
    try:
        assert wait_for(lambda: bool(calls))
    finally:
        sampler.stop()
    assert calls == [1], "five finishes in a row made one walk"
    assert storage.cached(db) is not None


def test_at_start_a_walk_runs_only_without_a_cache(world, db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        storage, "compute_sizes", lambda *args, **kwargs: calls.append(1) or sizes()
    )
    first = storage.StorageSampler(db, world.settings, debounce_seconds=0.05)
    first.ensure_cached()  # no cache yet
    first.start()
    try:
        assert wait_for(lambda: bool(calls))
    finally:
        first.stop()
    assert len(calls) == 1

    calls.clear()
    second = storage.StorageSampler(db, world.settings, debounce_seconds=0.05)
    second.ensure_cached()  # the cache exists: no walk
    second.start()
    time.sleep(0.2)
    second.stop()
    assert calls == []


def test_a_failed_walk_keeps_the_old_cache(world, db, monkeypatch):
    old = sizes()
    set_state(db, storage.CACHE_KEY, json.dumps(old))

    def broken(*args, **kwargs):
        raise RuntimeError("walk exploded")

    monkeypatch.setattr(storage, "compute_sizes", broken)
    sampler = storage.StorageSampler(db, world.settings, debounce_seconds=0.05)
    sampler.request()
    sampler.start()
    try:
        time.sleep(0.2)
    finally:
        sampler.stop()
    assert storage.cached(db) == old
