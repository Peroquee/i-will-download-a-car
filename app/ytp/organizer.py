"""The file organizer (issue #16): finished downloads become entries in the media library.

For a finished torrent it builds the target paths from the job's stored metadata (#14), links
the wanted files into the library folder of the media type and asks Jellyfin (#11) to look.

    movies      Filme/Title (2008)/Title (2008).mkv            (+ subtitles next to it)
    series      Serien/Title/Season 03/Title - S03E07.mkv       (a pack: every episode by its
                                                                 number in the file name)
    audiobooks  Hörbücher/Author/Title/<the audio files as they are>
    music       Musik/Artist/Album/<the audio files as they are>

The library folders are the MEDIA_DIR_* settings, directly under /data (docs/host-setup.md).

* **Hardlink, never move.** The library gets a hard link: one file on disk, two names. The torrent
  keeps seeding from its original path, which is never touched, renamed or deleted. Only when the
  two folders are on different filesystems (EXDEV) the file is copied instead. Any other failure
  is an error, not a silent copy that would double the disk usage.
* **Repeatable.** The reconciler (#15) may run an organization again (after a crash). A target
  that is already the same file is fine; a target that is a DIFFERENT file is never overwritten
  (the file is skipped and the job still succeeds if something else was linked; if nothing at all
  could be linked it fails and says why).
* **Junk is left out:** `.nfo`/`.txt`/`.url`/checksums and the like, sample files, screenshot
  folders, and for video types everything that is not a video or a subtitle.
* **Untrusted input.** The torrent chooses its own file names. Symlinks are never followed, the
  source must lie under `downloads/`, every target under its library folder, and every name goes
  through `safe_path_component` (the one function that decides what may be a folder name; also
  used for the metadata). A file whose name cannot pass is skipped with a note.

Problems a person can fix raise `OrganizeError` (the reconciler stores its text on the job).
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from .clients.jellyfin import LibraryRefresher
from .clients.qbittorrent import Torrent
from .config import Settings
from .jobs import Job
from .metadata import (
    AudiobookMetadata,
    MovieMetadata,
    MusicMetadata,
    SeriesMetadata,
    parse_metadata,
    safe_path_component,
)
from .reconciler import OrganizeError

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm"}
)
SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"})
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wav", ".wma", ".ape"}
)
COVER_NAMES = frozenset({"cover", "folder", "front", "albumart"})
COVER_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
JUNK_DIRECTORIES = frozenset(
    {"sample", "samples", "screens", "screen", "screenshots", "screenshot", "proof", "proofs"}
)
MAX_NAME_BYTES = 255  # what ext4 and ZFS allow for one name

_SAMPLE_WORD = re.compile(r"(^|[^a-z0-9])sample([^a-z0-9]|$)", re.IGNORECASE)
_SXXEYY = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,4})[ ._-]*[Ee](\d{1,4})((?:[ ._-]*[Ee-]\d{1,4})*)")
_NXNN = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})x(\d{2,3})(?![A-Za-z0-9])")
_EPISODE_ONLY = re.compile(r"(?<![A-Za-z])[Ee](?:p|pisode)?[ ._-]?(\d{1,4})(?![A-Za-z0-9])")
_SEASON_DIR = re.compile(r"^(?:season|staffel|s)[ ._-]*(\d{1,4})$", re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    source: Path
    target: Path


@dataclass
class Plan:
    links: list[Link] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # human-readable notes


@dataclass
class OrganizeResult:
    linked: int = 0
    copied: int = 0
    already_there: int = 0
    conflicts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ names and files


def _component(name: str) -> str | None:
    """A file or folder name from a torrent made safe, or None if it cannot be."""
    try:
        cleaned = safe_path_component(name)
    except PydanticCustomError:
        return None
    return cleaned if len(cleaned.encode()) <= MAX_NAME_BYTES else None


def _is_sample(path: Path, root: Path) -> bool:
    relative = path.relative_to(root) if path.is_relative_to(root) else Path(path.name)
    if any(part.lower() in JUNK_DIRECTORIES for part in relative.parts[:-1]):
        return True
    return bool(_SAMPLE_WORD.search(path.stem))


def _walk_files(root: Path) -> list[Path]:
    """Regular files below `root` (or `root` itself if it is one); symlinks are never followed."""
    if root.is_symlink():
        raise OrganizeError(f"{root} is a symbolic link; refusing to read through it")
    if root.is_file():
        return [root]
    files: list[Path] = []
    for directory, subdirs, names in os.walk(root, followlinks=False):
        subdirs[:] = sorted(d for d in subdirs if not os.path.islink(os.path.join(directory, d)))
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append(path)
    return files


def _size(path: Path) -> int:
    return path.stat().st_size


# ------------------------------------------------------------------ the planners


def plan_movie(meta: MovieMetadata, root: Path, library: Path) -> Plan:
    name = f"{meta.title} ({meta.year})"
    folder = library / name
    plan = Plan()
    videos = [
        p
        for p in _walk_files(root)
        if p.suffix.lower() in VIDEO_EXTENSIONS and not _is_sample(p, root)
    ]
    if not videos:
        raise OrganizeError("no video file found in the torrent")
    main = max(videos, key=_size)
    for extra in videos:
        if extra != main:
            plan.skipped.append(f"{extra.name}: extra video file (only the largest is used)")
    plan.links.append(Link(main, folder / f"{name}{main.suffix.lower()}"))
    stem = main.stem.casefold()
    for path in _walk_files(root):
        if path.suffix.lower() not in SUBTITLE_EXTENSIONS or _is_sample(path, root):
            continue
        # "Movie.2008.de.srt" next to "Movie.2008.mkv": keep ".de.srt" after the new name.
        if path.stem.casefold().startswith(stem) and len(path.stem) > len(stem):
            suffix = path.name[len(main.stem) :]
        else:
            suffix = path.suffix.lower()
        plan.links.append(Link(path, folder / f"{name}{suffix}"))
    return plan


def parse_episode(path: Path, root: Path, default_season: int) -> tuple[int, list[int]] | None:
    """(season, [episode numbers]) from a file's name or its folders, or None."""
    stem = path.stem
    match = _SXXEYY.search(stem)
    if match:
        episodes = [int(match[2])] + [int(n) for n in re.findall(r"\d{1,4}", match[3] or "")]
        return int(match[1]), sorted(set(episodes))
    match = _NXNN.search(stem)
    if match:
        return int(match[1]), [int(match[2])]
    match = _EPISODE_ONLY.search(stem)
    if match:
        season = default_season
        for part in reversed(path.relative_to(root).parts[:-1] if path != root else ()):
            folder = _SEASON_DIR.match(part.strip())
            if folder:
                season = int(folder[1])
                break
        return season, [int(match[1])]
    return None


