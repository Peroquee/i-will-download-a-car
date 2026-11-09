"""The newest uploads of a channel, listed with yt-dlp (instead of the RSS feed).

YouTube's RSS feed (`feeds/videos.xml?channel_id=...`) answers 404 for whole IP ranges and at
times for everybody (from about 2026-11-04 for all of this installation's channels, and public
reports since December 2025). The poller therefore asks yt-dlp first, with the **flat** listing of
the channel's Videos tab: one request, the 30 newest entries, no extraction of the single videos
(that happens only for videos that are new, to apply the live/premiere/Short rules of #39). The
tab lists neither Shorts nor live streams. Same proxy, same restricted yt-dlp (YouTube
extractors only, no config, no cache) as every other lookup (youtube.py).

What the list does NOT have is a publication date for each entry. The poller copes with that
(subscription_poller.py: it walks from the newest video and only dates the ones it does not know).

Everything yt-dlp is given is rebuilt from the channel id; failures become `ProbeError` with a
stable `kind` (`not_found` = the channel does not exist any more, `bot_check`, `network`,
`timeout`, `busy`, `unknown`).
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import youtube_cookies
from .feeds import VIDEO_ID, FeedEntry
from .youtube import ProbeError, YoutubeProbe, _clean, classify, ydl_options

logger = logging.getLogger(__name__)

LIMIT = 30
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_GONE = re.compile(
    r"channel (does not exist|is not available|was terminated|has been terminated)|"
    r"this account has been terminated|http error 404|404: not found",
    re.IGNORECASE,
)


def _list_with_ytdlp(url: str, options: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """The first `limit` flat entries of a channel tab. The entries come lazily page by page, so
    they are cut off here: asking for them all would walk through the whole channel."""
    import yt_dlp  # imported late: only needed when a listing really runs

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            entries = info.get("entries") if isinstance(info, dict) else None
            if entries is None:
                raise ProbeError("unknown", "YouTube returned no video list for this channel.")
            return [e for e in itertools.islice(entries, limit) if isinstance(e, dict)]
    except yt_dlp.utils.DownloadError as exc:
        text = str(exc)
        if _GONE.search(text):
            raise ProbeError(
                "not_found", "YouTube says this channel does not exist any more."
            ) from exc
        raise classify(text) from exc


class ChannelLister:
    """`list(channel_id)`: the newest videos of a channel, newest first, as `FeedEntry`s without a
    date. Never raises anything but ProbeError."""

    def __init__(
        self,
        probe: YoutubeProbe,
        *,
        limit: int = LIMIT,
        max_parallel: int = 2,
        extract: Callable[[str, dict[str, Any], int], list[dict[str, Any]]] | None = None,
        cookie_file: Path | None = None,
    ):
        self._probe = probe
        self.limit = limit
        self._extract = extract  # None: yt-dlp (looked up when used, so a test can replace it)
        self._slots = threading.BoundedSemaphore(max_parallel)
        self._cookie_file = cookie_file  # the admin's YouTube login cookies (#46)

    def __repr__(self) -> str:
        return f"ChannelLister(limit={self.limit})"

    def list(self, channel_id: str) -> list[FeedEntry]:
        if not CHANNEL_ID.fullmatch(channel_id):
            raise ProbeError("unsupported", "not a channel id")
        # Every listing gets its own throwaway cookie copy: yt-dlp truncates and rewrites the
        # cookiefile in place, and up to two listings run at the same time (#46 follow-up).
        # The slot is taken first, so no copy waits around for a free one.
        with self._slots, youtube_cookies.private_copy(self._cookie_file) as cookie_copy:
            options = ydl_options(self._probe.proxy, self._probe.socket_timeout, cookie_copy)
            options |= {
                "extract_flat": "in_playlist",
                "noplaylist": False,
                "allowed_extractors": ["youtube", "youtube:tab"],  # channel tabs are "youtube:tab"
            }
            url = f"https://www.youtube.com/channel/{channel_id}/videos"
            try:
                raw = (self._extract or _list_with_ytdlp)(url, options, self.limit)
            except ProbeError:
                raise
            except Exception as exc:  # a bug or an odd failure in yt-dlp must not stop the poll
                logger.exception("yt-dlp channel listing crashed")
                raise ProbeError("unknown", f"the listing failed: {type(exc).__name__}") from exc
        entries = []
        for item in raw:
            video_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id):
                entries.append(FeedEntry(video_id, _clean(item.get("title")), None))
        return entries
