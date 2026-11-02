"""Podcasts: reading a feed, naming episodes and filing them (issue #34).

A podcast is an RSS 2.0 feed (`<rss><channel><item>...`). Each item with an `<enclosure>` is an
episode. The feed is untrusted, like every feed (see `ytp.feeds` and `ytp.safefetch`):

* it is fetched with `SafeFetcher` (public addresses only, size and time limits);
* the XML goes through `feeds.read_xml` (UTF-8 only, no DTD or entity declaration);
* only the used fields are taken, each one checked: an episode needs a publication date and an
  enclosure address that passes `safefetch.check_url`; the rest of the item is ignored. Titles
  become file names only through `metadata.sanitize_name`;
* the enclosure address that is downloaded always comes from the feed. Nothing the browser sends
  is ever fetched: "download this episode" names the episode by its key and the app looks the
  address up in the feed again.

An episode is identified by `episode_key(feed address, GUID)`, or by its enclosure address when
the feed has no GUIDs. That key is the job's `external_id`, so the same episode is downloaded
once, whether it was picked by hand or found by the poller.

Files: `<MEDIA_DIR_PODCASTS>/<podcast>/<YYYY-MM-DD> - <title>.<ext>` and a cover once per podcast
(`folder.jpg` or `folder.png`, which Jellyfin reads). Nothing is overwritten; `place_episode` is
shared with YouTube-as-podcast (#35) so both end up in one folder.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from .feeds import FeedError, read_xml
from .metadata import sanitize_name
from .safefetch import FetchError, SafeFetcher, check_url, is_jpeg, is_png

logger = logging.getLogger(__name__)

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
# A back catalogue of hundreds of episodes with long show notes is several MiB of XML (#45).
MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_EPISODES = 3000
MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_EPISODE_BYTES = 2 * 1024**3
MAX_NAME_BYTES = 255
EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac", ".mp4", ".m4v", ".webm"}
)
TYPE_EXTENSIONS = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mpeg3": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac",
    "audio/ogg": ".ogg", "application/ogg": ".ogg", "audio/opus": ".opus",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/x-flac": ".flac",
    "video/mp4": ".mp4", "video/x-m4v": ".m4v", "video/webm": ".webm",
}  # fmt: skip


class PodcastError(Exception):
    """A feed or episode problem. `kind` is stable (the fetch kinds plus `bad_feed`)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Episode:
    key: str
    title: str
    published: datetime  # aware, UTC
    enclosure_url: str
    media_type: str  # as declared by the feed, may be empty
    size: int | None  # as declared, only for display


@dataclass(frozen=True)
class Podcast:
    feed_url: str
    title: str
    image_url: str | None
    episodes: list[Episode]  # newest first


def normalise_feed_url(url: str) -> str:
    """The feed address in the form used for keys and the unique index; PodcastError if unusable."""
    try:
        return check_url(url).url
    except FetchError as error:
        raise PodcastError(error.kind, error.message) from None


def episode_key(feed_url: str, identity: str) -> str:
    """The job's external_id for an episode: a hash, so a GUID of any shape is safe to store."""
    digest = hashlib.sha256(f"{feed_url}\n{identity}".encode()).hexdigest()
    return digest[:40]


def _text(node, tag: str) -> str:
    return " ".join((node.findtext(tag) or "").split())


def _public_url(value: str | None) -> str | None:
    """The address if it is a syntactically usable http(s) one; the network check comes later."""
    if not value:
        return None
    try:
        return check_url(value).url
    except FetchError:
        return None


def parse_date(text: str) -> datetime | None:
    """An RFC 822 date as used in RSS (also tolerates an ISO 8601 one); aware UTC or None."""
    value = text.strip()
    if not value:
        return None
    parsed: datetime | None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_podcast(data: bytes, feed_url: str) -> Podcast:
    """Read an RSS feed. Raises PodcastError("bad_feed") for anything unreadable.

    Items without a usable enclosure or publication date are skipped (one odd item must not hide
    the rest). Episodes come newest first, at most MAX_EPISODES.
    """
    try:
        root = read_xml(data, MAX_FEED_BYTES)
    except FeedError as error:
        raise PodcastError("bad_feed", error.message) from None
    if root.tag != "rss":
        raise PodcastError("bad_feed", "this is not an RSS feed")
    channel = root.find("channel")
    if channel is None:
        raise PodcastError("bad_feed", "the feed has no channel")
    title = _text(channel, "title")
    image = None
    itunes_image = channel.find(f"{ITUNES}image")
    if itunes_image is not None:
        image = _public_url(itunes_image.get("href"))
    if image is None:
        image = _public_url((channel.findtext("image/url") or "").strip())
    episodes: dict[str, Episode] = {}
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        url = _public_url(enclosure.get("url")) if enclosure is not None else None
        published = parse_date(item.findtext("pubDate") or "")
        if enclosure is None or url is None or published is None:
            continue
        guid = " ".join((item.findtext("guid") or "").split())
        key = episode_key(feed_url, guid[:500] or url)
        length = (enclosure.get("length") or "").strip()
        episode_title = _text(item, "title") or f"Episode {published.date().isoformat()}"
        episodes.setdefault(
            key,
            Episode(
                key=key,
                title=episode_title,
                published=published,
                enclosure_url=url,
                media_type=(enclosure.get("type") or "").split(";")[0].strip().lower(),
                size=int(length) if length.isdigit() else None,
            ),
        )
    ordered = sorted(episodes.values(), key=lambda e: e.published, reverse=True)
    return Podcast(feed_url, title, image, ordered[:MAX_EPISODES])


