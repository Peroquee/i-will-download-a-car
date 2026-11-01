"""YouTube videos as podcast episodes, optionally audio only (issue #35)."""

# ruff: noqa: E501

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from tests.unit.test_youtube_download import (
    ENV,
    ID,
    INFO,
    World,
    fake_download,
    make_client,
    post,
    wait_for,
)
from ytp import audiotags, jobs, subscriptions
from ytp.audiotags import Tags, embed_tags, tag_command
from ytp.podcasts import place_episode
from ytp.youtube import VideoRef
from ytp.youtube_download import (
    AUDIO_FORMAT,
    YoutubeDownloader,
    download_options,
    format_selector,
    upload_date,
)
from ytp.youtube_jobs import JOB_TYPE, YoutubeJobMetadata, job_external_id

# --- the yt-dlp options ---------------------------------------------------------------------------


def test_audio_only_asks_for_aac_first_and_copies_the_stream(tmp_path):
    options = download_options(
        tmp_path, proxy="", max_height=1080, socket_timeout=20, audio_only=True
    )
    assert options["format"] == AUDIO_FORMAT
    assert AUDIO_FORMAT.startswith("ba[ext=m4a]/ba[acodec^=mp4a]/ba")
    assert "merge_output_format" not in options
    keys = [step["key"] for step in options["postprocessors"]]
    assert keys == ["FFmpegThumbnailsConvertor", "FFmpegExtractAudio"]
    extract = options["postprocessors"][1]
    assert extract["preferredcodec"] == "best", "copy the stream, never re-encode"
    assert "preferredquality" not in extract


def test_the_video_options_are_unchanged(tmp_path):
    options = download_options(tmp_path, proxy="", max_height=1080, socket_timeout=20)
    assert options["format"] == format_selector(1080)
    assert options["merge_output_format"] == "mp4"
    assert [s["key"] for s in options["postprocessors"]] == ["FFmpegThumbnailsConvertor"]


def test_audio_only_keeps_all_the_safety_options(tmp_path):
    options = download_options(
        tmp_path, proxy="http://gluetun:8888", max_height=0, socket_timeout=9, audio_only=True
    )
    assert options["noplaylist"] is True
    assert options["allowed_extractors"] == ["youtube"]
    assert options["proxy"] == "http://gluetun:8888"
    assert options["outtmpl"] == {"default": "%(id)s.%(ext)s"}
    assert options["cachedir"] is False


@pytest.mark.parametrize("extension", ["m4a", "opus"])
def test_the_downloader_finds_the_audio_file(tmp_path, extension):
    seen = []
    downloader = YoutubeDownloader(
        download=fake_download((extension, "jpg"), seen=seen), min_free_bytes=0
    )
    done = downloader.download(VideoRef(ID), tmp_path / "w", audio_only=True)
    assert done.audio_only is True
    assert done.video.name == f"{ID}.{extension}"
    assert done.thumbnail is not None
    assert seen[0][1]["format"] == AUDIO_FORMAT


def test_a_video_download_does_not_mistake_an_audio_file_for_the_result(tmp_path):
    downloader = YoutubeDownloader(download=fake_download(("m4a", "jpg")), min_free_bytes=0)
    with pytest.raises(Exception, match="video file"):
        downloader.download(VideoRef(ID), tmp_path / "w")


def test_audio_only_without_an_audio_file_fails_clearly(tmp_path):
    downloader = YoutubeDownloader(download=fake_download(("mp4", "jpg")), min_free_bytes=0)
    with pytest.raises(Exception, match="audio file"):
        downloader.download(VideoRef(ID), tmp_path / "w", audio_only=True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20140210", date(2014, 2, 10)),
        ("", None),
        ("2014", None),
        ("20141340", None),
        ("abcdefgh", None),
        (None, None),
    ],
)
def test_the_upload_date(value, expected):
    assert upload_date({"upload_date": value}) == expected


# --- tags ----------------------------------------------------------------------------------------

TAGS = Tags(
    title="Folge 1: Ärger",
    artist="Blender",
    album="Blender",
    album_artist="Blender",
    published=date(2014, 2, 10),
    genre="Podcast",
    comment="https://www.youtube.com/watch?v=x",
)