def _episode_label(season: int, episodes: list[int]) -> str:
    label = f"S{season:02d}E{episodes[0]:02d}"
    if len(episodes) > 1:
        label += f"-E{episodes[-1]:02d}"
    return label


def plan_series(meta: SeriesMetadata, root: Path, library: Path) -> Plan:
    plan = Plan()
    show = library / meta.title
    videos = [
        p
        for p in _walk_files(root)
        if p.suffix.lower() in VIDEO_EXTENSIONS and not _is_sample(p, root)
    ]
    if not videos:
        raise OrganizeError("no video file found in the torrent")

    chosen: dict[tuple[int, int], Path] = {}  # (season, first episode) -> source
    labels: dict[Path, tuple[int, list[int]]] = {}
    if meta.episode is not None:
        main = max(videos, key=_size)
        labels[main] = (meta.season, [meta.episode])
        chosen[(meta.season, meta.episode)] = main
        for extra in videos:
            if extra != main:
                plan.skipped.append(f"{extra.name}: extra video file (only the largest is used)")
    else:
        for video in videos:
            parsed = parse_episode(video, root, meta.season)
            if parsed is None:
                plan.skipped.append(f"{video.name}: no episode number in the name")
                continue
            key = (parsed[0], parsed[1][0])
            if key in chosen:  # two files for one episode (a repack): keep the larger
                if _size(video) <= _size(chosen[key]):
                    plan.skipped.append(f"{video.name}: another file already covers this episode")
                    continue
                plan.skipped.append(f"{chosen[key].name}: another file already covers this episode")
                del labels[chosen[key]]
            chosen[key] = video
            labels[video] = parsed
        if not labels:
            raise OrganizeError(
                "could not find episode numbers (like S03E07) in the file names of this season pack"
            )
    for video, (season, episodes) in labels.items():
        label = _episode_label(season, episodes)
        folder = show / f"Season {season:02d}"
        stem = f"{meta.title} - {label}"
        plan.links.append(Link(video, folder / f"{stem}{video.suffix.lower()}"))
        for sub in _walk_files(root):
            if sub.suffix.lower() not in SUBTITLE_EXTENSIONS or sub.parent != video.parent:
                continue
            if sub.stem.casefold().startswith(video.stem.casefold()):
                plan.links.append(Link(sub, folder / f"{stem}{sub.name[len(video.stem) :]}"))
    return plan


def plan_audio(artist_or_author: str, title: str, root: Path, library: Path) -> Plan:
    """Audiobooks and music: Library/<who>/<what>/ with the files as the torrent has them."""
    plan = Plan()
    base = library / artist_or_author / title
    for path in _walk_files(root):
        relative = path.relative_to(root) if root.is_dir() else Path(path.name)
        suffix = path.suffix.lower()
        wanted = suffix in AUDIO_EXTENSIONS or (
            suffix in COVER_EXTENSIONS and path.stem.casefold() in COVER_NAMES
        )
        if not wanted or _is_sample(path, root):
            continue
        parts = [_component(part) for part in relative.parts]
        if None in parts or any(part.casefold() in JUNK_DIRECTORIES for part in parts[:-1]):  # type: ignore[union-attr]
            plan.skipped.append(f"{relative}: name cannot be used as a file name here")
            continue
        plan.links.append(Link(path, base.joinpath(*parts)))  # type: ignore[arg-type]
    if not any(link.source.suffix.lower() in AUDIO_EXTENSIONS for link in plan.links):
        raise OrganizeError("no audio file found in the torrent")
    return plan


