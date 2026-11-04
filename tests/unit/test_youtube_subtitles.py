"""YouTube subtitles and the hand-over to Whisper (docs/transcription.md).

The download is faked like in test_youtube_download.py (no network, no ffmpeg); the subtitle pass,
the file names, the filing and the transcription queue are real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ytp import jobs, transcriptions
from ytp.clients.jellyfin import LibraryRefresher
from ytp.config import ConfigError, Settings
from ytp.db import Database
from ytp.youtube import VideoRef
from ytp.youtube_download import (
    YoutubeDownloader,
    base_language,
    choose_subtitles,
    subtitle_options,
)
from ytp.youtube_jobs import JOB_TYPE, YoutubeJobMetadata, YoutubeWorker, has_subtitles

ID = "aqz-KE-bpKQ"
URL = f"https://www.youtube.com/watch?v={ID}"
TOKEN = "t" * 43
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
SRT = "1\n00:00:00,000 --> 00:00:01,000\nHallo\n"


def info(subtitles=None, automatic=None, language="de"):
    return {
        "id": ID,
        "title": "Big Buck Bunny",
        "channel": "Blender",
        "upload_date": "20140210",
        "language": language,
        "subtitles": subtitles or {},
        "automatic_captions": automatic or {},
    }


def fake_yt(answer: dict, *, sub_error: Exception | None = None, calls: list | None = None):
    """yt-dlp stand-in: the media pass writes mp4 + jpg, the subtitle pass the requested SRTs."""

    def download(url, options):
        home = Path(options["paths"]["home"])
        if calls is not None:
            calls.append(options)
        if options.get("skip_download"):
            if sub_error is not None:
                raise sub_error
            for pattern in options["subtitleslangs"]:
                key = pattern.replace("\\", "")
                (home / f"{ID}.{key}.srt").write_text(SRT, encoding="utf-8")
            return dict(answer)
        for ext in ("mp4", "jpg"):
            (home / f"{ID}.{ext}").write_bytes(b"data")
        return dict(answer)

    return download


# --- choosing tracks ------------------------------------------------------------------------------


def test_base_language():
    assert base_language("de-DE") == "de"
    assert base_language("en_GB") == "en"
    assert base_language("de-orig") == "de"
    assert base_language("live_chat") is None
    assert base_language("") is None


def test_person_made_subtitles_in_the_wanted_order_one_per_language():
    answer = info({"en-GB": [], "en": [], "de-DE": [], "fr": [], "live_chat": []})
    chosen = choose_subtitles(answer, ("de", "en"))
    assert [(c.key, c.lang, c.automatic) for c in chosen] == [
        ("de-DE", "de", False),
        ("en", "en", False),  # the plain one wins over the regional one
    ]


def test_automatic_captions_only_when_asked_and_only_in_the_videos_language():
    automatic = {"de-orig": [], "de": [], "en": [], "fr": []}  # en, fr: machine translations
    assert choose_subtitles(info(automatic=automatic), ("de", "en")) == []
    chosen = choose_subtitles(info(automatic=automatic), ("de", "en"), automatic=True)
    assert [(c.key, c.lang, c.automatic) for c in chosen] == [("de-orig", "de", True)]


def test_a_person_made_subtitle_beats_the_automatic_one():
    answer = info({"de": []}, {"de-orig": []})
    chosen = choose_subtitles(answer, ("de",), automatic=True)
    assert [(c.key, c.automatic) for c in chosen] == [("de", False)]


def test_the_subtitle_pass_fetches_nothing_but_the_chosen_tracks(tmp_path):
    chosen = choose_subtitles(info({"de-DE": []}), ("de",))
    options = subtitle_options(tmp_path, chosen, proxy="http://gluetun:8888", socket_timeout=15)
    assert options["skip_download"] is True
    assert options["writesubtitles"] is True
    assert options["writeautomaticsub"] is False
    assert options["subtitleslangs"] == [r"de\-DE"]
    assert options["allowed_extractors"] == ["youtube"]
    assert options["noplaylist"] is True
    assert options["proxy"] == "http://gluetun:8888"
    assert {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"} in options[
        "postprocessors"
    ]


# --- the downloader -------------------------------------------------------------------------------


def downloader(answer, **kw) -> YoutubeDownloader:
    return YoutubeDownloader(
        download=fake_yt(answer, **kw), min_free_bytes=0, subtitle_langs=("de", "en")
    )


def test_subtitles_come_with_the_video(tmp_path):
    result = downloader(info({"de": [], "en-US": []})).download(VideoRef(ID), tmp_path / "w")
    assert [(s.lang, s.path.name) for s in result.subtitles] == [
        ("de", f"{ID}.de.srt"),
        ("en", f"{ID}.en-US.srt"),
    ]
    assert result.language == "de"


def test_no_subtitles_means_no_second_request(tmp_path):
    calls: list = []
    result = downloader(info(), calls=calls).download(VideoRef(ID), tmp_path / "w")
    assert result.subtitles == ()
    assert len(calls) == 1


def test_a_failing_subtitle_pass_never_fails_the_video(tmp_path):
    result = downloader(info({"de": []}), sub_error=RuntimeError("HTTP 429")).download(
        VideoRef(ID), tmp_path / "w"
    )
    assert result.video.name == f"{ID}.mp4"
    assert result.subtitles == ()


def test_audio_only_never_asks_for_subtitles(tmp_path):
    calls: list = []

    def download(url, options):
        calls.append(options)
        (Path(options["paths"]["home"]) / f"{ID}.m4a").write_bytes(b"a")
        return info({"de": []})

    loader = YoutubeDownloader(download=download, min_free_bytes=0, subtitle_langs=("de",))
    assert loader.download(VideoRef(ID), tmp_path / "w", audio_only=True).subtitles == ()
    assert len(calls) == 1


# --- filing and the queue -------------------------------------------------------------------------


class World:
    def __init__(self, tmp: Path, **env: str):
        self.data = tmp / "data"
        for name in ENV.values():
            (self.data / name).mkdir(parents=True)
        (self.data / "downloads" / "incomplete").mkdir(parents=True)
        self.settings = Settings.from_env({**ENV, "DATA_ROOT": str(self.data), **env})
        self.db = Database.open(tmp / "app.db")
        self.refresher = LibraryRefresher(None)

    def run(self, answer, *, target="video", audio_only=False):
        meta = YoutubeJobMetadata(
            video_id=ID,
            channel="Blender",
            title="Big Buck Bunny",
            target=target,
            audio_only=audio_only,
        )
        external = ID if target == "video" else f"{ID}@{target}"
        job = jobs.submit_job(self.db, JOB_TYPE, external, meta.to_json())[0]

        def download(url, options):
            if audio_only:
                (Path(options["paths"]["home"]) / f"{ID}.m4a").write_bytes(b"a")
                return dict(answer)
            return fake_yt(answer)(url, options)

        loader = YoutubeDownloader(
            download=download,
            min_free_bytes=0,
            subtitle_langs=self.settings.youtube_subtitle_langs,
        )
        worker = YoutubeWorker(self.db, self.settings, loader, self.refresher)
        assert worker.run_one()
        return jobs.get_job(self.db, job.id)

    def files(self, library="Youtube"):
        root = self.data / library
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def queue(self):
        return [
            (r["media_path"], r["language"], r["status"])
            for r in self.db.query("SELECT * FROM transcriptions ORDER BY id")
        ]


def test_youtube_subtitles_are_filed_where_jellyfin_looks_and_nothing_is_queued(tmp_path):
    world = World(tmp_path, TRANSCRIBER_TOKEN=TOKEN)
    assert world.run(info({"de": [], "en": []})).status == "done"
    assert world.files() == [
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].de.srt",
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].en.srt",
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].info.json",
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].jpg",
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].mp4",
    ]
    assert world.queue() == []


def test_a_video_without_subtitles_goes_to_whisper(tmp_path):
    world = World(tmp_path, TRANSCRIBER_TOKEN=TOKEN)
    assert world.run(info(language="en")).status == "done"
    assert world.queue() == [("Youtube/Blender/Big Buck Bunny [aqz-KE-bpKQ].mp4", "en", "queued")]


def test_without_a_transcriber_token_nothing_is_queued(tmp_path):
    world = World(tmp_path)
    assert world.run(info()).status == "done"
    assert world.queue() == []


def test_a_video_filed_again_with_a_subtitle_already_there_is_not_queued(tmp_path):
    world = World(tmp_path, TRANSCRIBER_TOKEN=TOKEN)
    folder = world.data / "Youtube" / "Blender"
    folder.mkdir()
    (folder / "Big Buck Bunny [aqz-KE-bpKQ].de.srt").write_text(SRT)
    world.run(info())
    assert world.queue() == []


def test_a_podcast_kept_as_video_gets_subtitles_or_whisper(tmp_path):
    world = World(tmp_path, TRANSCRIBER_TOKEN=TOKEN)
    world.run(info({"de": []}), target="podcast")
    assert world.files("Podcasts") == [
        "Blender/2014-02-10 - Big Buck Bunny.de.srt",
        "Blender/2014-02-10 - Big Buck Bunny.jpg",
        "Blender/2014-02-10 - Big Buck Bunny.mp4",
    ]
    assert world.queue() == []


def test_an_audio_only_episode_is_never_transcribed(tmp_path):
    world = World(tmp_path, TRANSCRIBER_TOKEN=TOKEN)
    assert world.run(info(), target="podcast", audio_only=True).status == "done"
    assert world.queue() == []


def test_subtitles_can_be_switched_off(tmp_path):
    world = World(tmp_path, YOUTUBE_SUBTITLE_LANGS="")
    assert world.settings.youtube_subtitle_langs == ()
    world.run(info({"de": []}))
    assert not [f for f in world.files() if f.endswith(".srt")]


def test_has_subtitles_looks_for_any_language_and_format(tmp_path):
    video = tmp_path / "Title [id].mp4"
    video.write_bytes(b"v")
    assert not has_subtitles(video)
    (tmp_path / "Title [id] (2).en.srt").write_text("x")  # another video's
    assert not has_subtitles(video)
    (tmp_path / "Title [id].fr.ass").write_text("x")
    assert has_subtitles(video)


# --- settings -------------------------------------------------------------------------------------


def settings(**env):
    return Settings.from_env({**ENV, **env})


def test_subtitle_and_transcriber_defaults():
    s = settings()
    assert s.youtube_subtitle_langs == ("de", "en")
    assert s.youtube_auto_subtitles is False
    assert s.transcription_enabled is False


def test_subtitle_languages_are_cleaned_and_checked():
    assert settings(YOUTUBE_SUBTITLE_LANGS=" DE, en ,de,,").youtube_subtitle_langs == ("de", "en")
    with pytest.raises(ConfigError, match="YOUTUBE_SUBTITLE_LANGS"):
        settings(YOUTUBE_SUBTITLE_LANGS="de,english")
    with pytest.raises(ConfigError, match="YOUTUBE_AUTO_SUBTITLES"):
        settings(YOUTUBE_AUTO_SUBTITLES="maybe")
    assert settings(YOUTUBE_AUTO_SUBTITLES="1").youtube_auto_subtitles is True


def test_the_transcriber_token_is_a_secret_and_must_be_long():
    s = settings(TRANSCRIBER_TOKEN=TOKEN)
    assert s.transcription_enabled
    for text in (repr(s), str(s), str(s.summary())):
        assert TOKEN not in text
    assert s.summary()["transcription_enabled"] is True
    assert settings(TRANSCRIBER_TOKEN="changeme").transcription_enabled is False
    with pytest.raises(ConfigError, match="TRANSCRIBER_TOKEN"):
        settings(TRANSCRIBER_TOKEN="short")


def test_the_queue_module_and_the_downloader_agree_on_the_subtitle_name():
    video = Path("/x/Title [id].mp4")
    assert transcriptions.subtitle_path(video, "de") == Path("/x/Title [id].de.srt")
