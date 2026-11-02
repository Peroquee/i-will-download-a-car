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

**Audio only** (issues #35, #36): `audio_only=True` asks yt-dlp for the best audio stream and keeps
it as it is. YouTube serves AAC in an mp4 container and Opus in webm; yt-dlp's
`FFmpegExtractAudio` with `preferredcodec: best` copies the audio into `.m4a` (AAC) or `.opus`
(Opus) **without re-encoding**: no quality loss and no CPU. AAC (m4a) is preferred because every
player, phone and Jellyfin client plays it and it can carry a cover picture and tags; Opus is the
fallback when no AAC stream exists. Nothing is ever converted to mp3 (that would be a lossy
re-encode of a lossy stream). Tags and the cover are added afterwards by `ytp.audiotags`.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .youtube import ProbeError, VideoRef, _SilentLogger, classify

logger = logging.getLogger(__name__)

MIN_FREE_BYTES = 2 * 1024**3  # refuse to start with less than 2 GiB free
MAX_NAME_BYTES = 255
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")
AUDIO_EXTENSIONS = (".m4a", ".opus", ".ogg", ".aac", ".mp3")
AUDIO_FORMAT = "ba[ext=m4a]/ba[acodec^=mp4a]/ba"  # AAC first, then whatever audio is best
THUMBNAIL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
INFO_KEYS = (
    "id", "title", "channel", "channel_id", "uploader", "uploader_id", "duration",
    "upload_date", "description", "tags", "categories", "width", "height", "fps",
)  # fmt: skip


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
) -> dict[str, Any]:
    """The yt-dlp options for a download (fixed in code; pinned by tests)."""
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
    return options


def _download_with_ytdlp(url: str, options: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
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
        socket_timeout: float = 20.0,
        download: Callable[[str, dict[str, Any]], dict[str, Any]] = _download_with_ytdlp,
        min_free_bytes: int = MIN_FREE_BYTES,
    ):
        self.proxy = proxy
        self.max_height = max_height
        self._socket_timeout = socket_timeout
        self._download = download
        self._min_free = min_free_bytes

    def download(
        self,
        ref: VideoRef,
        workdir: Path,
        progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        audio_only: bool = False,
    ) -> Downloaded:
        """Download into `workdir` (created fresh). Raises ProbeError or DownloadFailed."""
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        free = shutil.disk_usage(workdir).free
        if free < self._min_free:
            raise DownloadFailed(
                f"not enough free space on the media volume ({free // 1024**2} MiB left, "
                f"{self._min_free // 1024**2} MiB needed to start)"
            )
        options = download_options(
            workdir,
            proxy=self.proxy,
            max_height=self.max_height,
            socket_timeout=self._socket_timeout,
            progress_hook=progress,
            audio_only=audio_only,
        )
        try:
            info = self._download(ref.url, options)
        except (ProbeError, DownloadFailed):
            raise
        except OSError as exc:
            raise DownloadFailed(f"could not write the download: {exc.strerror or exc}") from exc
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
