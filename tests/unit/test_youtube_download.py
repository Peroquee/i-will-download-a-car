"""YouTube downloads (issue #18): options, filing, the worker and POST /api/youtube/download.

No network and no ffmpeg: the download itself is faked; what is real is the file handling, the
database queue, the names, the failure paths and the HTTP layer.
"""

# ruff: noqa: E501  (long literals in the fixtures and parametrised tables)

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.helpers.fake_jellyfin import API_KEY as JELLYFIN_KEY
from tests.helpers.fake_jellyfin import FakeJellyfin
from ytp import jobs
from ytp.clients.jellyfin import LibraryRefresher
from ytp.config import ConfigError, Settings
from ytp.db import Database
from ytp.main import create_app
from ytp.youtube import ProbeError, VideoRef
from ytp.youtube_download import (
    AUDIO_FORMAT,
    DownloadFailed,
    YoutubeDownloader,
    download_options,
    file_stem,
    format_selector,
    place,
    summary,
)
from ytp.youtube_jobs import JOB_TYPE, YoutubeJobMetadata, YoutubeWorker

ID = "aqz-KE-bpKQ"
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
URL = f"https://www.youtube.com/watch?v={ID}"
INFO = {
    "id": ID,
    "title": "Big Buck Bunny",
    "channel": "Blender",
    "channel_id": "UC1",
    "duration": 635,
    "upload_date": "20140210",
    "description": "d" * 6000,
    "tags": ["a"] * 80,
    "formats": [
        {"url": "https://rr1---sn.googlevideo.com/videoplayback?ip=198.51.100.1&sig=SECRET"}
    ],
    "http_headers": {"Cookie": "SECRET"},
    "url": "https://rr1---sn.googlevideo.com/videoplayback?ip=198.51.100.1",
    "width": 1920,
    "height": 1080,
}


def fake_download(files=("mp4", "jpg"), info=None, error=None, seen=None):
    """A stand-in for yt-dlp: writes the files into the work folder as yt-dlp would."""

    def download(url, options):
        if seen is not None:
            seen.append((url, options))
        if error is not None:
            raise error
        home = Path(options["paths"]["home"])
        video_id = url[-11:]
        for ext in files:
            (home / f"{video_id}.{ext}").write_bytes(b"data-" + ext.encode())
        for hook in options["progress_hooks"]:
            for done in (10, 30, 60, 100):
                hook({"status": "downloading", "downloaded_bytes": done, "total_bytes": 100})
        return dict(INFO if info is None else info)

    return download


class World:
    def __init__(self, tmp: Path):
        self.data = tmp / "data"
        for name in ENV.values():
            (self.data / name).mkdir(parents=True)
        (self.data / "downloads" / "incomplete").mkdir(parents=True)
        self.settings = Settings.from_env({**ENV, "DATA_ROOT": str(self.data)})
        self.db = Database.open(tmp / "app.db")
        self.requested: list = []
        refresher = LibraryRefresher(None)
        refresher.request_refresh = lambda folder=None: self.requested.append(folder)  # type: ignore[method-assign]
        self.refresher = refresher

    def worker(self, **kw) -> YoutubeWorker:
        downloader = YoutubeDownloader(
            download=kw.pop("download", fake_download()), min_free_bytes=kw.pop("min_free", 0)
        )
        return YoutubeWorker(self.db, self.settings, downloader, self.refresher, **kw)

    def submit(self, channel="Blender", title="Big Buck Bunny", video_id=ID):
        meta = YoutubeJobMetadata(video_id=video_id, channel=channel, title=title)
        return jobs.submit_job(self.db, JOB_TYPE, video_id, meta.model_dump_json())[0]

    def library(self) -> list[str]:
        root = self.data / "Youtube"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def status(self, job_id=1):
        job = jobs.get_job(self.db, job_id)
        return job.status, job.error


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# --- options and format -------------------------------------------------------------------------------


