"""Which videos of a subscribed channel are not downloaded automatically (issue #39).

The poller queues ordinary videos. Three kinds are left out, because a download would fail or
produce something nobody asked for:

* **live**: a live stream that is running, or one that has ended (`live_status` `is_live` /
  `was_live`, `is_live`, `was_live`, `media_type` `livestream`);
* **premiere**: an announced video that has not started (`live_status` `is_upcoming`, or yt-dlp's
  "Premieres in ..." error). A premiere and a scheduled live stream look the same before they
  start, so both are reported as "premiere". Once it has aired, a premiere is skipped as well
  (it was judged when announced and is not looked at again);
* **short**: a YouTube Short. Decided from cheap signals first, and in this order:
  1. the feed's link is a `/shorts/` link (no request needed, see `feeds.parse_feed`);
  2. yt-dlp says `media_type` is `short`, or the page address is a `/shorts/` one;
  3. the video is at most `SHORT_MAX_SECONDS` (3 minutes, YouTube's limit for a Short) long AND
     its picture is taller than wide. A short vertical clip that was uploaded as an ordinary
     video is therefore treated as a Short too; a short landscape clip is not. Unknown size
     counts as "not a Short" (an ordinary video is never dropped on a guess).

Videos pasted by hand are never filtered; this is only used by the poller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SHORT_MAX_SECONDS = 180


@dataclass(frozen=True)
class VideoTraits:
    """The few fields of yt-dlp's answer that decide, nothing else is kept."""

    live_status: str = ""
    is_live: bool = False
    was_live: bool = False
    media_type: str = ""
    duration: int = 0
    width: int = 0
    height: int = 0
    webpage_url: str = ""

    @classmethod
    def from_info(cls, info: dict[str, Any]) -> VideoTraits:
        width, height = _size(info)
        duration = info.get("duration")
        return cls(
            live_status=_text(info.get("live_status")),
            is_live=info.get("is_live") is True,
            was_live=info.get("was_live") is True,
            media_type=_text(info.get("media_type")),
            duration=int(duration) if isinstance(duration, (int, float)) and duration > 0 else 0,
            width=width,
            height=height,
            webpage_url=_text(info.get("webpage_url")),
        )


def _text(value: Any) -> str:
    return value.strip().lower()[:200] if isinstance(value, str) else ""


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _size(info: dict[str, Any]) -> tuple[int, int]:
    """Picture size: the top-level fields, else the tallest format that has one."""
    width, height = _number(info.get("width")), _number(info.get("height"))
    if width and height:
        return width, height
    best = (0, 0)
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        w, h = _number(fmt.get("width")), _number(fmt.get("height"))
        if w and h and h > best[1]:
            best = (w, h)
    return best


def skip_reason(traits: VideoTraits) -> str | None:
    """`live`, `premiere`, `short`, or None for an ordinary video."""
    if (
        traits.is_live
        or traits.was_live
        or traits.live_status in {"is_live", "was_live", "post_live"}
        or traits.media_type == "livestream"
    ):
        return "live"
    if traits.live_status == "is_upcoming":
        return "premiere"
    if traits.media_type == "short" or "/shorts/" in traits.webpage_url:
        return "short"
    if 0 < traits.duration <= SHORT_MAX_SECONDS and 0 < traits.width < traits.height:
        return "short"
    return None
