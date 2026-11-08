"""YouTube download jobs: the queue in the database and the worker that runs it (issue #18).

`POST /api/youtube/download` only records a job (type `youtube`, keyed by the video id, status
`queued`) and wakes the worker; the download happens in the background, one at a time, and the
job's status says where it is:

    queued -> downloading -> organizing -> done          any of them -> failed (readable reason)

The database is the queue, so nothing is lost when the app restarts: at start, jobs that were
`downloading` or `organizing` go back to `queued` and run again from the beginning (the work
folder is thrown away and made fresh). One failed download does not stop the next. A failed job
is retried by submitting the same video again, as for magnets.

**Targets** (issues #35, #36). The same queue serves three destinations, chosen when the video is
submitted and stored in the job's metadata:

* `video` (the default, what the jobs of #18 are): `<Youtube>/<channel>/<title> [<id>].mp4`;
* `podcast`: filed like an RSS episode of #34, `<Podcasts>/<podcast>/<YYYY-MM-DD> - <title>.<ext>`,
  so a podcast fed from YouTube and one fed from RSS share a folder. With `audio_only` the file is
  the audio stream (m4a/opus, no re-encode) with tags and cover; without it the video goes into the
  podcast folder with its thumbnail beside it.

* `music`: `<Musik>/<artist>/<album>/<title>.<ext>` like a torrent album (`channel` is the
  artist, `album` the album, `title` the track); audio only is optional here too, and the file gets
  the artist/album/title tags Jellyfin's music library reads.

The job's `external_id` is the video id for the `video` target (unchanged) and `<id>@<target>` for
the others, so the same video can be a video AND a podcast episode, but is queued once per target.

**Resolution** (#47): a job may carry `max_height` (0 = best, or one of 2160, 1440, 1080, 720, 480,
360), chosen in the input form. It caps the video stream of that job only; without it, and for
podcast or audio-only downloads, the server default (`YOUTUBE_MAX_HEIGHT`) applies.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from . import jobs
from .audiotags import Tags, embed_tags
from .clients.jellyfin import LibraryRefresher
from .config import Settings
from .db import Database
from .jobs import Job
from .metadata import Name
from .music import place_track
from .podcasts import PodcastError, place_episode
from .youtube import ProbeError, VideoRef, parse_video_url
from .youtube_download import (
    MAX_HEIGHTS,
    Downloaded,
    DownloadFailed,
    YoutubeDownloader,
    place,
    upload_date,
)

logger = logging.getLogger(__name__)

JOB_TYPE = "youtube"
LIBRARY_KEYS = {"video": "youtube", "podcast": "podcasts", "music": "music"}
TARGETS = ("video", "podcast", "music")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class YoutubeJobMetadata(BaseModel):
    """What `jobs.metadata` holds for a YouTube job: the CONFIRMED names, which become folders.

    `channel` is the folder-level name: the channel for a video, the podcast for a podcast
    episode. `title` is the video's or the episode's title. Jobs made before the targets existed
    have neither `target` nor `audio_only` and mean "video".
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["youtube"] = "youtube"
    video_id: str
    channel: Name
    title: Name
    target: Literal["video", "podcast", "music"] = "video"
    audio_only: bool = False
    # The cap chosen in the input form (#47); absent = the server default (YOUTUBE_MAX_HEIGHT).
    max_height: int | None = None
    album: Name | None = None  # music only (issue #36)
    # YYYY-MM-DD, when the feed told us (the poller); the download's own upload date wins.
    published: str | None = None

    @field_validator("max_height")
    @classmethod
    def _max_height(cls, value: int | None) -> int | None:
        if value is not None and value not in MAX_HEIGHTS:
            choices = ", ".join(str(height) for height in MAX_HEIGHTS[1:])
            raise ValueError(f"max_height must be 0 (best) or one of {choices}")
        return value

    @field_validator("published")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        if value is not None:
            if not _DATE.fullmatch(value):
                raise ValueError("published must be YYYY-MM-DD")
            date.fromisoformat(value)
        return value

    def to_json(self) -> str:
        """The text stored in `jobs.metadata`: without nulls and without the defaults of the
        fields added later, so a plain video job looks exactly as it did before the targets."""
        data = self.model_dump(mode="json", exclude_none=True)
        if self.target == "video":
            del data["target"]
        if not self.audio_only:
            del data["audio_only"]
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @model_validator(mode="after")
    def _audio_needs_a_target(self) -> YoutubeJobMetadata:
        if self.audio_only and self.target == "video":
            raise ValueError("audio_only needs a target other than video")
        if self.target == "music" and self.album is None:
            raise ValueError("a music track needs an album")
        if self.target != "music" and self.album is not None:
            raise ValueError("an album is for music only")
        return self


