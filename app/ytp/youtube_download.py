"""Downloading one YouTube video and filing it into the library (issue #18).

yt-dlp (as a library, like the probe in `ytp.youtube`) downloads into a private work folder,
`downloads/incomplete/youtube/<video id>/`, which Jellyfin never sees. Only when everything is
complete are the files moved (same file system, so a rename) into

    <DATA_ROOT>/<MEDIA_DIR_YOUTUBE>/<channel>/<title> [<video id>].mp4
                                              /<title> [<video id>].jpg        (the thumbnail)
                                              /<title> [<video id>].info.json  (a small summary)

The names come from the channel and title the user confirmed, passed through
`safe_path_component`, never from yt-dlp's template: inside the work folder the files are named
by the video id, so nothing untrusted decides a path until we build it ourselves.

* **Format:** prefer H.264 video with AAC audio in an mp4 (plays everywhere, Jellyfin can
  direct-play it), at most `max_height` pixels high (default 1080; 0 = no limit). Needs ffmpeg to
  merge YouTube's separate video and audio streams: it is installed in the image.
* **`.info.json`:** yt-dlp's own file carries signed stream addresses with the downloader's IP
  address in them, which must not sit in a library Jellyfin reads. We write a small summary of
  the harmless fields instead (title, channel, duration, upload date, description, tags).
* A failure never leaves a partial file in the library; the work folder is removed either way.
* **Bounded (#70):** a download that brings no new bytes for 10 minutes, runs longer than 6
  hours, or leaves an ffmpeg helper running for 15 minutes fails with a readable reason instead
  of holding the single download worker forever.

**Audio only** (issues #35, #36): `audio_only=True` asks yt-dlp for the best audio stream and keeps
it as it is. YouTube serves AAC in an mp4 container and Opus in webm; yt-dlp's
`FFmpegExtractAudio` with `preferredcodec: best` copies the audio into `.m4a` (AAC) or `.opus`
(Opus) **without re-encoding**: no quality loss and no CPU. AAC (m4a) is preferred because every
player, phone and Jellyfin client plays it and it can carry a cover picture and tags; Opus is the
fallback when no AAC stream exists. Nothing is ever converted to mp3 (that would be a lossy
re-encode of a lossy stream). Tags and the cover are added afterwards by `ytp.audiotags`.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import youtube_cookies
from .youtube import ProbeError, VideoRef, _SilentLogger, classify

logger = logging.getLogger(__name__)

MIN_FREE_BYTES = 2 * 1024**3  # refuse to start with less than 2 GiB free
MAX_NAME_BYTES = 255
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")
AUDIO_EXTENSIONS = (".m4a", ".opus", ".ogg", ".aac", ".mp3")
AUDIO_FORMAT = "ba[ext=m4a]/ba[acodec^=mp4a]/ba"  # AAC first, then whatever audio is best
THUMBNAIL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
# Bounds on one download (#70): yt-dlp's socket_timeout only limits one read, so a trickling
# stream or a hung ffmpeg merge would hold the single download worker forever. No new bytes
# for STALL_SECONDS fails the job; DEADLINE_SECONDS caps the whole download; every helper
# subprocess (ffmpeg) is killed after SUBPROCESS_TIMEOUT_SECONDS.
STALL_SECONDS = 600.0
DEADLINE_SECONDS = 6 * 3600.0
SUBPROCESS_TIMEOUT_SECONDS = 900.0
# The resolutions the input form offers and the API accepts live in `ytp.resolution` (#47, #65).
INFO_KEYS = (
    "id", "title", "channel", "channel_id", "uploader", "uploader_id", "duration",
    "upload_date", "description", "tags", "categories", "width", "height", "fps",
)  # fmt: skip
# Shown when an age-restricted download has no cookies to try (#46).
AGE_COOKIE_HINT = (
    " An admin can add YouTube cookies in the settings to download age-restricted videos."
)


class DownloadFailed(Exception):
    """A download problem with a message a person can read (it becomes the job's error)."""


def format_selector(max_height: int) -> str:
    """yt-dlp's format expression: H.264 + AAC in mp4 first, then anything reasonable."""
    cap = f"[height<={max_height}]" if max_height > 0 else ""
    return (
        f"bv*[vcodec^=avc1]{cap}+ba[ext=m4a]/bv*[ext=mp4]{cap}+ba[ext=m4a]/"
        f"b[ext=mp4]{cap}/bv*{cap}+ba/b{cap}"
    )


def download_options(
    workdir: Path,
    *,
    proxy: str,
    max_height: int,
    socket_timeout: float,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    audio_only: bool = False,
    cookie_file: Path | None = None,
) -> dict[str, Any]:
    """The yt-dlp options for a download (fixed in code; pinned by tests).

    `cookie_file` (#46) is passed only when it exists: the admin's YouTube login cookies.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "allowed_extractors": ["youtube"],
        "cachedir": False,
        "enable_file_urls": False,
        "geo_bypass": False,
        "color": "never",
        "logger": _SilentLogger(),
        "socket_timeout": socket_timeout,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 1,
        "format": format_selector(max_height),
        "merge_output_format": "mp4",
        # Named by the video id, inside a folder only we use; the real names are ours to build.
        "outtmpl": {"default": "%(id)s.%(ext)s"},
        "paths": {"home": str(workdir), "temp": str(workdir)},
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
        ],
        "writeinfojson": False,  # we write a cleaned summary ourselves
        "overwrites": False,
        "continuedl": False,
        "progress_hooks": [progress_hook] if progress_hook else [],
        **({"proxy": proxy} if proxy else {}),
    }
    if audio_only:
        options["format"] = AUDIO_FORMAT
        del options["merge_output_format"]
        options["postprocessors"] = [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            # "best" = copy the stream into its natural container (m4a for AAC, opus for Opus).
            {"key": "FFmpegExtractAudio", "preferredcodec": "best"},
        ]
    # Only if the admin stored a cookies file (#46): yt-dlp may rewrite it while it runs.
    if cookie_file is not None and cookie_file.is_file():
        options["cookiefile"] = str(cookie_file)
    return options


def _minutes_text(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def _hours_text(seconds: float) -> str:
    hours = max(1.0, round(seconds / 3600, 1))
    text = f"{hours:g}"
    return f"{text} hour" if hours == 1 else f"{text} hours"


class ProgressWatchdog:
    """The job's progress hook, with a stall and an overall limit around it (#70).

    Raising inside a yt-dlp progress hook aborts the download; yt-dlp does not swallow it.
    Progress is counted PER FILE: yt-dlp downloads video and audio as two files (`bv+ba`) and
    `downloaded_bytes` starts at 0 again for the second, so a global maximum would treat a
    slow audio part as a stall while data is arriving. Any change of a file's byte count
    counts as progress; only a callback that brings no change after `stall_seconds` fails.
    """

    def __init__(
        self,
        forward: Callable[[dict[str, Any]], None] | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        stall_seconds: float = STALL_SECONDS,
        deadline_seconds: float = DEADLINE_SECONDS,
    ):
        self._forward = forward
        self._clock = clock
        self._stall = stall_seconds
        self._deadline = deadline_seconds
        self._started = clock()
        self._last_change = self._started
        self._seen: dict[str, int] = {}

    def __call__(self, data: dict[str, Any]) -> None:
        now = self._clock()
        downloaded = data.get("downloaded_bytes")
        if isinstance(downloaded, int):
            name = data.get("filename")
            key = name if isinstance(name, str) else ""
            if self._seen.get(key) != downloaded:
                self._seen[key] = downloaded
                self._last_change = now
        if now - self._started > self._deadline:
            raise DownloadFailed(
                f"the download did not finish within {_hours_text(self._deadline)}"
            )
        if now - self._last_change > self._stall:
            raise DownloadFailed(
                f"the download stalled: no data arrived for {_minutes_text(self._stall)}"
            )
        if self._forward is not None:
            self._forward(data)


@contextlib.contextmanager
def popen_timeout(seconds: float) -> Iterator[None]:
    """Give every subprocess yt-dlp starts a timeout (#70).

    yt-dlp's `Popen.run` accepts a timeout, but its ffmpeg postprocessors never pass one; a
    hung merge would otherwise keep the single YouTube worker busy forever.

    This patches yt-dlp's class PROCESS-WIDE, and nested or concurrent use would restore the
    wrong function. That is fine here: only the one YouTube worker thread downloads at a time.
    """
    from yt_dlp.utils import Popen

    original = Popen.run.__func__

    @functools.wraps(original)
    def run_with_timeout(cls, *args, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return original(cls, *args, **kwargs)

    Popen.run = classmethod(run_with_timeout)
    try:
        yield
    finally:
        Popen.run = classmethod(original)


def _download_with_ytdlp(url: str, options: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    try:
        with popen_timeout(SUBPROCESS_TIMEOUT_SECONDS), yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise classify(str(exc)) from exc
    if not isinstance(info, dict):
        raise DownloadFailed("YouTube returned nothing for this video.")
    return info


def summary(info: dict[str, Any], ref: VideoRef) -> dict[str, Any]:
    """The harmless fields of yt-dlp's answer, for the `.info.json` next to the video."""
    result: dict[str, Any] = {key: info[key] for key in INFO_KEYS if info.get(key) is not None}
    result["id"] = ref.video_id
    result["webpage_url"] = ref.url
    if isinstance(result.get("description"), str):
        result["description"] = result["description"][:5000]
    if isinstance(result.get("tags"), list):
        result["tags"] = [str(tag)[:100] for tag in result["tags"][:50]]
    result["saved_by"] = "youtube-premium"
    return result


def upload_date(info: dict[str, Any]) -> date | None:
    """The video's upload date from yt-dlp's `YYYYMMDD`, or None."""
    text = str(info.get("upload_date") or "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


@dataclass(frozen=True)
class Downloaded:
    video: Path  # the main file: the video, or the audio file for audio_only
    thumbnail: Path | None
    info: dict[str, Any]
    audio_only: bool = False


class YoutubeDownloader:
    def __init__(
        self,
        *,
        proxy: str = "",
        max_height: int = 1080,
        max_height_source: Callable[[], int] | None = None,
        socket_timeout: float = 20.0,
        download: Callable[[str, dict[str, Any]], dict[str, Any]] = _download_with_ytdlp,
        min_free_bytes: int = MIN_FREE_BYTES,
        cookie_file: Path | None = None,
        stall_seconds: float = STALL_SECONDS,
        deadline_seconds: float = DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.proxy = proxy
        self.max_height = max_height
        # The server-wide default can change while the app runs (#79): ask for it at each download.
        self._max_height_source = max_height_source
        self._socket_timeout = socket_timeout
        self._download = download
        self._min_free = min_free_bytes
        self.cookie_file = cookie_file  # the admin's YouTube login cookies (#46)
        self._stall = stall_seconds
        self._deadline = deadline_seconds
        self._clock = clock

    def _default_height(self) -> int:
        return self.max_height if self._max_height_source is None else self._max_height_source()

    def download(
        self,
        ref: VideoRef,
        workdir: Path,
        progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        audio_only: bool = False,
        max_height: int | None = None,
    ) -> Downloaded:
        """Download into `workdir` (created fresh). Raises ProbeError or DownloadFailed.

        `max_height` overrides this downloader's default for one job (0 = no limit); None means
        the configured default, so jobs created before #47 keep their behaviour.
        """
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        free = shutil.disk_usage(workdir).free
        if free < self._min_free:
            raise DownloadFailed(
                f"not enough free space on the media volume ({free // 1024**2} MiB left, "
                f"{self._min_free // 1024**2} MiB needed to start)"
            )
        # Each run gets its own throwaway copy: yt-dlp truncates and rewrites the cookiefile
        # in place, and several runs share the stored login (#46 follow-up).
        with youtube_cookies.private_copy(self.cookie_file) as cookie_copy:
            watchdog = ProgressWatchdog(
                progress,
                clock=self._clock,
                stall_seconds=self._stall,
                deadline_seconds=self._deadline,
            )
            options = download_options(
                workdir,
                proxy=self.proxy,
                max_height=self._default_height() if max_height is None else max_height,
                socket_timeout=self._socket_timeout,
                progress_hook=watchdog,
                audio_only=audio_only,
                cookie_file=cookie_copy,
            )
            try:
                info = self._download(ref.url, options)
            except ProbeError as exc:
                if exc.kind == "age" and cookie_copy is None:
                    # Tell the owner how to fix it; the message names no cookie content (#46).
                    raise ProbeError(exc.kind, exc.message + AGE_COOKIE_HINT) from exc
                raise
            except DownloadFailed:
                raise
            except subprocess.TimeoutExpired as exc:
                raise DownloadFailed(
                    "a helper program did not finish within "
                    f"{_minutes_text(SUBPROCESS_TIMEOUT_SECONDS)}"
                ) from exc
            except OSError as exc:
                raise DownloadFailed(
                    f"could not write the download: {exc.strerror or exc}"
                ) from exc
            except Exception as exc:  # a bug or odd failure in yt-dlp must end as a failed job
                logger.exception("yt-dlp download crashed", extra={"video_id": ref.video_id})
                raise DownloadFailed(f"the download failed: {type(exc).__name__}") from exc
        media = _find(workdir, ref.video_id, AUDIO_EXTENSIONS if audio_only else VIDEO_EXTENSIONS)
        if media is None or media.stat().st_size == 0:
            raise DownloadFailed(
                "yt-dlp finished without producing "
                + ("an audio file" if audio_only else "a video file")
            )
        return Downloaded(
            media,
            _find(workdir, ref.video_id, THUMBNAIL_EXTENSIONS),
            summary(info, ref),
            audio_only,
        )


def _find(folder: Path, video_id: str, extensions: tuple[str, ...]) -> Path | None:
    for ext in extensions:
        candidate = folder / f"{video_id}{ext}"
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


# ---------------------------------------------------------------------------------- filing


def file_stem(title: str, video_id: str, longest_suffix: str = ".info.json") -> str:
    """`<title> [<id>]`, with the title shortened if the longest file name would not fit."""
    tail = f" [{video_id}]"
    room = MAX_NAME_BYTES - len((tail + longest_suffix).encode())
    shortened = title
    while len(shortened.encode()) > room:
        shortened = shortened[:-1]
    return shortened.rstrip() + tail


def place(
    downloaded: Downloaded, library: Path, channel: str, title: str, video_id: str
) -> list[Path]:
    """Move the finished files into `<library>/<channel>/`; returns the final paths.

    The video goes last, so a file that looks complete is complete. Existing targets are never
    overwritten: a video already filed under this id is simply left as it is.
    """
    folder = library / channel
    library_root = library.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(library_root):
        raise DownloadFailed("the channel folder would be outside the library")
    stem = file_stem(title, video_id)
    moves: list[tuple[Path, Path]] = []
    if downloaded.thumbnail is not None:
        moves.append((downloaded.thumbnail, folder / f"{stem}.jpg"))
    info_path = downloaded.video.with_name(f"{video_id}.info.json")
    info_path.write_text(
        json.dumps(downloaded.info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    moves.append((info_path, folder / f"{stem}.info.json"))
    moves.append((downloaded.video, folder / f"{stem}{downloaded.video.suffix.lower()}"))
    placed = []
    for source, target in moves:
        if target.exists() or target.is_symlink():
            logger.info("target exists, leaving it", extra={"target": target.name})
            placed.append(target)
            continue
        try:
            os.replace(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise DownloadFailed(f"could not file {target.name[:60]}: {exc.strerror}") from exc
            shutil.move(str(source), str(target))
        placed.append(target)
    return placed