def test_the_format_prefers_h264_and_aac_in_mp4_and_caps_the_height():
    selector = format_selector(1080)
    assert selector.startswith("bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]")
    assert "[height<=1080]" in selector
    assert "height" not in format_selector(0)


def test_the_options_are_locked_down(tmp_path):
    options = download_options(
        tmp_path, proxy="http://gluetun:8888", max_height=720, socket_timeout=15
    )
    assert options["noplaylist"] is True
    assert options["allowed_extractors"] == ["youtube"]
    assert options["merge_output_format"] == "mp4"
    assert options["outtmpl"] == {"default": "%(id)s.%(ext)s"}, "named by id, never by title"
    assert options["paths"] == {"home": str(tmp_path), "temp": str(tmp_path)}
    assert options["writeinfojson"] is False
    assert options["writethumbnail"] is True
    assert options["proxy"] == "http://gluetun:8888"
    assert options["cachedir"] is False
    assert options["enable_file_urls"] is False
    assert "exec" not in options
    assert "proxy" not in download_options(tmp_path, proxy="", max_height=0, socket_timeout=1)


def test_the_real_ytdlp_accepts_the_download_options(tmp_path):
    import yt_dlp

    with yt_dlp.YoutubeDL(
        download_options(tmp_path, proxy="http://gluetun:8888", max_height=1080, socket_timeout=10)
    ) as ydl:
        assert ydl.params["merge_output_format"] == "mp4"


# --- the cleaned .info.json -------------------------------------------------------------------------------


@pytest.mark.security
def test_the_info_summary_has_no_stream_addresses_or_headers():
    cleaned = summary(INFO, VideoRef(ID))
    text = json.dumps(cleaned)
    for secret in ("googlevideo", "SECRET", "198.51.100.1", "Cookie"):
        assert secret not in text
    assert "formats" not in cleaned
    assert "url" not in cleaned
    assert cleaned["webpage_url"] == URL
    assert len(cleaned["description"]) == 5000
    assert len(cleaned["tags"]) == 50
    assert cleaned["saved_by"] == "youtube-premium"
    assert cleaned["title"] == "Big Buck Bunny"


# --- the downloader --------------------------------------------------------------------------------------------


def test_a_download_gives_the_video_the_thumbnail_and_the_summary(tmp_path):
    work = tmp_path / "work"
    result = YoutubeDownloader(download=fake_download(), min_free_bytes=0).download(
        VideoRef(ID), work
    )
    assert result.video == work / f"{ID}.mp4"
    assert result.thumbnail == work / f"{ID}.jpg"
    assert result.info["channel"] == "Blender"


def test_a_leftover_work_folder_is_cleared_first(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "old.part").write_text("junk")
    YoutubeDownloader(download=fake_download(), min_free_bytes=0).download(VideoRef(ID), work)
    assert not (work / "old.part").exists()


def test_no_thumbnail_is_fine_no_video_is_not(tmp_path):
    got = YoutubeDownloader(download=fake_download(files=("mp4",)), min_free_bytes=0).download(
        VideoRef(ID), tmp_path / "a"
    )
    assert got.thumbnail is None
    with pytest.raises(DownloadFailed, match="without producing a video"):
        YoutubeDownloader(download=fake_download(files=("jpg",)), min_free_bytes=0).download(
            VideoRef(ID), tmp_path / "b"
        )


def test_an_empty_video_file_is_a_failure(tmp_path):
    def empty(url, options):
        (Path(options["paths"]["home"]) / f"{ID}.mp4").write_bytes(b"")
        return dict(INFO)

    with pytest.raises(DownloadFailed):
        YoutubeDownloader(download=empty, min_free_bytes=0).download(VideoRef(ID), tmp_path / "w")


def test_too_little_free_space_is_refused_before_anything_is_downloaded(tmp_path):
    seen = []
    downloader = YoutubeDownloader(download=fake_download(seen=seen), min_free_bytes=10**18)
    with pytest.raises(DownloadFailed, match="not enough free space"):
        downloader.download(VideoRef(ID), tmp_path / "w")
    assert seen == []


