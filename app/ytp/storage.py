"""The dashboard's storage bar: category sizes, computed rarely and cached (#82).

The media drive holds the six libraries (movies, series, audiobooks, music, podcasts, YouTube)
and the downloads folder. Downloads are HARDLINKED into the libraries, so one file can appear
in two places; a plain `du` per folder would count it twice. This walk counts every inode ONCE:
the library folders first (a file that exists in a library belongs to that category), then the
downloads folder (files that exist only there are the "downloads" segment). Everything else on
the drive is "other" (`used` minus what was counted; backups, other files, ...).

**The walk is expensive** (terabytes of files), so it never runs while the dashboard is
polled: a background thread computes it when a download finishes (debounced: a burst of
finished jobs is one walk) and at app start if no cached answer exists yet. The dashboard
reads the cache and shows when it was taken; free and total stay live.
`shutil.disk_usage` is cheap and read on every request.

A folder that cannot be read is skipped and logged; the dashboard never fails because of it.
The cache lives in `app_state`, so it survives a restart.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, get_state, set_state
from .timeutil import now_text

logger = logging.getLogger(__name__)

CACHE_KEY = "storage.sizes"
DEBOUNCE_SECONDS = 5.0  # a burst of finished downloads is one walk


def _tree_size(root: Path, seen: set[tuple[int, int]]) -> int:
    """Bytes of the regular files below `root` that were not counted yet.

    Symlinks are never followed (a library is not a way to count the rest of the disk twice).
    An unreadable folder or entry is logged and skipped, never raised.
    """
    if not root.is_dir():
        return 0
    total = 0
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            inode = (stat.st_dev, stat.st_ino)
                            if inode in seen:  # the download this library file links to
                                continue
                            seen.add(inode)
                            total += stat.st_size
                    except OSError:
                        logger.warning("storage: cannot read an entry", extra={"path": entry.path})
        except OSError:
            logger.warning("storage: cannot read a folder", extra={"path": str(folder)})
    return total


def compute_sizes(
    settings: Settings,
    *,
    usage: Callable[[Path], Any] = shutil.disk_usage,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """The category sizes for the cache, each inode counted once (#82)."""
    started = now()
    seen: set[tuple[int, int]] = set()
    categories: dict[str, int] = {}
    for kind in settings.library_dirs:
        categories[kind] = _tree_size(settings.library_path(kind), seen)
    downloads = _tree_size(settings.downloads_dir, seen)
    try:
        used = int(usage(settings.data_root).used)
    except OSError:
        logger.warning(
            "storage: cannot read the disk usage", extra={"path": str(settings.data_root)}
        )
        used = sum(categories.values()) + downloads  # "other" unknown: do not invent it
    other = max(0, used - sum(categories.values()) - downloads)
    return {
        "at": now_text(started),
        "categories": categories,
        "downloads": downloads,
        "other": other,
    }


def cached(db: Database) -> dict[str, Any] | None:
    """The cached sizes, or None when nothing has been computed yet (or it is unreadable)."""
    raw = get_state(db, CACHE_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("categories"), dict):
        return None
    return value


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def disk_view(
    db: Database, settings: Settings, *, usage: Callable[[Path], Any] = shutil.disk_usage
) -> dict[str, Any] | None:
    """The dashboard's `disk` answer: live free/total plus the cached category sizes (#82).

    The existing fields (`free_percent`, `free_gb`, `total_gb`) stay exactly as they were; the
    bytes are added for the bar. Without a cache the size fields are null and the page says
    that they are being calculated.
    """
    try:
        usage_now = usage(settings.data_root)
    except OSError:
        return None
    if not usage_now.total:
        return None
    view: dict[str, Any] = {
        "free_percent": round(usage_now.free * 100 / usage_now.total),
        "free_gb": round(usage_now.free / 1e9),
        "total_gb": round(usage_now.total / 1e9),
        "total_bytes": usage_now.total,
        "free_bytes": usage_now.free,
        "categories": None,
        "downloads": None,
        "other": None,
        "at": None,
    }
    sizes = cached(db)
    if sizes is not None:
        view["categories"] = {
            kind: size
            for kind, size in sizes["categories"].items()
            if isinstance(kind, str) and _count(size) is not None
        }
        view["downloads"] = _count(sizes.get("downloads"))
        view["other"] = _count(sizes.get("other"))
        at = sizes.get("at")
        view["at"] = at if isinstance(at, str) else None
    return view


class StorageSampler:
    """Computes the category sizes in the background and caches them (#82).

    `request()` is what a finished download calls: the thread waits `debounce_seconds` first,
    so a burst of finishes is one walk. `ensure_cached()` runs at app start and only computes
    when no cached answer exists yet. A failed walk is logged and the old cache stays.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        usage: Callable[[Path], Any] = shutil.disk_usage,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._db = db
        self._settings = settings
        self._usage = usage
        self._debounce = debounce_seconds
        self._now = now
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="storage", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(5)

    def request(self) -> None:
        """A download finished: recompute soon. Never raises, never waits."""
        self._wake.set()

    def ensure_cached(self) -> None:
        """At app start: compute only when there is no cached answer yet."""
        if cached(self._db) is None:
            self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                return
            if self._stop.wait(self._debounce):  # let a burst of finishes arrive
                return
            self._wake.clear()
            self._compute()

    def _compute(self) -> None:
        try:
            sizes = compute_sizes(self._settings, usage=self._usage, now=self._now)
        except Exception:  # a storage walk must never kill the thread
            logger.exception("storage: sizes could not be computed")
            return
        try:
            set_state(self._db, CACHE_KEY, json.dumps(sizes))
        except Exception:
            logger.exception("storage: sizes could not be cached")
            return
        logger.info("storage: sizes computed", extra={"categories": len(sizes["categories"])})