def test_an_m4a_gets_tags_and_the_cover_without_re_encoding(tmp_path):
    command = tag_command(tmp_path / "a.m4a", tmp_path / "out.m4a", TAGS, tmp_path / "c.jpg")
    assert command[0] == "ffmpeg"
    assert "-nostdin" in command
    assert command[command.index("-c") + 1] == "copy"
    assert command.count("-i") == 2
    assert command[command.index("-disposition:v:0") : command.index("-disposition:v:0") + 2] == [
        "-disposition:v:0",
        "attached_pic",
    ]
    assert "title=Folge 1: Ärger" in command
    assert "album=Blender" in command
    assert "date=2014-02-10" in command
    assert "genre=Podcast" in command
    assert command[-1].endswith("out.m4a")


def test_an_mp3_gets_id3v2_3_and_a_named_cover(tmp_path):
    command = tag_command(tmp_path / "a.mp3", tmp_path / "out.mp3", TAGS, tmp_path / "c.jpg")
    assert command[command.index("-id3v2_version") + 1] == "3"
    assert "title=Album cover" in command


def test_opus_gets_tags_but_no_cover(tmp_path):
    command = tag_command(tmp_path / "a.opus", tmp_path / "out.opus", TAGS, tmp_path / "c.jpg")
    assert command.count("-i") == 1
    assert "attached_pic" not in command
    assert "title=Folge 1: Ärger" in command


def test_without_a_cover_there_is_one_input(tmp_path):
    command = tag_command(tmp_path / "a.m4a", tmp_path / "out.m4a", TAGS, None)
    assert command.count("-i") == 1


@pytest.mark.security
def test_a_tag_value_is_one_argument_and_cannot_start_an_option(tmp_path):
    nasty = Tags(
        title="x -y -i /etc/passwd\nartist=evil", artist="; rm -rf /", album="$(touch pwned)"
    )
    command = tag_command(tmp_path / "a.m4a", tmp_path / "out.m4a", nasty, None)
    values = [command[i + 1] for i, item in enumerate(command) if item == "-metadata"]
    assert values[0] == "title=x -y -i /etc/passwd artist=evil", (
        "control characters become spaces, one line"
    )
    assert all(v.split("=", 1)[0] in {"title", "artist", "album"} for v in values)
    assert command.count("-y") == 0
    assert len(values) == 3


def test_empty_tags_are_left_out_and_long_ones_are_cut():
    pairs = dict(Tags(title="t" * 5000, artist="  ").pairs())
    assert len(pairs["title"]) == audiotags.MAX_TAG_LENGTH
    assert "artist" not in pairs


class FakeRun:
    def __init__(self, returncode=0, write=True, error=None):
        self.returncode, self.write, self.error = returncode, write, error
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        assert kwargs["check"] is False
        assert kwargs["timeout"] == audiotags.TIMEOUT_SECONDS
        if self.error:
            raise self.error
        if self.write:
            Path(command[-1]).write_bytes(b"tagged-audio")
        return subprocess.CompletedProcess(command, self.returncode, "", "boom")


def test_tagging_replaces_the_file(tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"plain")
    run = FakeRun()
    assert embed_tags(audio, TAGS, None, runner=run) is True
    assert audio.read_bytes() == b"tagged-audio"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.m4a"], "no temporary file left"


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(returncode=1),
        FakeRun(write=False),
        FakeRun(error=FileNotFoundError("ffmpeg")),
        FakeRun(error=subprocess.TimeoutExpired("ffmpeg", 1)),
        FakeRun(error=OSError("disk full")),
    ],
)
def test_a_tagging_failure_keeps_the_untagged_file_and_never_raises(tmp_path, run):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"plain")
    assert embed_tags(audio, TAGS, None, runner=run) is False
    assert audio.read_bytes() == b"plain"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.m4a"]


def test_unsupported_or_missing_files_are_not_touched(tmp_path):
    run = FakeRun()
    other = tmp_path / "a.wav"
    other.write_bytes(b"x")
    assert embed_tags(other, TAGS, None, runner=run) is False
    assert embed_tags(tmp_path / "missing.m4a", TAGS, None, runner=run) is False
    assert run.commands == []


# --- the worker ----------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def submit(
    world,
    target="podcast",
    audio_only=True,
    podcast="Blender Cast",
    title="Big Buck Bunny",
    published=None,
):
    meta = YoutubeJobMetadata(
        video_id=ID,
        channel=podcast,
        title=title,
        target=target,
        audio_only=audio_only,
        published=published,
    )
    return jobs.submit_job(world.db, JOB_TYPE, job_external_id(ID, target), meta.to_json())[0]