def test_errors_keep_their_meaning(tmp_path):
    with pytest.raises(ProbeError) as caught:
        YoutubeDownloader(
            download=fake_download(error=ProbeError("private", "private")), min_free_bytes=0
        ).download(VideoRef(ID), tmp_path / "a")
    assert caught.value.kind == "private"
    with pytest.raises(DownloadFailed, match="No space left"):
        YoutubeDownloader(
            download=fake_download(error=OSError(28, "No space left on device")), min_free_bytes=0
        ).download(VideoRef(ID), tmp_path / "b")
    with pytest.raises(DownloadFailed, match="KeyError"):
        YoutubeDownloader(download=fake_download(error=KeyError("x")), min_free_bytes=0).download(
            VideoRef(ID), tmp_path / "c"
        )


# --- naming and filing ----------------------------------------------------------------------------------------


def test_the_stem_is_title_and_id():
    assert file_stem("Big Buck Bunny", ID) == f"Big Buck Bunny [{ID}]"


def test_a_long_title_is_shortened_so_every_file_name_fits_255_bytes():
    stem = file_stem("ü" * 200, ID)
    assert len((stem + ".info.json").encode()) <= 255
    assert stem.endswith(f" [{ID}]")


def test_filing_moves_the_files_into_channel_folder_with_the_right_names(world, tmp_path):
    downloaded = YoutubeDownloader(download=fake_download(), min_free_bytes=0).download(
        VideoRef(ID), tmp_path / "w"
    )
    place(downloaded, world.data / "Youtube", "Blender", "Big Buck Bunny", ID)
    assert world.library() == [
        f"Blender/Big Buck Bunny [{ID}].info.json",
        f"Blender/Big Buck Bunny [{ID}].jpg",
        f"Blender/Big Buck Bunny [{ID}].mp4",
    ]
    info = json.loads(
        (world.data / "Youtube" / "Blender" / f"Big Buck Bunny [{ID}].info.json").read_text()
    )
    assert info["id"] == ID
    assert not list((tmp_path / "w").glob("*.mp4")), "moved, not copied"


def test_an_existing_target_is_left_alone(world, tmp_path):
    folder = world.data / "Youtube" / "Blender"
    folder.mkdir()
    existing = folder / f"Big Buck Bunny [{ID}].mp4"
    existing.write_bytes(b"already here")
    downloaded = YoutubeDownloader(download=fake_download(), min_free_bytes=0).download(
        VideoRef(ID), tmp_path / "w"
    )
    place(downloaded, world.data / "Youtube", "Blender", "Big Buck Bunny", ID)
    assert existing.read_bytes() == b"already here"


@pytest.mark.security
@pytest.mark.parametrize("channel", ["../../etc", "a/b", "..", "x\ny", "."])
def test_names_that_climb_out_cannot_even_be_submitted(channel):
    with pytest.raises(Exception, match=r"(?i)must not|validation"):
        YoutubeJobMetadata(video_id=ID, channel=channel, title="ok")


# --- the worker -----------------------------------------------------------------------------------------------------


def test_a_queued_job_is_downloaded_filed_and_marked_done(world):
    world.submit()
    assert world.worker().run_one() is True
    assert world.status() == ("done", None)
    assert f"Blender/Big Buck Bunny [{ID}].mp4" in world.library()
    assert world.requested == ["Youtube"], "Jellyfin is asked to refresh the YouTube library"


def test_the_status_passes_through_downloading_and_organizing(world):
    world.submit()
    seen = []

    def watching(url, options):
        seen.append(world.status()[0])
        return fake_download()(url, options)

    worker = world.worker(download=watching)
    original = worker._file

    def spy(*args, **kwargs):
        seen.append(world.status()[0])
        return original(*args, **kwargs)

    worker._file = spy
    worker.run_one()
    assert seen == ["downloading", "organizing"]
    assert world.status()[0] == "done"


def test_no_job_means_nothing_to_do(world):
    assert world.worker().run_one() is False