# ------------------------------------------------------------------ doing it


def _copy(source: Path, target: Path) -> None:
    """Copy via a temporary name so a half-written file never has the final name."""
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def execute(plan: Plan, library: Path, downloads: Path) -> OrganizeResult:
    result = OrganizeResult(skipped=list(plan.skipped))
    library_root = library.resolve()
    downloads_root = downloads.resolve()
    for link in plan.links:
        source, target = link.source, link.target
        # Belt and braces: the planners only produce these, but a mistake there must not become
        # a file outside the library, or a link to something outside downloads/.
        if not source.resolve().is_relative_to(downloads_root):
            raise OrganizeError(f"{source} is not inside the downloads folder")
        if not target.resolve().is_relative_to(library_root):
            raise OrganizeError(f"{target} would be outside the library folder")
        if len(target.name.encode()) > MAX_NAME_BYTES:
            result.skipped.append(f"{target.name[:60]}...: file name too long")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OrganizeError(
                f"cannot create the folder {target.parent}: {exc.strerror}"
            ) from exc
        if target.exists() or target.is_symlink():
            if not target.is_symlink() and os.path.samestat(source.stat(), target.stat()):
                result.already_there += 1
            else:
                result.conflicts.append(str(target.relative_to(library)))
                logger.warning("organizer: target exists with other content, not overwriting",
                               extra={"target": str(target)})  # fmt: skip
            continue
        try:
            os.link(source, target)
            result.linked += 1
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise OrganizeError(
                    f"cannot link {source.name} into the library: {exc.strerror}"
                ) from exc
            try:
                _copy(source, target)
            except OSError as copy_exc:
                raise OrganizeError(
                    f"cannot copy {source.name} into the library: {copy_exc.strerror}"
                ) from copy_exc
            result.copied += 1
            logger.warning("organizer: copied instead of hardlinked (different filesystems)",
                           extra={"target": str(target)})  # fmt: skip
    if result.linked + result.copied + result.already_there == 0:
        if result.conflicts:
            raise OrganizeError(
                f"{len(result.conflicts)} target file(s) already exist in the library with other "
                f"content, e.g. {result.conflicts[0]}; nothing was overwritten"
            )
        raise OrganizeError("nothing in the torrent could be put into the library")
    return result


class FileOrganizer:
    """`organize(job, torrent)` for the reconciler: plan, link, ask Jellyfin to refresh."""

    def __init__(self, settings: Settings, refresher: LibraryRefresher):
        self.settings = settings
        self.refresher = refresher

    def __call__(self, job: Job, torrent: Torrent) -> None:
        try:
            meta = parse_metadata(job.metadata)
        except ValidationError as exc:
            raise OrganizeError(
                f"the stored metadata is not valid any more: {exc.error_count()} error(s)"
            ) from exc
        library_name = self.settings.library_dirs[meta.type]
        library = self.settings.data_root / library_name
        downloads = self.settings.downloads_dir
        root = Path(torrent.content_path)
        if not str(root) or not root.is_absolute():
            raise OrganizeError("qBittorrent did not report where the files are")
        if not root.resolve().is_relative_to(downloads.resolve()):
            raise OrganizeError(f"the files are not inside {downloads} ({root})")
        if not root.exists():
            raise OrganizeError(f"the downloaded files are missing: {root}")
        if not library.is_dir():
            raise OrganizeError(
                f"the library folder {library_name} does not exist under {self.settings.data_root} "
                "(run make layout)"
            )

        if isinstance(meta, MovieMetadata):
            plan = plan_movie(meta, root, library)
        elif isinstance(meta, SeriesMetadata):
            plan = plan_series(meta, root, library)
        elif isinstance(meta, AudiobookMetadata):
            plan = plan_audio(meta.author, meta.title, root, library)
        elif isinstance(meta, MusicMetadata):
            plan = plan_audio(meta.artist, meta.album, root, library)
        else:  # pragma: no cover - the union is closed
            raise OrganizeError(f"unknown media type {meta.type!r}")

        result = execute(plan, library, downloads)
        logger.info(
            "organized",
            extra={
                "job_id": job.id,
                "type": meta.type,
                "linked": result.linked,
                "copied": result.copied,
                "already_there": result.already_there,
                "conflicts": len(result.conflicts),
                "skipped": len(result.skipped),
            },
        )
        # Fail soft by design: this only notes the request; Jellyfin trouble never reaches here.
        self.refresher.request_refresh(unicodedata.normalize("NFC", library_name))