def podcasts_tree(world):
    root = world.data / "Podcasts"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture
def tagged(monkeypatch):
    calls = []

    def fake_embed(path, tags, cover=None, **kwargs):
        calls.append((path.name, tags, cover.name if cover else None))
        return True

    monkeypatch.setattr("ytp.youtube_jobs.embed_tags", fake_embed)
    return calls


def test_audio_only_goes_to_the_podcast_folder_tagged_with_the_cover(world, tagged):
    job = submit(world)
    world.worker(download=fake_download(("m4a", "jpg"))).run_one()
    assert world.status(job.id) == ("done", None)
    assert podcasts_tree(world) == ["Blender Cast/2014-02-10 - Big Buck Bunny.m4a"]
    assert world.library() == [], "nothing in the Youtube library"
    (name, tags, cover) = tagged[0]
    assert (name, cover) == (f"{ID}.m4a", f"{ID}.jpg")
    assert (tags.title, tags.artist, tags.album, tags.genre) == (
        "Big Buck Bunny",
        "Blender Cast",
        "Blender Cast",
        "Podcast",
    )
    assert tags.published == date(2014, 2, 10)
    assert world.requested == ["Podcasts"]
    assert not (world.data / "downloads/incomplete/youtube" / ID).exists()


def test_opus_is_filed_with_its_own_extension(world, tagged):
    submit(world)
    world.worker(download=fake_download(("opus", "jpg"))).run_one()
    assert podcasts_tree(world) == ["Blender Cast/2014-02-10 - Big Buck Bunny.opus"]


def test_a_failed_tagging_does_not_fail_the_episode(world, monkeypatch):
    monkeypatch.setattr("ytp.youtube_jobs.embed_tags", lambda *a, **k: False)
    job = submit(world)
    world.worker(download=fake_download(("m4a", "jpg"))).run_one()
    assert world.status(job.id) == ("done", None)
    assert podcasts_tree(world) == ["Blender Cast/2014-02-10 - Big Buck Bunny.m4a"]


def test_the_video_as_an_episode_keeps_the_picture_beside_it(world, tagged):
    job = submit(world, audio_only=False)
    world.worker().run_one()
    assert world.status(job.id) == ("done", None)
    assert podcasts_tree(world) == [
        "Blender Cast/2014-02-10 - Big Buck Bunny.jpg",
        "Blender Cast/2014-02-10 - Big Buck Bunny.mp4",
    ]
    assert tagged == [], "a video is not tagged"


def test_the_feed_date_is_used_when_the_download_has_none(world, tagged):
    info = {k: v for k, v in INFO.items() if k != "upload_date"}
    submit(world, published="2026-05-06")
    world.worker(download=fake_download(("m4a", "jpg"), info=info)).run_one()
    assert podcasts_tree(world) == ["Blender Cast/2026-05-06 - Big Buck Bunny.m4a"]


def test_the_episode_shares_its_folder_with_an_rss_episode(world, tagged):
    rss = world.data / "Podcasts" / "Blender Cast" / "2014-02-10 - Big Buck Bunny.m4a"
    rss.parent.mkdir()
    rss.write_bytes(b"from the rss feed, a different file")
    submit(world)
    world.worker(download=fake_download(("m4a", "jpg"))).run_one()
    assert podcasts_tree(world) == [
        "Blender Cast/2014-02-10 - Big Buck Bunny (2).m4a",
        "Blender Cast/2014-02-10 - Big Buck Bunny.m4a",
    ]
    assert rss.read_bytes() == b"from the rss feed, a different file"


def test_the_same_video_can_be_a_video_and_an_episode(world, tagged):
    plain = submit(world, target="video", audio_only=False, podcast="Blender")
    episode = submit(world)
    assert plain.id != episode.id
    assert plain.external_id == ID
    assert episode.external_id == f"{ID}@podcast"
    worker = world.worker(download=fake_download(("mp4", "m4a", "jpg")))
    worker.run_one()
    worker.run_one()
    assert world.status(plain.id)[0] == "done"
    assert world.status(episode.id)[0] == "done"
    assert world.library() == [
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].info.json",
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].jpg",
        "Blender/Big Buck Bunny [aqz-KE-bpKQ].mp4",
    ]


def test_the_same_video_and_target_is_one_job(world):
    first = submit(world)
    second = submit(world)
    assert first.id == second.id


