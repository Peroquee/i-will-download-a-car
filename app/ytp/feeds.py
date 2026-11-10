"""A YouTube channel's RSS feed: fetching it and reading it safely (issue #20).

`https://www.youtube.com/feeds/videos.xml?channel_id=UC...` lists a channel's 15 newest videos
and needs no API key. The feed comes from the internet, so it is treated as untrusted:

* it is fetched through the same HTTP proxy as yt-dlp (`YTDLP_PROXY`, the VPN by default), with
  short timeouts, no redirects and a size limit;
* the XML is read with the standard library after a **refusal of any DTD or entity declaration**
  (a real feed has none): that is what "billion laughs" and external-entity tricks need, so
  without them ElementTree is safe to use and no extra dependency is needed. The check is made on
  the text, so the feed must be UTF-8 (no NUL bytes, valid UTF-8): a UTF-16 feed could otherwise
  hide the declaration from it;
* only the fields used are taken (video id, title, publication time), each one checked: an id
  must look like a video id, a time must parse. Titles are later made safe as folder names by
  `metadata.sanitize_name`; nothing of the feed is ever used as a path or a command.

Failures are `FeedError(kind, message)`: `network` (not reachable, probably the VPN), `not_found`
(404: the channel is gone), `temporary` (429/5xx), `bad_feed` (anything unreadable).

Since 2026-11 the feed is only the fallback: `channel_videos.py` lists the channel with yt-dlp
first, because YouTube answers the feed with 404 for whole networks. A 404 of the FEED therefore
does not mean "this channel has no feed"; the texts below say what happened, and the ones that
concern YouTube as a whole (`SERVICE_WIDE`) are recognised by the alert monitor, which raises ONE
alert for all channels instead of one per channel.
"""

from __future__ import annotations

import http.client
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from . import __version__, timeutil

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MAX_FEED_BYTES = 1024 * 1024
MAX_ENTRIES = 50
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_DTD = re.compile(r"<!\s*(doctype|entity)", re.IGNORECASE)


# Messages that are about YouTube, not about one channel. Kept as constants: the alert monitor
# recognises them by their text (`is_service_wide`).
SERVICE_DOWN = (
    "YouTube is not giving out video lists right now (the channel page and the RSS feed both "
    "failed). That is on YouTube's side, not this channel's. The app tries again."
)
SERVICE_BOT = (
    "YouTube asks for proof that this is not a bot, and its RSS feed is not available either. "
    "Try again later, or set YTDLP_PROXY empty to ask YouTube directly instead of through the VPN."
)
SERVICE_RSS_404 = (
    "YouTube's RSS feed is not available (HTTP 404). YouTube switches it off for whole networks "
    "at times; it is not about this channel."
)
CHANNEL_GONE = "YouTube says this channel does not exist any more (deleted or renamed?)."
SERVICE_WIDE = frozenset({SERVICE_DOWN, SERVICE_BOT, SERVICE_RSS_404})


def is_service_wide(message: str | None) -> bool:
    """Is this a `last_error` that says YouTube is the problem, for every channel alike?"""
    return message in SERVICE_WIDE


class FeedError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    title: str
    published: datetime | None  # timezone-aware UTC; None for a listing made with yt-dlp
    short_link: bool = False  # the feed's own link is a /shorts/ address (#39)


def parse_time(text: str) -> datetime | None:
    """An ISO 8601 time (`...Z` or with an offset) as aware UTC; None if it is not one."""
    return timeutil.parse_time_or_none(text)


def read_xml(data: bytes, max_bytes: int) -> ET.Element:
    """The root element of an untrusted XML document, or FeedError("bad_feed").

    The refusals that make the standard library parser safe: too large, not UTF-8, and any DTD
    or entity declaration (see the module description). Shared with the podcast feeds.
    """
    if len(data) > max_bytes:
        raise FeedError("bad_feed", "the feed is unexpectedly large")
    # Only UTF-8 is read. Another encoding (UTF-16 with or without a byte order mark, which has
    # NUL bytes between the letters) would hide "<!DOCTYPE" from the check below.
    if b"\x00" in data:
        raise FeedError("bad_feed", "the feed is not UTF-8 text")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FeedError("bad_feed", "the feed is not UTF-8 text") from None
    if _DTD.search(text):
        raise FeedError("bad_feed", "the feed contains a DTD or entity declaration; refused")
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise FeedError("bad_feed", "the feed is not valid XML") from None


