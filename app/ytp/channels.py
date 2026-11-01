"""YouTube channels: which addresses are accepted, and resolving one to its channel id (#19).

The channel RSS feed (#20) is keyed by the channel id (`UC` + 22 characters), not by the
`@handle`, so the id is looked up ONCE when someone subscribes and stored.

`parse_channel_ref` accepts, and nothing else:

* a channel id (`UCxxxxxxxxxxxxxxxxxxxxxx`),
* a handle (`@name`),
* the address of a channel on youtube.com: `/channel/UC...` or `/@name`, optionally followed by
  a tab such as `/videos`,
* the address of ONE video (the channel is read from the video, which is what the YouTube form
  has at hand when "subscribe" is ticked).

Everything else (other sites, ports, logins, playlists, the legacy `/c/` and `/user/` names)
is refused with a message before yt-dlp sees anything, and what yt-dlp gets is rebuilt from the
parsed value, never the pasted text. The lookup goes through the same proxy and the same
restricted yt-dlp as the probe (see youtube.py), and never starts a download.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .youtube import (
    MAX_URL_LENGTH,
    WATCH_HOSTS,
    InvalidVideoUrl,
    ProbeError,
    YoutubeProbe,
    _clean,
    _extract_with_ytdlp,
    parse_video_url,
    ydl_options,
)

logger = logging.getLogger(__name__)

CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
HANDLE = re.compile(r"@[A-Za-z0-9._-]{3,30}")
CHANNEL_TABS = frozenset({"", "videos", "featured", "streams", "shorts", "about", "playlists"})


class InvalidChannel(ValueError):
    """The text is not an address of a YouTube channel. The message is safe to show."""


@dataclass(frozen=True)
class ChannelRef:
    kind: str  # "id" | "handle" | "video"
    value: str  # the channel id, the handle with its "@", or the video id

    @property
    def url(self) -> str:
        """The only address handed to yt-dlp for an id or a handle: rebuilt from the value."""
        if self.kind == "id":
            return f"https://www.youtube.com/channel/{self.value}"
        if self.kind == "handle":
            return f"https://www.youtube.com/{self.value}"
        return f"https://www.youtube.com/watch?v={self.value}"


@dataclass(frozen=True)
class Channel:
    channel_id: str
    name: str


def parse_channel_ref(text: str) -> ChannelRef:
    """What a pasted text points to; raises InvalidChannel."""
    if not isinstance(text, str):
        raise InvalidChannel("the channel must be text")
    value = text.strip()
    if not value:
        raise InvalidChannel("the channel is empty")
    if len(value) > MAX_URL_LENGTH:
        raise InvalidChannel(f"the text is longer than {MAX_URL_LENGTH} characters")
    if not value.isprintable() or any(ch.isspace() for ch in value):
        raise InvalidChannel("the channel must be a single line without spaces")
    if CHANNEL_ID.fullmatch(value):
        return ChannelRef("id", value)
    if HANDLE.fullmatch(value):
        return ChannelRef("handle", value)

    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise InvalidChannel("expected a channel id, an @handle or a youtube.com address")
    if parts.username is not None or parts.password is not None:
        raise InvalidChannel("the address must not contain a login")
    try:
        port = parts.port
    except ValueError:
        raise InvalidChannel("the address has an invalid port") from None
    if port not in (None, 80, 443):
        raise InvalidChannel("the address must not name a port")
    host = (parts.hostname or "").lower()
    if host not in WATCH_HOSTS and host not in {"youtu.be", "www.youtu.be"}:
        raise InvalidChannel("only youtube.com addresses are accepted")

    segments = [s for s in parts.path.split("/") if s]
    if host in WATCH_HOSTS and segments:
        first = segments[0]
        tab = segments[2] if len(segments) > 2 else ""
        if first == "channel" and len(segments) >= 2 and CHANNEL_ID.fullmatch(segments[1]):
            if len(segments) <= 3 and tab in CHANNEL_TABS:
                return ChannelRef("id", segments[1])
        elif first.startswith("@") and HANDLE.fullmatch(first):
            if len(segments) <= 2 and (segments[1] if len(segments) > 1 else "") in CHANNEL_TABS:
                return ChannelRef("handle", first)
        elif first in {"c", "user"}:
            raise InvalidChannel(
                "this old-style channel address cannot be used: paste the @handle address, "
                "the /channel/UC... address, or a video of the channel"
            )
        elif first in {"playlist"}:
            raise InvalidChannel("this is a playlist: paste a channel address or a video of it")
    try:
        return ChannelRef("video", parse_video_url(value).video_id)
    except InvalidVideoUrl:
        raise InvalidChannel(
            "no channel found: paste the channel id, the @handle, the channel address "
            "or the address of one of its videos"
        ) from None


def _channel_from_info(info: dict[str, Any], expected_id: str | None = None) -> Channel:
    """Pull id and name out of yt-dlp's answer for a channel page; ProbeError when unusable."""
    channel_id = info.get("channel_id") or info.get("uploader_id") or info.get("id")
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ProbeError("unknown", "YouTube did not tell which channel this is.")
    if expected_id is not None and channel_id != expected_id:
        raise ProbeError("unknown", "YouTube answered with a different channel than asked for.")
    name = _clean(info.get("channel") or info.get("uploader") or info.get("title"))
    return Channel(channel_id, name or channel_id)


class ChannelResolver:
    """Finds the channel id and name behind a `ChannelRef`. Never raises anything but ProbeError."""

    def __init__(
        self,
        probe: YoutubeProbe,
        *,
        timeout: float | None = None,
        max_parallel: int = 2,
        extract: Callable[[str, dict[str, Any]], dict[str, Any]] = _extract_with_ytdlp,
    ):
        self._probe = probe
        self.timeout = probe.timeout if timeout is None else timeout
        self._extract = extract
        self._slots = threading.BoundedSemaphore(max_parallel)

    def _lookup_page(self, ref: ChannelRef) -> Channel:
        options = ydl_options(self._probe.proxy, self._probe.socket_timeout)
        # The channel page lists its videos; one entry is more than enough, none is needed.
        options |= {
            "extract_flat": "in_playlist",
            "playlistend": 1,
            "noplaylist": False,
            # Channel pages belong to yt-dlp's "youtube:tab" extractor; still YouTube only.
            "allowed_extractors": ["youtube", "youtube:tab"],
        }
        try:
            info = self._extract(ref.url, options)
        except ProbeError:
            raise
        except Exception as exc:
            logger.exception("yt-dlp channel lookup crashed")
            raise ProbeError("unknown", f"the lookup failed: {type(exc).__name__}") from exc
        return _channel_from_info(info, ref.value if ref.kind == "id" else None)

    async def resolve(self, ref: ChannelRef) -> Channel:
        if ref.kind == "video":
            video = await self._probe.probe(parse_video_url(ref.url))
            return _channel_from_info({"channel_id": video.channel_id, "channel": video.channel})
        if not self._slots.acquire(blocking=False):
            raise ProbeError("busy", "Too many lookups are running, try again in a moment.")

        def work() -> Channel:
            try:
                return self._lookup_page(ref)
            finally:
                self._slots.release()

        try:
            return await asyncio.wait_for(asyncio.to_thread(work), timeout=self.timeout)
        except TimeoutError:
            raise ProbeError(
                "timeout", f"YouTube did not answer within {int(self.timeout)} seconds."
            ) from None