class PodcastFeedClient:
    """Fetches and reads a feed. Network errors become PodcastError with the fetch kind."""

    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher

    def load(self, feed_url: str) -> Podcast:
        normalised = normalise_feed_url(feed_url)
        try:
            fetched = self.fetcher.fetch_bytes(normalised, kind="feed", max_bytes=MAX_FEED_BYTES)
        except FetchError as error:
            raise PodcastError(error.kind, error.message) from None
        return parse_podcast(fetched.body, normalised)

    def cover(self, image_url: str) -> tuple[bytes, str] | None:
        """The cover picture and its extension (".jpg"/".png"), or None. Never raises: a missing
        cover is not worth failing an episode for."""
        try:
            fetched = self.fetcher.fetch_bytes(image_url, kind="image", max_bytes=MAX_COVER_BYTES)
        except FetchError as error:
            logger.info("cover not fetched", extra={"kind": error.kind})
            return None
        if is_jpeg(fetched.body[:8]):
            return fetched.body, ".jpg"
        if is_png(fetched.body[:8]):
            return fetched.body, ".png"
        return None


# ------------------------------------------------------------------------------------ filing


def choose_extension(*sources: tuple[str, str]) -> str | None:
    """The file extension for an episode from ("url", address) and ("type", content type) hints,
    in the order given; only extensions from a fixed list are ever used."""
    for kind, value in sources:
        if kind == "url":
            suffix = Path(urlsplit(value).path).suffix.lower()
            if suffix in EXTENSIONS:
                return suffix
        elif kind == "type":
            found = TYPE_EXTENSIONS.get(value.split(";")[0].strip().lower())
            if found:
                return found
    return None


def episode_filename(published: date, title: str, extension: str) -> str:
    """`2026-10-31 - Title.mp3`, the title shortened so the name fits the file system."""
    prefix = f"{published.isoformat()} - "
    room = MAX_NAME_BYTES - len((prefix + extension + " (99)").encode())
    shortened = sanitize_name(title, "Episode")
    while len(shortened.encode()) > room:
        shortened = shortened[:-1]
    return f"{prefix}{shortened.rstrip() or 'Episode'}{extension}"


def _unique_target(folder: Path, name: str, size: int) -> tuple[Path, bool]:
    """(path, already_there): an existing file of the same name AND size is the same episode
    filed earlier (a job that was interrupted after filing); otherwise a free name is found."""
    target = folder / name
    stem, suffix = target.stem, target.suffix
    counter = 1
    while target.exists() or target.is_symlink():
        if target.is_file() and not target.is_symlink() and target.stat().st_size == size:
            return target, True
        counter += 1
        target = folder / f"{stem} ({counter}){suffix}"
    return target, False


def file_into(source: Path, library: Path, parts: tuple[str, ...], name: str) -> Path:
    """Move a finished download to `<library>/<parts...>/<name>` and return the final path.

    The one place that files an audio or video download into a library (podcast episodes here,
    tracks in `ytp.music`). Never overwrites: a different file with the same name gets ` (2)`, an
    identical one (same name and size: a job interrupted right after filing) is recognised and not
    copied again. The folder must stay inside the library (a symlink cannot lead out of it).
    `source` and the library are on one file system (a rename); a copy is the fallback.
    """
    # One level at a time, checking before creating: a symlink anywhere on the way (an artist
    # folder pointing out of the library) must not get a new folder made behind it.
    root = library.resolve()
    folder = library
    for part in parts:
        folder = folder / part
        if folder.exists() or folder.is_symlink():
            if not folder.resolve().is_relative_to(root) or not folder.is_dir():
                raise PodcastError("bad_feed", "the target folder would be outside the library")
        else:
            folder.mkdir()
    target, already = _unique_target(folder, name, source.stat().st_size)
    if already:
        source.unlink(missing_ok=True)
        return target
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(target))
    return target


def place_episode(
    source: Path, library: Path, podcast: str, published: date, title: str, extension: str
) -> Path:
    """Move a finished download into `<library>/<podcast>/` as `<date> - <title>.<ext>`."""
    return file_into(source, library, (podcast,), episode_filename(published, title, extension))


def has_cover(folder: Path) -> bool:
    """Is there a cover picture in the folder already (ours, or one the owner put there)?"""
    return any(
        (folder / f"{stem}{ext}").exists()
        for stem in ("folder", "cover", "poster")
        for ext in (".jpg", ".jpeg", ".png")
    )


def save_cover(library: Path, podcast: str, data: bytes, extension: str) -> Path | None:
    """Write `folder.jpg`/`folder.png` into the podcast folder unless it has a cover already
    (ours, or the owner's own: folder/cover with jpg/jpeg/png). Returns the path written."""
    folder = library / podcast
    folder.mkdir(parents=True, exist_ok=True)
    if has_cover(folder):
        return None
    target = folder / f"folder{extension}"
    try:
        with target.open("xb") as out:
            out.write(data)
    except FileExistsError:
        return None
    return target