def test_a_job_made_before_the_targets_existed_still_runs(world):
    """Old rows have neither target nor audio_only: they mean "video"."""
    old = f'{{"type":"youtube","video_id":"{ID}","channel":"Blender","title":"Old"}}'
    job = jobs.submit_job(world.db, JOB_TYPE, ID, old)[0]
    world.worker().run_one()
    assert world.status(job.id) == ("done", None)
    assert world.library()[0].startswith("Blender/Old [")


def test_a_missing_podcast_library_fails_with_the_fix(world, tagged):
    (world.data / "Podcasts").rmdir()
    job = submit(world)
    world.worker(download=fake_download(("m4a", "jpg"))).run_one()
    status, reason = world.status(job.id)
    assert status == "failed"
    assert "Podcasts" in reason
    assert "make layout" in reason


def test_a_target_that_does_not_match_the_job_key_is_refused(world):
    meta = YoutubeJobMetadata(
        video_id=ID, channel="X", title="Y", target="podcast", audio_only=True
    )
    job = jobs.submit_job(world.db, JOB_TYPE, ID, meta.to_json())[0]  # key says video
    world.worker().run_one()
    assert world.status(job.id)[0] == "failed"
    assert podcasts_tree(world) == []


def test_audio_only_with_the_video_target_is_invalid_data():
    with pytest.raises(ValueError, match="audio_only"):
        YoutubeJobMetadata(video_id=ID, channel="X", title="Y", audio_only=True)


def test_plain_video_jobs_are_stored_exactly_as_before():
    meta = YoutubeJobMetadata(video_id=ID, channel="Blender", title="Big Buck Bunny")
    assert (
        meta.to_json()
        == '{"type":"youtube","video_id":"aqz-KE-bpKQ","channel":"Blender","title":"Big Buck Bunny"}'
    )


def test_the_published_date_must_be_a_date():
    for bad in ("yesterday", "2026-13-40", "2026-1-1"):
        with pytest.raises(ValueError, match=r"published|date"):
            YoutubeJobMetadata(video_id=ID, channel="X", title="Y", target="podcast", published=bad)


def test_place_episode_is_the_one_used_for_both_sources(tmp_path):
    """The RSS worker and the YouTube worker file through the same function."""
    assert place_episode.__module__ == "ytp.podcasts"


# --- the endpoint --------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    test_client = make_client(tmp_path, download=fake_download(("mp4", "m4a", "jpg")))
    yield test_client
    test_client.__exit__(None, None, None)


def test_the_endpoint_queues_an_audio_only_episode_and_the_worker_files_it(client, tagged):
    response = post(client, target="podcast", audio_only=True, channel="Blender Cast")
    assert response.status_code == 201
    assert response.json()["target"] == "podcast"
    wait_for(client, "done")
    (job,) = jobs.list_jobs(client.app.state.db)
    assert job.external_id == f"{ID}@podcast"
    assert job.metadata["audio_only"] is True
    assert job.metadata["target"] == "podcast"
    root = client.app.state.settings.data_root / "Podcasts"
    assert [p.name for p in root.rglob("*.m4a")] == ["2014-02-10 - Big Buck Bunny.m4a"]


def test_the_default_target_is_the_video(client):
    body = post(client).json()
    assert body["target"] == "video"
    wait_for(client, "done")
    assert jobs.list_jobs(client.app.state.db)[0].external_id == ID


def test_the_video_can_be_queued_again_for_another_target(client, tagged):
    first = post(client)
    second = post(client, target="podcast", audio_only=True)
    third = post(client, target="podcast", audio_only=True)
    assert (first.status_code, second.status_code, third.status_code) == (201, 201, 200)
    assert third.json()["duplicate"] is True
    assert third.json()["id"] == second.json()["id"]


@pytest.mark.parametrize(
    ("extra", "loc"),
    [
        ({"audio_only": True}, "body"),  # audio only needs the podcast target
        ({"target": "music"}, "target"),  # music comes with #36
        ({"target": "radio"}, "target"),
        ({"target": "podcast", "audio_only": "yes"}, "audio_only"),
        ({"target": "podcast", "audio_only": 1}, "audio_only"),
    ],
)
def test_invalid_targets_are_field_errors(client, extra, loc):
    response = post(client, **extra)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == loc
    assert jobs.list_jobs(client.app.state.db) == []


