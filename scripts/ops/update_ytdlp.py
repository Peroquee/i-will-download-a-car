#!/usr/bin/env python3
"""update_ytdlp.py: is the pinned yt-dlp still current, and bump the pin on request (issue #30).

YouTube changes often and yt-dlp releases a fix within days; a stale yt-dlp is the usual reason
downloads or the probe suddenly fail for every video (`kind` unknown, see docs/youtube.md). The
version is pinned in `app/requirements.txt` so builds are reproducible. This script compares the
pin with the newest release on PyPI:

    scripts/ops/update_ytdlp.py            # report; exit 0 = current, 10 = a newer release exists
    scripts/ops/update_ytdlp.py --apply    # rewrite the pin (only that line), then say what to run
    scripts/ops/update_ytdlp.py --apply --dry-run   # show the change, write nothing

It never builds or restarts anything: a new yt-dlp is a deliberate change that goes through the
tests and a rebuild (`--apply` prints the steps). Exit code 2 = it could not tell
(no network, unexpected answer). Safe to re-run; with the pin already current `--apply` does
nothing. Only the standard library is used.

Test hook: `--requirements` points at another file, `--index-url` at another JSON source.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = REPO_ROOT / "app" / "requirements.txt"
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
PIN = re.compile(r"^(yt-dlp==)(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)\s*$", re.MULTILINE)
VERSION = re.compile(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?")
NEXT_STEPS = """next steps (on the machine with the checkout):
  1. make test-unit                       the tests that pin yt-dlp's options and error texts
  2. YTP_NETWORK_TESTS=1 make test-system   the real-YouTube tests (probe, download, channels)
  3. git add app/requirements.txt && git commit, push, merge as for any change
  4. docker compose build app && docker compose up -d app   (the watchdog uses the same image:
     docker compose up -d watchdog)
Roll back by putting the old line back and rebuilding."""


class UpdateError(Exception):
    pass


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def pinned_version(text: str) -> str:
    match = PIN.search(text)
    if not match:
        raise UpdateError("no line like 'yt-dlp==2026.10.26' in the requirements file")
    return match.group(2)


def latest_release(index_url: str, timeout: float = 20.0) -> tuple[str, datetime | None]:
    """(newest stable version, its upload time) from PyPI's JSON; raises UpdateError."""
    request = urllib.request.Request(index_url, headers={"User-Agent": "youtube-premium-ops"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"could not read {index_url}: {exc}") from exc
    try:
        releases = data["releases"]
        candidates = [
            v
            for v, files in releases.items()
            if VERSION.fullmatch(v) and files and not all(f.get("yanked") for f in files)
        ]
        newest = max(candidates, key=version_key)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise UpdateError("PyPI answered in an unexpected shape") from exc
    uploaded = None
    try:
        stamp = releases[newest][0]["upload_time_iso_8601"]
        uploaded = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return newest, uploaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check (and bump) the pinned yt-dlp version.")
    parser.add_argument(
        "--apply", action="store_true", help="rewrite the pin to the newest release"
    )
    parser.add_argument("--dry-run", action="store_true", help="with --apply: show, write nothing")
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--index-url", default=os.environ.get("YTP_PYPI_JSON", PYPI_URL))
    args = parser.parse_args(argv)

    try:
        text = args.requirements.read_text(encoding="utf-8")
        current = pinned_version(text)
        newest, uploaded = latest_release(args.index_url)
    except (UpdateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    age = ""
    if uploaded is not None:
        days = (datetime.now(UTC) - uploaded).days
        age = f", released {days} days ago"
    if version_key(newest) <= version_key(current):
        print(f"yt-dlp is current: pinned {current}, newest {newest}{age}")
        return 0
    print(f"yt-dlp can be updated: pinned {current}, newest {newest}{age}")
    if not args.apply:
        print("run with --apply to change the pin (then follow the next steps it prints)")
        return 10
    updated = PIN.sub(lambda m: f"{m.group(1)}{newest}", text, count=1)
    print(f"  {args.requirements.name}: yt-dlp=={current} -> yt-dlp=={newest}")
    if args.dry_run:
        print("dry run: nothing was written")
        return 10
    temporary = args.requirements.with_name(args.requirements.name + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(args.requirements)
    print(NEXT_STEPS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
