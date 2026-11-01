"""Writing tags and a cover picture into a downloaded audio file (issues #35, #36).

yt-dlp's audio-only download gives `<id>.m4a` (AAC) or `<id>.opus` with YouTube's own metadata
inside, which is not what the owner confirmed in the form. Jellyfin's music library reads the
tags in the files (artist, album, track title, year), so the confirmed values are written there
with ffmpeg, **copying the audio stream** (no re-encoding, no quality loss):

    ffmpeg -i in.m4a [-i cover.jpg] -map 0:a [-map 1:v] -c copy [-disposition:v:0 attached_pic]
           -metadata title=... -metadata artist=... out.m4a

* A cover picture is attached for `.m4a`, `.mp4` and `.mp3`. Ogg/Opus has no simple attached
  picture (it needs an encoded block), so an `.opus` file gets tags only; a `folder.jpg` in the
  library folder is how such a file gets a cover in Jellyfin.
* Everything is passed as one argument per value to ffmpeg (no shell), and the files are named by
  the video id inside our own work folder, so no user text decides a path. A tag value cannot
  start an option because it is always the part after `key=`.
* **Tagging is best effort.** A missing ffmpeg, an error or a timeout leaves the untagged file as
  it was; the download is still good and the job still succeeds. The caller gets `False` and may
  log it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

COVER_EXTENSIONS = frozenset({".m4a", ".mp4", ".mp3"})
TAGGABLE_EXTENSIONS = frozenset({".m4a", ".mp4", ".mp3", ".opus", ".ogg"})
TIMEOUT_SECONDS = 600
MAX_TAG_LENGTH = 1000


@dataclass(frozen=True)
class Tags:
    title: str
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    published: date | None = None
    genre: str = ""
    comment: str = ""

    def pairs(self) -> list[tuple[str, str]]:
        """The ffmpeg metadata keys with their (cleaned, shortened) values; empty ones left out."""
        values = [
            ("title", self.title),
            ("artist", self.artist),
            ("album", self.album),
            ("album_artist", self.album_artist),
            ("date", self.published.isoformat() if self.published else ""),
            ("genre", self.genre),
            ("comment", self.comment),
        ]
        return [(key, _clean(value)) for key, value in values if _clean(value)]


def _clean(value: str) -> str:
    """One line of text without control characters, at most MAX_TAG_LENGTH characters."""
    text = "".join(" " if ch.isspace() or not ch.isprintable() else ch for ch in str(value))
    return " ".join(text.split())[:MAX_TAG_LENGTH]


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def tag_command(source: Path, target: Path, tags: Tags, cover: Path | None) -> list[str]:
    """The ffmpeg command line (one argument per item; the caller runs it without a shell)."""
    extension = source.suffix.lower()
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(source)]
    with_cover = cover is not None and extension in COVER_EXTENSIONS
    if with_cover:
        command += ["-i", str(cover), "-map", "0:a", "-map", "1:v"]
        command += ["-disposition:v:0", "attached_pic"]
        if extension == ".mp3":
            command += [
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
    else:
        command += ["-map", "0:a"]
    command += ["-map_metadata", "-1", "-c", "copy"]
    if extension == ".mp3":
        command += ["-id3v2_version", "3"]
    for key, value in tags.pairs():
        command += ["-metadata", f"{key}={value}"]
    command.append(str(target))
    return command


def embed_tags(
    path: Path,
    tags: Tags,
    cover: Path | None = None,
    *,
    runner: Runner = subprocess.run,
) -> bool:
    """Write the tags (and the cover where the format allows) into `path`, replacing it.

    Returns True if the file was tagged, False if it was left as it was (unsupported format, ffmpeg
    missing or failed). Never raises.
    """
    if path.suffix.lower() not in TAGGABLE_EXTENSIONS or not path.is_file():
        return False
    target = path.with_name(f"{path.stem}.tagged{path.suffix}")
    target.unlink(missing_ok=True)
    try:
        result = runner(
            tag_command(path, target, tags, cover),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        ok = result.returncode == 0 and target.is_file() and target.stat().st_size > 0
        if not ok:
            logger.warning("tagging failed", extra={"returncode": result.returncode})
            target.unlink(missing_ok=True)
            return False
        os.replace(target, path)
        return True
    except (OSError, subprocess.SubprocessError):  # no ffmpeg, a timeout, a full disk
        logger.warning("tagging failed", exc_info=True)
        target.unlink(missing_ok=True)
        return False
