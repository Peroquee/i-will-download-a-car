"""YouTube: which URLs are accepted, and the metadata probe (issue #17).

The probe asks yt-dlp, **used as a library** (never a shell), for the metadata of ONE video and
downloads nothing. Two rules make the untrusted input safe:

* `parse_video_url` accepts only a single-video address on youtube.com or youtu.be and returns
  the video id. Everything yt-dlp is given afterwards is rebuilt from that id
  (`https://www.youtube.com/watch?v=<id>`): whatever else was in the pasted text (a playlist
  parameter, a start time, a tracking tail) is dropped, and playlists, channels, handles and
  every other site are refused before yt-dlp sees anything.
* yt-dlp is restricted to its YouTube extractors, runs with `noplaylist`, never reads config
  files or writes a cache, and goes through the proxy in `YTDLP_PROXY` (by default gluetun's
  HTTP proxy, so the requests leave through the VPN; see docs/youtube.md).

`YoutubeProbe.probe` never hangs a request: yt-dlp runs in a worker thread with short socket
timeouts and no retries, the caller waits at most `timeout` seconds, and at most `max_parallel`
probes run at once (an abandoned slow probe cannot pile up).

Failures become `ProbeError(kind, message)` with a stable `kind` for the UI: `private`,
`unavailable` (deleted or removed), `region`, `age`, `members`, `live`, `bot_check`, `network`,
`not_found`, `timeout`, `busy`, `unsupported`, `unknown`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .video_filter import VideoTraits

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 300
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
WATCH_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")
THUMBNAIL_SCHEMES = ("https",)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class InvalidVideoUrl(ValueError):
    """The text is not an address of a single YouTube video. The message is safe to show."""


class ProbeError(Exception):
    """The probe could not get the video's metadata. `kind` is stable, `message` is for people."""

    def __init__(self, kind: str, message: str, *, upcoming: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        # kind "live" only: the video has not started yet (a premiere or a scheduled stream),
        # as opposed to a stream that is running or has ended (#39).
        self.upcoming = upcoming


@dataclass(frozen=True)
class VideoRef:
    video_id: str

    @property
    def url(self) -> str:
        """The only address ever handed to yt-dlp: rebuilt from the id."""
        return f"https://www.youtube.com/watch?v={self.video_id}"


def parse_video_url(text: str) -> VideoRef:
    """The video a pasted YouTube address points to; raises InvalidVideoUrl."""
    if not isinstance(text, str):
        raise InvalidVideoUrl("the address must be text")
    value = text.strip()
    if not value:
        raise InvalidVideoUrl("the address is empty")
    if len(value) > MAX_URL_LENGTH:
        raise InvalidVideoUrl(f"the address is longer than {MAX_URL_LENGTH} characters")
    if not value.isprintable() or any(ch.isspace() for ch in value):
        raise InvalidVideoUrl("the address must be a single line without spaces")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise InvalidVideoUrl("the address must start with https://")
    if parts.username is not None or parts.password is not None:
        raise InvalidVideoUrl("the address must not contain a login")
    try:
        port = parts.port
    except ValueError:
        raise InvalidVideoUrl("the address has an invalid port") from None
    if port not in (None, 80, 443):
        raise InvalidVideoUrl("the address must not name a port")
    host = (parts.hostname or "").lower()

    candidate = ""
    if host in SHORT_HOSTS:
        candidate = parts.path.strip("/").split("/")[0] if parts.path.strip("/") else ""
    elif host in WATCH_HOSTS:
        if parts.path == "/watch":
            values = parse_qs(parts.query).get("v", [])
            candidate = values[0] if len(values) == 1 else ""
        elif parts.path.startswith(PATH_PREFIXES):
            candidate = parts.path.split("/")[2] if parts.path.count("/") >= 2 else ""
        elif parts.path.startswith(("/playlist", "/channel/", "/c/", "/user/", "/@")):
            raise InvalidVideoUrl(
                "this is a playlist or channel address: paste the link of one video"
            )
    else:
        raise InvalidVideoUrl("only youtube.com and youtu.be addresses are accepted")
    if not VIDEO_ID.fullmatch(candidate):
        raise InvalidVideoUrl("no video found in this address: paste the link of one video")
    return VideoRef(candidate)


# ---------------------------------------------------------------------------------- errors

# (kind, regex on yt-dlp's message, text for people). First match wins.
_CLASSIFY: tuple[tuple[str, str, str], ...] = (
    ("private", r"private video", "This video is private."),
    (
        "age",
        r"confirm your age|age[- ]restricted|inappropriate for some users",
        "This video is age-restricted and cannot be read without a YouTube login.",
    ),
    (
        "region",
        r"not available in your country|blocked it in your country|"
        r"not made this video available in your country|geo[- ]?restrict",
        "This video is not available in this country.",
    ),
    (
        "members",
        r"members-only|join this channel|available to this channel's members",
        "This video is for channel members only.",
    ),
    (
        "bot_check",
        r"confirm you.{0,3}re not a bot|sign in to confirm",
        "YouTube wants proof that this is not a bot (it often does this for VPN addresses). "
        "Try again later, or set YTDLP_PROXY empty to ask directly.",
    ),
    (
        "live",
        r"live event will begin|premieres? in|is a live|live stream",
        "This is a live stream or premiere that has not ended yet.",
    ),
    (
        "unavailable",
        r"video unavailable|video is unavailable|has been removed|no longer available|"
        r"does not exist|"
        r"video is not available|account.*terminated|copyright|deleted",
        "This video is deleted or no longer available.",
    ),
    (
        "not_found",
        r"http error 404|404: not found",
        "Nothing was found at this address.",
    ),
    (
        "network",
        r"unable to download|urlopen error|connection (refused|reset|aborted)|timed out|"
        r"tunnel connection failed|proxy|name or service not known|temporary failure|"
        r"network is unreachable|http error 5\d\d|ssl",
        "YouTube could not be reached (is the VPN up?).",
    ),
    ("unsupported", r"unsupported url|is not a valid url", "This is not a video address."),
)


def classify(message: str) -> ProbeError:
    """Turn a yt-dlp error text into a ProbeError with a stable kind."""
    cleaned = _ANSI.sub("", message or "").strip()
    for kind, pattern, text in _CLASSIFY:
        if re.search(pattern, cleaned, re.IGNORECASE):
            upcoming = kind == "live" and bool(
                re.search(r"live event will begin|premieres? in", cleaned, re.IGNORECASE)
            )
            return ProbeError(kind, text, upcoming=upcoming)
    first_line = cleaned.splitlines()[0] if cleaned else "unknown error"
    return ProbeError("unknown", f"YouTube returned an error: {first_line[:200]}")


# ---------------------------------------------------------------------------------- the probe


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    url: str
    title: str
    channel: str
    channel_id: str
    duration: int  # seconds, 0 if unknown
    thumbnail: str

    @property
    def duration_text(self) -> str:
        hours, rest = divmod(self.duration, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "url": self.url,
            "title": self.title,
            "channel": self.channel,
            "channel_id": self.channel_id,
            "duration": self.duration,
            "duration_text": self.duration_text,
            "thumbnail": self.thumbnail,
        }


def ydl_options(
    proxy: str, socket_timeout: float, cookie_file: Path | None = None
) -> dict[str, Any]:
    """The yt-dlp options for a metadata-only probe (also used to pin them in tests).

    `cookie_file` (#46): the admin's YouTube login cookies, passed only when the file exists.
    The probe itself never uses it; a channel listing may.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "allowed_extractors": ["youtube"],  # regex on extractor names: YouTube only
        "cachedir": False,  # no files written
        "socket_timeout": socket_timeout,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "color": "never",
        "enable_file_urls": False,
        "geo_bypass": False,
        "logger": _SilentLogger(),
        **({"proxy": proxy} if proxy else {}),
    }
    if cookie_file is not None and cookie_file.is_file():
        options["cookiefile"] = str(cookie_file)
    return options


class _SilentLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def _extract_with_ytdlp(url: str, options: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp  # imported late: the dependency is only needed when a probe really runs

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            # process=False: the extractor's own answer, without resolving formats. All we need
            # (title, channel, duration, thumbnail) is in it, and it is much faster.
            info = ydl.extract_info(url, download=False, process=False)
    except yt_dlp.utils.DownloadError as exc:
        raise classify(str(exc)) from exc
    if not isinstance(info, dict):
        raise ProbeError("unknown", "YouTube returned nothing for this video.")
    return info


class YoutubeProbe:
    def __init__(
        self,
        *,
        proxy: str = "",
        timeout: float = 25.0,
        socket_timeout: float = 10.0,
        max_parallel: int = 2,
        extract: Callable[[str, dict[str, Any]], dict[str, Any]] = _extract_with_ytdlp,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self._socket_timeout = socket_timeout
        self._extract = extract
        self._slots = threading.BoundedSemaphore(max_parallel)

    @property
    def socket_timeout(self) -> float:
        return self._socket_timeout

    def __repr__(self) -> str:
        return f"YoutubeProbe(proxy={'set' if self.proxy else 'none'}, timeout={self.timeout})"

    def probe_blocking(self, ref: VideoRef) -> VideoInfo:
        """Run the probe in this thread (it still holds a slot). Raises ProbeError."""
        try:
            info = self._extract(ref.url, ydl_options(self.proxy, self._socket_timeout))
        except ProbeError:
            raise
        except Exception as exc:  # a bug or an odd failure in yt-dlp must not become a 500
            logger.exception("yt-dlp probe crashed", extra={"video_id": ref.video_id})
            raise ProbeError("unknown", f"the probe failed: {type(exc).__name__}") from exc
        return self._to_info(ref, info)

    def traits_blocking(self, ref: VideoRef) -> VideoTraits:
        """What the poller needs to know about a NEW video of a subscription (#39): live status
        and the Short signals. Same options, proxy and slots as the probe; raises ProbeError
        (`kind` `live` also stands for "not started yet": yt-dlp refuses upcoming videos)."""
        with self._slots:
            try:
                info = self._extract(ref.url, ydl_options(self.proxy, self._socket_timeout))
            except ProbeError:
                raise
            except Exception as exc:  # an odd failure in yt-dlp must not stop the poll
                logger.exception("yt-dlp probe crashed", extra={"video_id": ref.video_id})
                raise ProbeError("unknown", f"the probe failed: {type(exc).__name__}") from exc
        return VideoTraits.from_info(info)

    async def probe(self, ref: VideoRef) -> VideoInfo:
        """The probe with an overall deadline and a limit on parallel runs."""
        if not self._slots.acquire(blocking=False):
            raise ProbeError("busy", "Too many probes are running, try again in a moment.")

        def work() -> VideoInfo:
            try:
                return self.probe_blocking(ref)
            finally:
                self._slots.release()  # only when the thread really ends, even if abandoned

        try:
            return await asyncio.wait_for(asyncio.to_thread(work), timeout=self.timeout)
        except TimeoutError:
            logger.warning("probe timed out", extra={"video_id": ref.video_id})
            raise ProbeError(
                "timeout", f"YouTube did not answer within {int(self.timeout)} seconds."
            ) from None

    @staticmethod
    def _to_info(ref: VideoRef, info: dict[str, Any]) -> VideoInfo:
        if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
            raise ProbeError("unsupported", "This is a playlist, not one video.")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise ProbeError(
                "live",
                "This is a live stream or premiere that has not ended yet.",
                upcoming=info.get("live_status") == "is_upcoming",
            )
        title = _clean(info.get("title"))
        if not title:
            raise ProbeError("unavailable", "YouTube returned no title for this video.")
        duration = info.get("duration")
        return VideoInfo(
            video_id=ref.video_id,
            url=ref.url,
            title=title,
            channel=_clean(info.get("channel") or info.get("uploader")),
            channel_id=_clean(info.get("channel_id") or info.get("uploader_id")),
            duration=int(duration) if isinstance(duration, (int, float)) and duration > 0 else 0,
            thumbnail=_thumbnail(info),
        )


def _clean(value: Any) -> str:
    """Display text from YouTube: a string without control characters, trimmed."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable() or ch == " ").strip()[:300]


def _thumbnail(info: dict[str, Any]) -> str:
    candidates = [info.get("thumbnail")] + [
        t.get("url") for t in reversed(info.get("thumbnails") or []) if isinstance(t, dict)
    ]
    for url in candidates:
        if isinstance(url, str) and urlsplit(url).scheme in THUMBNAIL_SCHEMES:
            return url
    return ""