def test_the_work_folder_is_removed_after_success_and_after_failure(world):
    world.submit()
    world.worker().run_one()
    assert not (world.data / "downloads" / "incomplete" / "youtube" / ID).exists()
    world.submit(video_id="bbbbbbbbbbb")
    world.worker(download=fake_download(error=ProbeError("network", "no route"))).run_one()
    assert not (world.data / "downloads" / "incomplete" / "youtube" / "bbbbbbbbbbb").exists()


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (ProbeError("private", "This video is private."), "This video is private."),
        (ProbeError("network", "YouTube could not be reached (is the VPN up?)."), "VPN"),
        (DownloadFailed("not enough free space"), "not enough free space"),
        (RuntimeError("boom"), "RuntimeError"),
    ],
)
def test_failures_end_in_failed_with_a_readable_reason_and_leave_the_library_alone(
    world, error, words
):
    world.submit()
    world.worker(download=fake_download(error=error)).run_one()
    status, reason = world.status()
    assert status == "failed"
    assert words in reason
    assert world.library() == []
    assert world.requested == []


def test_one_failed_job_does_not_stop_the_next(world):
    world.submit()
    world.submit(video_id="bbbbbbbbbbb")
    calls = {"n": 0}

    def flaky(url, options):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProbeError("network", "down")
        return fake_download()(url, options)

    worker = world.worker(download=flaky)
    assert worker.run_one()
    assert worker.run_one()
    assert world.status(1)[0] == "failed"
    assert world.status(2)[0] == "done"


def test_jobs_run_one_at_a_time_oldest_first(world):
    first = world.submit()
    second = world.submit(video_id="bbbbbbbbbbb")
    order = []

    def recording(url, options):
        order.append(url[-11:])
        return fake_download()(url, options)

    worker = world.worker(download=recording)
    worker.run_one()
    assert order == [ID]
    assert world.status(first.id)[0] == "done"
    assert world.status(second.id)[0] == "queued"


def test_a_library_folder_that_is_missing_fails_the_job_readably(world):
    world.submit()
    (world.data / "Youtube").rmdir()
    world.worker().run_one()
    status, reason = world.status()
    assert status == "failed"
    assert "does not exist" in reason


def test_stored_data_that_is_invalid_fails_the_job_instead_of_crashing(world):
    with world.db.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (type, external_id, metadata) VALUES ('youtube', ?, '{\"channel\": \"../x\"}')",
            (ID,),
        )
    world.worker().run_one()
    status, reason = world.status()
    assert status == "failed"
    assert "not valid" in reason


def test_a_job_with_a_mismatching_id_is_refused(world):
    world.submit()
    with world.db.transaction() as conn:
        conn.execute("UPDATE jobs SET external_id = 'ccccccccccc'")
    world.worker().run_one()
    assert world.status()[0] == "failed"