def _is_short_link(node: ET.Element) -> bool:
    """Does the entry's alternate link point to youtube.com/shorts/...? (The feed links Shorts
    that way, so this is a free first signal, see `ytp.video_filter`.)"""
    for link in node.findall(f"{ATOM}link"):
        if link.get("rel", "alternate") != "alternate":
            continue
        parts = urlsplit((link.get("href") or "").strip())
        host = (parts.hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com"} and parts.path.startswith(
            "/shorts/"
        ):
            return True
    return False


def parse_feed(data: bytes, channel_id: str | None = None) -> list[FeedEntry]:
    """The videos of a feed, newest first. Raises FeedError("bad_feed") for anything unreadable.

    Entries with an invalid video id or time are skipped (one odd entry must not hide the rest).
    If `channel_id` is given, the feed must say it belongs to that channel.
    """
    root = read_xml(data, MAX_FEED_BYTES)
    if root.tag != f"{ATOM}feed":
        raise FeedError("bad_feed", "this is not an Atom feed")
    if channel_id is not None:
        # The feed's own `yt:channelId` comes WITHOUT the "UC" prefix (the entries have it).
        declared = (root.findtext(f"{YT}channelId") or "").strip()
        if declared and declared not in (channel_id, channel_id.removeprefix("UC")):
            raise FeedError("bad_feed", "the feed belongs to a different channel")
    entries: list[FeedEntry] = []
    for node in root.findall(f"{ATOM}entry")[:MAX_ENTRIES]:
        video_id = (node.findtext(f"{YT}videoId") or "").strip()
        published = parse_time(node.findtext(f"{ATOM}published") or "")
        if not VIDEO_ID.fullmatch(video_id) or published is None:
            continue
        title = " ".join((node.findtext(f"{ATOM}title") or "").split())
        entries.append(FeedEntry(video_id, title, published, _is_short_link(node)))
    entries.sort(key=lambda e: e.published or datetime.min.replace(tzinfo=UTC), reverse=True)
    return entries


class FeedClient:
    """Fetches feeds through the proxy (EMPTY proxy = directly). `base` is only changed by tests."""

    def __init__(
        self,
        proxy: str = "",
        *,
        timeout: float = 15.0,
        base: str = "https://www.youtube.com",
    ):
        self.proxy = proxy
        self.timeout = timeout
        self._base = urlsplit(base)

    def __repr__(self) -> str:
        return f"FeedClient(proxy={'set' if self.proxy else 'none'})"

    def _connection(self) -> http.client.HTTPConnection:
        host = self._base.hostname or "www.youtube.com"
        if self._base.scheme == "http":  # tests only: a local server, no TLS, no proxy
            return http.client.HTTPConnection(host, self._base.port or 80, timeout=self.timeout)
        if self.proxy:
            parts = urlsplit(self.proxy)
            conn = http.client.HTTPSConnection(
                parts.hostname or "", parts.port or 8888, timeout=self.timeout
            )
            conn.set_tunnel(host, self._base.port or 443)
            return conn
        return http.client.HTTPSConnection(host, self._base.port or 443, timeout=self.timeout)

    def fetch(self, channel_id: str) -> bytes:
        if not CHANNEL_ID.fullmatch(channel_id):
            raise FeedError("bad_feed", "not a channel id")
        path = f"/feeds/videos.xml?channel_id={quote(channel_id)}"
        headers = {
            "User-Agent": f"youtube-premium/{__version__}",
            "Accept": "application/atom+xml, application/xml;q=0.9",
        }
        conn = self._connection()
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            status = response.status
            body = response.read(MAX_FEED_BYTES + 1)
        except (OSError, http.client.HTTPException):
            raise FeedError("network", "the feed could not be fetched (is the VPN up?)") from None
        finally:
            conn.close()
        if status == 404:
            raise FeedError("not_found", SERVICE_RSS_404)
        if status == 429 or status >= 500:
            raise FeedError("temporary", f"YouTube answered HTTP {status}; will try again later")
        if status != 200:
            raise FeedError("bad_feed", f"YouTube answered HTTP {status}")
        return body
