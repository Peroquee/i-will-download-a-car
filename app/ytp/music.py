"""Filing a YouTube track into the music library (issue #36).

    <DATA_ROOT>/<MEDIA_DIR_MUSIC>/<artist>/<album>/<title>.<ext>

the layout the torrent organizer uses for music (`organizer.py`: `Musik/Artist/Album/files`), so a
track from YouTube and an album from a torrent sit side by side, and Jellyfin's music library reads
both. Artist, album and title are the names the user confirmed, already through
`safe_path_component`; the title is shortened if the file name would be too long. Nothing is
overwritten (`file_into`).
"""

from __future__ import annotations

from pathlib import Path

from .metadata import sanitize_name
from .podcasts import MAX_NAME_BYTES, file_into


def track_filename(title: str, extension: str) -> str:
    """`<title>.<ext>`, the title shortened so the name (with room for ` (99)`) fits."""
    room = MAX_NAME_BYTES - len((extension + " (99)").encode())
    shortened = sanitize_name(title, "Track")
    while len(shortened.encode()) > room:
        shortened = shortened[:-1]
    return f"{shortened.rstrip() or 'Track'}{extension}"


def place_track(
    source: Path, library: Path, artist: str, album: str, title: str, extension: str
) -> Path:
    """Move a finished download into `<library>/<artist>/<album>/` and return the final path."""
    return file_into(source, library, (artist, album), track_filename(title, extension))