def test_after_a_restart_interrupted_jobs_run_again_from_the_start(world):
    world.submit()
    with world.db.transaction() as conn:
        conn.execute("UPDATE jobs SET status = 'downloading'")
    stop = threading.Event()
    worker = world.worker(idle_seconds=0.05)
    thread = threading.Thread(target=worker.run, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 5
    while world.status()[0] != "done" and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    worker.wake()
    thread.join(5)
    assert world.status()[0] == "done"


def test_wake_makes_the_idle_worker_look_at_once(world):
    stop = threading.Event()
    worker = world.worker(idle_seconds=30)
    thread = threading.Thread(target=worker.run, args=(stop,))
    thread.start()
    time.sleep(0.1)
    world.submit()
    worker.wake()
    deadline = time.monotonic() + 5
    while world.status()[0] != "done" and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    worker.wake()
    thread.join(5)
    assert world.status()[0] == "done"


def test_the_progress_is_logged_per_quarter_not_per_chunk(world, caplog):
    import logging

    world.submit()
    with caplog.at_level(logging.INFO, logger="ytp.youtube_jobs"):
        world.worker().run_one()
    lines = [r for r in caplog.records if r.getMessage() == "youtube download progress"]
    assert [r.percent for r in lines] == [25, 50, 100], (
        "10% is below the first quarter; no line per chunk"
    )


def test_requeue_interrupted_only_touches_youtube_jobs(world):
    world.submit()
    magnet, _ = jobs.submit_magnet_job(world.db, "a" * 40, "{}")
    with world.db.transaction() as conn:
        conn.execute("UPDATE jobs SET status = 'downloading'")
    assert jobs.requeue_interrupted(world.db, "youtube") == 1
    assert jobs.get_job(world.db, magnet.id).status == "downloading", "qBittorrent's, not ours"


# --- the endpoint -----------------------------------------------------------------------------------------------------


def make_client(tmp_path: Path, download=None, **env: str) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    data = tmp_path / "data"
    for name in ENV.values():
        (data / name).mkdir(parents=True, exist_ok=True)
    (data / "downloads" / "incomplete").mkdir(parents=True, exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(data), "CONFIG_DIR": str(config), **env}
    client = TestClient(create_app(Settings.from_env(values)))
    client.__enter__()
    worker = client.app.state.youtube_worker
    if worker is not None:
        worker.downloader = YoutubeDownloader(
            download=download or fake_download(),
            min_free_bytes=0,
            max_height=client.app.state.settings.youtube_max_height,
        )
    return client


@pytest.fixture
def client(tmp_path):
    test_client = make_client(tmp_path)
    yield test_client
    test_client.__exit__(None, None, None)


def post(client, url=URL, channel="Blender", title="Big Buck Bunny", **extra):
    return client.post(
        "/api/youtube/download", json={"url": url, "channel": channel, "title": title, **extra}
    )


def wait_for(client, status, job_id=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get_job(client.app.state.db, job_id)
        if job and job.status == status:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {status}: {jobs.get_job(client.app.state.db, job_id)}")


def test_the_endpoint_queues_a_job_and_the_background_worker_finishes_it(client, tmp_path):
    response = post(client)
    assert response.status_code == 201
    assert response.json() == {
        "id": 1,
        "video_id": ID,
        "type": "youtube",
        "target": "video",
        "status": "queued",
        "duplicate": False,
    }
    job = wait_for(client, "done")
    assert job.metadata == {
        "type": "youtube",
        "video_id": ID,
        "channel": "Blender",
        "title": "Big Buck Bunny",
    }
    assert (tmp_path / "data" / "Youtube" / "Blender" / f"Big Buck Bunny [{ID}].mp4").exists()


def test_a_pasted_address_with_a_tail_is_reduced_to_the_id(client):
    response = post(client, f"https://youtu.be/{ID}?list=PLevil&t=3")
    assert response.status_code == 201
    assert response.json()["video_id"] == ID


def test_the_same_video_twice_is_one_job(client):
    first = post(client)
    wait_for(client, "done")
    second = post(client)
    assert second.status_code == 200
    assert second.json() == {**first.json(), "status": "done", "duplicate": True}
    assert client.app.state.db.query("SELECT COUNT(*) FROM jobs")[0][0] == 1


def test_a_failed_job_is_retried_by_sending_it_again(tmp_path):
    state = {"fail": True}

    def sometimes(url, options):
        if state["fail"]:
            raise ProbeError("network", "YouTube could not be reached")
        return fake_download()(url, options)

    c = make_client(tmp_path, download=sometimes)
    try:
        post(c)
        job = wait_for(c, "failed")
        assert "could not be reached" in job.error
        state["fail"] = False
        again = post(c, title="Better Title")
        assert again.status_code == 201
        assert again.json()["id"] == 1
        done = wait_for(c, "done")
        assert done.metadata["title"] == "Better Title"
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"url": "https://vimeo.com/1", "channel": "c", "title": "t"}, ["body", "url"]),
        ({"url": URL, "channel": "", "title": "t"}, ["body", "channel"]),
        ({"url": URL, "channel": "a/b", "title": "t"}, ["body", "channel"]),
        ({"url": URL, "channel": "c", "title": "../../etc/passwd"}, ["body", "title"]),
        ({"url": URL, "channel": "c", "title": "x" * 300}, ["body", "title"]),
        ({"url": URL, "channel": "c", "title": "two\nlines"}, ["body", "title"]),
        ({"url": URL, "channel": "c"}, ["body", "title"]),
    ],
)
def test_bad_input_is_a_field_level_422_and_nothing_is_queued(client, body, loc):
    response = client.post("/api/youtube/download", json=body)
    assert response.status_code == 422
    assert loc in [e["loc"] for e in response.json()["detail"]]
    assert client.app.state.db.query("SELECT COUNT(*) FROM jobs")[0][0] == 0


