"""YouTube login cookies (#46): the admin stores a Netscape cookies.txt file.

YouTube does not allow a username/password login through yt-dlp any more. The only working way
to download age-restricted videos is a browser's cookies file (Netscape `cookies.txt` format),
handed to yt-dlp as `cookiefile`. **The cookies ARE the login**: a leaked file lets anybody act
as that YouTube account.

Rules this module enforces:

* A file is only accepted when it looks like a Netscape cookie file: the header line
  `# Netscape HTTP Cookie File` or `# HTTP Cookie File`, then tab-separated lines with 7 fields
  (domain, include-subdomains, path, secure, expiry, name, value). `#HttpOnly_<domain>` lines of
  a Chrome export are cookie lines too.
* Only cookies for `youtube.com` and `google.com` (and their subdomains) are kept: YouTube's
  login needs some google.com cookies too (`SID`, `HSID`, ...), and nothing else belongs there.
  At least one youtube.com cookie must be present.
* The stored file lives at `<DATA_ROOT>/secrets/youtube-cookies.txt`, the folder is mode 0700
  and the file 0600, written atomically (a temp file in the same folder, then `os.replace`).
* yt-dlp never gets the stored file itself: `private_copy` hands every run its own throwaway
  copy (mode 0600, deleted afterwards). yt-dlp truncates and rewrites `cookiefile` in place at
  the end of a run, and several runs share the login (the YouTube worker, parallel channel
  listings); the stored file is only ever written by the PUT endpoint.

Nothing here logs or raises the cookie text: `CookieFileError` messages name the problem, never
the content. The endpoints in `youtube_cookies_api.py` return only counts and times.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings

logger = logging.getLogger(__name__)

DIR_NAME = "secrets"
FILE_NAME = "youtube-cookies.txt"
MAX_BYTES = 1024 * 1024  # 1 MiB: a real cookies.txt is a few KiB
HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
HTTP_ONLY = "#HttpOnly_"
FIELDS = 7


class CookieFileError(ValueError):
    """The text is not a usable Netscape cookie file. The message is safe to show (no content)."""


def cookie_path(settings: Settings) -> Path:
    """Where the cookies file is stored (it need not exist)."""
    return settings.data_root / DIR_NAME / FILE_NAME


def cookie_file(settings: Settings) -> Path | None:
    """The stored file, or None when no cookies are stored (the downloader asks this)."""
    path = cookie_path(settings)
    return path if path.is_file() else None


def _copy_to_temp(path: Path) -> Path | None:
    """A new private copy of `path`, or None when it cannot be made (logged, no content)."""
    try:
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=".youtube-cookies-", suffix=".tmp")
        temp = Path(name)
        try:
            os.fchmod(handle, 0o600)  # mkstemp already creates 0600; be explicit
            with os.fdopen(handle, "wb") as target, path.open("rb") as source:
                shutil.copyfileobj(source, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(handle)  # already closed when os.fdopen or the with block got it
            temp.unlink(missing_ok=True)
            raise
        return temp
    except OSError:
        # Never the content, never the path (the folder is private, but still).
        logger.warning("youtube cookies could not be copied; this run has no cookies")
        return None


@contextlib.contextmanager
def private_copy(path: Path | None) -> Iterator[Path | None]:
    """A disposable private copy of the stored cookies file for ONE yt-dlp run (#46).

    yt-dlp saves its cookie jar back into `cookiefile` with mode 'w' at the end of every run
    (truncate, then write). Several yt-dlp runs share the stored file (the YouTube worker and up
    to two channel listings), so handing it over directly would let one run read another's
    half-written file, and a crash mid-write could empty the admin's login. Each run therefore
    gets its own copy (mode 0600, in the private 0700 secrets folder), which is deleted
    afterwards, also when the run raises. The stored file is only ever written by the PUT
    endpoint.

    Yields None when no cookies are stored (the "only when the file exists" behaviour) or when
    the copy cannot be made: that run simply goes on without cookies.
    """
    if path is None or not path.is_file():
        yield None
        return
    temp = _copy_to_temp(path)
    if temp is None:
        yield None
        return
    try:
        yield temp
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def _domain_of(line: str) -> str:
    """The domain field of a cookie line, without a leading dot and lower-case."""
    return line.split("\t", 1)[0].lstrip(".").lower()


def _wanted(domain: str) -> bool:
    """A youtube.com or google.com cookie (the login needs both); nothing else is kept."""
    return (
        domain == "youtube.com"
        or domain.endswith(".youtube.com")
        or domain == "google.com"
        or domain.endswith(".google.com")
    )


def _is_youtube(domain: str) -> bool:
    return domain == "youtube.com" or domain.endswith(".youtube.com")


def _cookie_line(line: str) -> bool:
    """True for a stored cookie line (also a Chrome `#HttpOnly_` one), False for comments."""
    body = line[len(HTTP_ONLY) :] if line.startswith(HTTP_ONLY) else line
    return bool(body.strip()) and not body.startswith("#") and len(body.split("\t")) == FIELDS


def parse(text: str) -> list[str]:
    """The cookie lines to store, or `CookieFileError`.

    The header must come first; malformed cookie lines are refused (the admin can export the
    file again). Foreign domains and comments are dropped; a file without a youtube.com cookie
    is refused. The returned lines keep their original text, including a `#HttpOnly_` prefix.
    """
    if not isinstance(text, str):
        raise CookieFileError("the cookies file must be text")
    normalized = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    start = next((index for index, line in enumerate(lines) if line.strip()), None)
    if start is None or lines[start].strip() not in HEADERS:
        raise CookieFileError(
            "the file does not start with the Netscape cookies header "
            "('# Netscape HTTP Cookie File' or '# HTTP Cookie File')"
        )
    kept: list[str] = []
    has_youtube = False
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        body = line[len(HTTP_ONLY) :] if line.startswith(HTTP_ONLY) else line
        if body.startswith("#"):
            continue  # a comment (or another header line)
        if len(body.split("\t")) != FIELDS:
            raise CookieFileError(
                "a line is not in the tab-separated Netscape format (7 fields); "
                "export the cookies again"
            )
        domain = _domain_of(body)
        if not _wanted(domain):
            continue
        if _is_youtube(domain):
            has_youtube = True
        kept.append(line)
    if not has_youtube:
        raise CookieFileError("the file has no youtube.com cookie")
    return kept


def save(settings: Settings, text: str) -> int:
    """Validate and write the file atomically; returns how many cookies were stored."""
    kept = parse(text)
    path = cookie_path(settings)
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    content = "\n".join((HEADERS[0], *kept)) + "\n"
    handle, temp_name = tempfile.mkstemp(dir=folder, prefix=".youtube-cookies-", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as open_file:
            os.chmod(temp, 0o600)
            open_file.write(content)
            open_file.flush()
            os.fsync(open_file.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return len(kept)


def remove(settings: Settings) -> bool:
    """Delete the stored file; True if there was one."""
    path = cookie_path(settings)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _count(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if _cookie_line(line))


def status(settings: Settings) -> dict[str, object]:
    """What the API may tell: presence, the file time and how many cookies, never contents."""
    path = cookie_path(settings)
    try:
        info = path.stat()
    except OSError:
        return {"present": False, "updated_at": None, "cookie_count": 0}
    return {
        "present": True,
        "updated_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds"),
        "cookie_count": _count(path),
    }