def test_a_failed_episode_can_be_retried_from_the_list(client, tagged):
    client.app.state.youtube_worker.downloader = YoutubeDownloader(
        download=fake_download(("mp4", "jpg")), min_free_bytes=0
    )  # no audio file: fails
    post(client, target="podcast", audio_only=True)
    job = wait_for(client, "failed")
    assert "audio file" in job.error
    client.app.state.youtube_worker.downloader = YoutubeDownloader(
        download=fake_download(("m4a", "jpg")), min_free_bytes=0
    )
    assert client.post(f"/api/jobs/{job.id}/retry").status_code == 200
    wait_for(client, "done", job.id)


# --- subscriptions with a target -----------------------------------------------------------------


def test_a_subscription_remembers_its_target(tmp_path):
    from ytp.db import Database

    db = Database.open(tmp_path / "app.db")
    plain, _ = subscriptions.add(db, "UC" + "a" * 22, "A")
    podcast, _ = subscriptions.add(db, "UC" + "b" * 22, "B", 0, "podcast", True)
    assert (plain.target, plain.audio_only) == ("video", False)
    assert (podcast.target, podcast.audio_only) == ("podcast", True)
    assert podcast.as_dict()["target"] == "podcast"
    assert subscriptions.get(db, podcast.id).audio_only is True
    db.close()


def test_the_database_refuses_an_unknown_target(tmp_path):
    import sqlite3

    from ytp.db import Database

    db = Database.open(tmp_path / "app.db")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO subscriptions (channel_id, channel_name, target) VALUES ('UCx', 'x', 'music')"
        )
    db.close()


def test_the_poller_queues_new_videos_for_the_subscriptions_target(tmp_path):
    from datetime import UTC, datetime, timedelta

    from tests.unit.test_feeds import entry, feed
    from tests.unit.test_subscription_poller import FakeFeeds, FakeWorker, make_poller
    from ytp.db import Database

    db = Database.open(tmp_path / "app.db")
    cid = "UCSMOQeBJ2RAnuFungnQOxLg"
    sub, _ = subscriptions.add(db, cid, "Blender Cast", 0, "podcast", True)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET created_at = '2026-10-01T00:00:00.000Z' WHERE id = ?",
            (sub.id,),
        )
    feeds = FakeFeeds()
    when = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    feeds.answers[cid] = feed(entry("aaaaaaaaaaa", "Fresh", when), channel=cid)
    poller = make_poller(db, feeds, worker=FakeWorker())
    assert poller.poll(subscriptions.get(db, sub.id)) == 1
    (job,) = jobs.list_jobs(db)
    assert job.external_id == "aaaaaaaaaaa@podcast"
    assert job.metadata["target"] == "podcast"
    assert job.metadata["audio_only"] is True
    assert job.metadata["channel"] == "Blender Cast"
    assert job.metadata["published"] == when[:10]
    db.close()


def test_the_subscription_api_takes_a_target(tmp_path):
    from starlette.testclient import TestClient

    from ytp.config import Settings
    from ytp.main import create_app

    config = tmp_path / "config"
    config.mkdir()
    settings = Settings.from_env(
        {
            **ENV,
            "DATA_ROOT": str(tmp_path),
            "CONFIG_DIR": str(config),
            "SUBSCRIPTION_POLL_MINUTES": "0",
            "PODCAST_POLL_MINUTES": "0",
        }
    )
    cid = "UCSMOQeBJ2RAnuFungnQOxLg"
    with TestClient(create_app(settings), base_url="http://nas.lan") as test_client:
        resolver = test_client.app.state.channels

        async def resolve(ref):
            from ytp.channels import Channel

            return Channel(cid, "Blender")

        resolver.resolve = resolve
        for bad in (
            {"audio_only": True},
            {"target": "music"},
            {"target": "podcast", "audio_only": "x"},
        ):
            response = test_client.post("/api/subscriptions", json={"channel": cid, **bad})
            assert response.status_code == 422, bad
        ok = test_client.post(
            "/api/subscriptions",
            json={"channel": cid, "target": "podcast", "audio_only": True, "name": "Mein Podcast"},
        )
        assert ok.status_code == 201
        assert (ok.json()["target"], ok.json()["audio_only"], ok.json()["channel_name"]) == (
            "podcast",
            True,
            "Mein Podcast",
        )
        listed = test_client.get("/api/subscriptions").json()["subscriptions"][0]
        assert (listed["target"], listed["audio_only"]) == ("podcast", True)