def test_extra_fields_are_refused(client):
    assert post(client, quality="4320p").status_code == 422


def test_names_are_normalised_before_they_become_folders(client, tmp_path):
    post(client, channel="  Kanal Ä  ", title="Größe Reise")
    wait_for(client, "done")
    assert (tmp_path / "data" / "Youtube" / "Kanal Ä").is_dir()


def test_without_a_database_the_endpoint_answers_503(tmp_path):
    c = make_client(tmp_path, CONFIG_DIR=str(tmp_path / "missing"))
    try:
        response = post(c)
    finally:
        c.__exit__(None, None, None)
    assert response.status_code == 503


def test_only_post_is_allowed(client):
    assert client.get("/api/youtube/download").status_code == 405


def test_the_download_goes_through_the_configured_proxy_and_height(tmp_path):
    seen = []
    c = make_client(tmp_path, download=fake_download(seen=seen), YOUTUBE_MAX_HEIGHT="720")
    try:
        c.app.state.youtube_worker.downloader.proxy = "http://gluetun:8888"
        c.app.state.youtube_worker.downloader.max_height = 720
        post(c)
        wait_for(c, "done")
    finally:
        c.__exit__(None, None, None)
    url, options = seen[0]
    assert url == URL
    assert options["proxy"] == "http://gluetun:8888"
    assert "height<=720" in options["format"]


# --- the per-download resolution (#47) -------------------------------------------------------------


@pytest.mark.parametrize("height", [0, 2160, 1440, 1080, 720, 480, 360])
def test_each_allowed_max_height_is_accepted_and_stored(client, height):
    response = post(client, max_height=height)
    assert response.status_code == 201
    job = wait_for(client, "done")
    assert job.metadata["max_height"] == height


@pytest.mark.parametrize("height", [999, "abc", -1, 1, True, 1080.5, [720], {"n": 1}])
def test_a_bad_max_height_is_a_400_and_queues_nothing(client, height):
    response = post(client, max_height=height)
    assert response.status_code == 400
    assert "max_height" in response.json()["detail"]
    assert client.app.state.db.query("SELECT COUNT(*) FROM jobs")[0][0] == 0


def test_a_missing_max_height_keeps_the_server_default(tmp_path):
    seen = []
    c = make_client(tmp_path, download=fake_download(seen=seen), YOUTUBE_MAX_HEIGHT="720")
    try:
        post(c)
        job = wait_for(c, "done")
    finally:
        c.__exit__(None, None, None)
    assert "max_height" not in job.metadata
    assert "height<=720" in seen[0][1]["format"]


def test_a_job_max_height_overrides_the_default_for_that_download(tmp_path):
    seen = []
    c = make_client(tmp_path, download=fake_download(seen=seen), YOUTUBE_MAX_HEIGHT="720")
    try:
        post(c, max_height=2160)
        wait_for(c, "done")
    finally:
        c.__exit__(None, None, None)
    assert "height<=2160" in seen[0][1]["format"]


def test_an_audio_only_job_ignores_max_height(tmp_path):
    seen = []
    c = make_client(tmp_path, download=fake_download(files=("m4a",), seen=seen))
    try:
        post(c, target="podcast", audio_only=True, max_height=360)
        wait_for(c, "done")
    finally:
        c.__exit__(None, None, None)
    options = seen[0][1]
    assert options["format"] == AUDIO_FORMAT
    assert "height" not in options["format"]