def job_external_id(video_id: str, target: str = "video") -> str:
    """The job key: the video id, plus `@<target>` for anything but the plain video."""
    return video_id if target == "video" else f"{video_id}@{target}"


class YoutubeWorker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        downloader: YoutubeDownloader,
        refresher: LibraryRefresher,
        *,
        idle_seconds: float = 5.0,
    ):
        self.db = db
        self.settings = settings
        self.downloader = downloader
        self.refresher = refresher
        self._idle = idle_seconds
        self._wake = threading.Event()
        self.processed = 0
        # Percent of the running download by job id, for the job list (not persisted).
        self.progress: dict[int, int] = {}
        # Size, speed and ETA of the running download by job id, for the dashboard (#38).
        self.details: dict[int, dict[str, int | None]] = {}

    def wake(self) -> None:
        """A job was just queued: look now instead of at the next idle check."""
        self._wake.set()

    # ------------------------------------------------------------------ the loop

    def run(self, stop: threading.Event) -> None:
        jobs.requeue_interrupted(self.db, JOB_TYPE)
        while not stop.is_set():
            try:
                handled = self.run_one()
            except Exception:  # the worker must outlive any bug in one job
                logger.exception("youtube worker failed")
                handled = False
            if not handled:
                self._wake.wait(self._idle)
                self._wake.clear()

    def run_one(self) -> bool:
        """Take the next queued YouTube job and finish it (done or failed). False if none."""
        job = jobs.claim_next_queued(self.db, JOB_TYPE)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("youtube job crashed", extra={"job_id": job.id})
            self._fail(job, f"the download failed: {type(exc).__name__}")
        finally:
            self.progress.pop(job.id, None)
            self.details.pop(job.id, None)
        self.processed += 1
        return True

    # ------------------------------------------------------------------ one job

    def _workdir(self, video_id: str) -> Path:
        return self.settings.downloads_dir / "incomplete" / "youtube" / video_id

    def _process(self, job: Job) -> None:
        try:
            meta = YoutubeJobMetadata.model_validate(job.metadata)
            video_id = job.external_id.partition("@")[0]
            ref = parse_video_url(f"https://www.youtube.com/watch?v={video_id}")
        except Exception:
            self._fail(job, "the stored job data is not valid any more; submit the video again")
            return
        if meta.video_id != ref.video_id or job.external_id != job_external_id(
            ref.video_id, meta.target
        ):
            self._fail(job, "the stored job data does not match its video; submit it again")
            return
        library_key = LIBRARY_KEYS[meta.target]
        library = self.settings.library_path(library_key)
        if not library.is_dir():
            self._fail(job, f"the library folder {library.name} does not exist (run make layout)")
            return
        workdir = self._workdir(ref.video_id)
        logger.info("youtube download starts", extra={"job_id": job.id, "video_id": ref.video_id})
        try:
            downloaded = self.downloader.download(
                ref,
                workdir,
                self._progress(job),
                audio_only=meta.audio_only,
                # Audio-only streams have no height, and podcast downloads keep the default;
                # a video job without the key keeps the default too (#47).
                max_height=(
                    None if meta.audio_only or meta.target == "podcast" else meta.max_height
                ),
            )
            if not jobs.transition(self.db, job.id, "organizing", expected=("downloading",)):
                return  # someone else changed the job; leave it alone
            self._file(job, ref, meta, downloaded, library)
        except (ProbeError, DownloadFailed) as exc:
            message = exc.message if isinstance(exc, ProbeError) else str(exc)
            self._fail(job, message)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _file(
        self,
        job: Job,
        ref: VideoRef,
        meta: YoutubeJobMetadata,
        downloaded: Downloaded,
        library: Path,
    ) -> None:
        try:
            if meta.target == "podcast":
                self._file_podcast(ref, meta, downloaded, library)
            elif meta.target == "music":
                self._file_music(ref, meta, downloaded, library)
            else:
                place(downloaded, library, meta.channel, meta.title, ref.video_id)
        except (DownloadFailed, PodcastError, OSError) as exc:
            self._fail(job, getattr(exc, "message", None) or str(exc))
            return
        if jobs.transition(self.db, job.id, "done", expected=("organizing",)):
            logger.info("job done", extra={"job_id": job.id, "video_id": ref.video_id})
            self.refresher.request_refresh(self.settings.library_dirs[LIBRARY_KEYS[meta.target]])

    def _file_music(
        self, ref: VideoRef, meta: YoutubeJobMetadata, downloaded: Downloaded, library: Path
    ) -> None:
        """A video as a track: `<artist>/<album>/<title>.<ext>`, tagged when it is audio only."""
        published = upload_date(downloaded.info)
        if downloaded.audio_only:
            tagged = embed_tags(
                downloaded.video,
                Tags(
                    title=meta.title,
                    artist=meta.channel,
                    album=meta.album or "",
                    album_artist=meta.channel,
                    published=published,
                    comment=ref.url,
                ),
                downloaded.thumbnail,
            )
            if not tagged:
                logger.warning("track filed without tags", extra={"video_id": ref.video_id})
        target = place_track(
            downloaded.video, library, meta.channel, meta.album or "", meta.title,
            downloaded.video.suffix.lower(),
        )  # fmt: skip
        if downloaded.thumbnail is not None and not downloaded.audio_only:
            sidecar = target.with_suffix(".jpg")
            if not sidecar.exists() and not sidecar.is_symlink():
                shutil.move(str(downloaded.thumbnail), str(sidecar))

    def _file_podcast(
        self, ref: VideoRef, meta: YoutubeJobMetadata, downloaded: Downloaded, library: Path
    ) -> None:
        """A video as a podcast episode: the same names and folder as an RSS episode (#34).

        Audio only: tags (podcast = album, title = episode) and the thumbnail as the cover are
        written into the file. A video episode gets its thumbnail beside it, named like it.
        """
        published = (
            upload_date(downloaded.info)
            or (date.fromisoformat(meta.published) if meta.published else None)
            or date.today()
        )
        if downloaded.audio_only:
            tagged = embed_tags(
                downloaded.video,
                Tags(
                    title=meta.title,
                    artist=meta.channel,
                    album=meta.channel,
                    album_artist=meta.channel,
                    published=published,
                    genre="Podcast",
                    comment=ref.url,
                ),
                downloaded.thumbnail,
            )
            if not tagged:
                logger.warning("episode filed without tags", extra={"video_id": ref.video_id})
        target = place_episode(
            downloaded.video, library, meta.channel, published, meta.title,
            downloaded.video.suffix.lower(),
        )  # fmt: skip
        if downloaded.thumbnail is not None and not downloaded.audio_only:
            sidecar = target.with_suffix(".jpg")
            if not sidecar.exists() and not sidecar.is_symlink():
                shutil.move(str(downloaded.thumbnail), str(sidecar))

    def _fail(self, job: Job, reason: str) -> None:
        jobs.transition(
            self.db,
            job.id,
            "failed",
            expected=("queued", "downloading", "organizing"),
            error=reason,
        )
        logger.warning("youtube job failed", extra={"job_id": job.id, "reason": reason})

    def _progress(self, job: Job):
        """A yt-dlp progress hook that logs each quarter once (not every chunk)."""
        reached = {"quarter": 0}

        def hook(data: dict[str, Any]) -> None:
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes")
            if data.get("status") != "downloading" or not total or not done:
                return
            self.progress[job.id] = min(int(done * 100 / total), 100)
            speed, eta = data.get("speed"), data.get("eta")
            self.details[job.id] = {
                "done_bytes": int(done),
                "total_bytes": int(total),
                "speed": int(speed) if isinstance(speed, (int, float)) and speed > 0 else None,
                "eta": int(eta) if isinstance(eta, (int, float)) and eta >= 0 else None,
            }
            quarter = int(done * 4 / total)
            if quarter > reached["quarter"]:
                reached["quarter"] = quarter
                logger.info(
                    "youtube download progress",
                    extra={"job_id": job.id, "percent": min(quarter * 25, 100)},
                )

        return hook