def test_a_podcast_job_ignores_max_height(tmp_path):
    seen = []
    c = make_client(tmp_path, download=fake_download(seen=seen), YOUTUBE_MAX_HEIGHT="720")
    try:
        post(c, target="podcast", max_height=2160)
        wait_for(c, "done")
    finally:
        c.__exit__(None, None, None)
    assert "height<=720" in seen[0][1]["format"]


def test_old_job_metadata_without_max_height_uses_the_default(world):
    seen = []
    jobs.submit_job(
        world.db,
        JOB_TYPE,
        ID,
        json.dumps(
            {
                "type": "youtube",
                "video_id": ID,
                "channel": "Blender",
                "title": "Big Buck Bunny",
            }
        ),
    )
    worker = world.worker(download=fake_download(seen=seen))
    worker.downloader.max_height = 480
    assert worker.run_one()
    assert "height<=480" in seen[0][1]["format"]


def test_the_stored_metadata_refuses_an_unknown_height():
    with pytest.raises(ValueError, match="max_height"):
        YoutubeJobMetadata(video_id=ID, channel="Blender", title="X", max_height=999)


def test_the_page_can_read_the_default_resolution(client):
    response = client.get("/api/youtube/options")
    assert response.status_code == 200
    assert response.json() == {
        "max_height": 1080,
        "choices": [0, 2160, 1440, 1080, 720, 480, 360],
    }


def test_the_options_show_the_configured_default(tmp_path):
    c = make_client(tmp_path, YOUTUBE_MAX_HEIGHT="720")
    try:
        response = c.get("/api/youtube/options")
    finally:
        c.__exit__(None, None, None)
    assert response.json()["max_height"] == 720


def test_a_finished_video_asks_jellyfin_for_a_refresh(tmp_path):
    jellyfin = FakeJellyfin().start()
    try:
        c = make_client(tmp_path, JELLYFIN_URL=jellyfin.url, JELLYFIN_API_KEY=JELLYFIN_KEY)
        try:
            c.app.state.jellyfin.quiet_seconds = 0
            post(c)
            wait_for(c, "done")
            assert c.app.state.jellyfin.pending
        finally:
            c.__exit__(None, None, None)
    finally:
        jellyfin.stop()


def test_a_slow_download_does_not_block_the_server(tmp_path):
    release = threading.Event()

    def slow(url, options):
        release.wait(5)
        return fake_download()(url, options)

    c = make_client(tmp_path, download=slow)
    try:
        started = time.monotonic()
        assert post(c).status_code == 201
        assert time.monotonic() - started < 2
        assert client_health(c) == 200
        wait_for(c, "downloading")
        release.set()
        wait_for(c, "done")
    finally:
        release.set()
        c.__exit__(None, None, None)


def client_health(c) -> int:
    return c.get("/health").status_code


# --- configuration --------------------------------------------------------------------------------------------------------


def test_max_height_defaults_to_1080_and_accepts_0():
    assert Settings.from_env(ENV).youtube_max_height == 1080
    assert Settings.from_env({**ENV, "YOUTUBE_MAX_HEIGHT": "0"}).youtube_max_height == 0
    assert Settings.from_env({**ENV, "YOUTUBE_MAX_HEIGHT": "2160"}).youtube_max_height == 2160


@pytest.mark.parametrize("value", ["abc", "100", "9999", "-1", "10.5"])
def test_a_bad_max_height_is_reported(value):
    with pytest.raises(ConfigError, match="YOUTUBE_MAX_HEIGHT"):
        Settings.from_env({**ENV, "YOUTUBE_MAX_HEIGHT": value})


def test_ffmpeg_is_installed_in_the_image():
    dockerfile = (Path(__file__).resolve().parents[2] / "app" / "Dockerfile").read_text()
    assert "ffmpeg" in dockerfile
    assert "--no-install-recommends" in dockerfile
