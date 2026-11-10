"""YouTube videos as music tracks, optionally audio only (issue #36)."""

# ruff: noqa: E501

from __future__ import annotations

import pytest

from tests.unit.test_youtube_download import ID, World, fake_download, make_client, post, wait_for
from ytp import jobs
from ytp.music import place_track, track_filename
from ytp.podcasts import PodcastError
from ytp.youtube_jobs import JOB_TYPE, YoutubeJobMetadata, job_external_id

# --- the file name and the folder -------------------------------------------------------------


def test_a_track_is_filed_like_a_torrent_album(tmp_path):
    library = tmp_path / "Musik"
    library.mkdir()
    source = tmp_path / "t.part"
    source.write_bytes(b"audio")
    target = place_track(source, library, "Kraftwerk", "Autobahn", "Autobahn", ".m4a")
    assert target == library / "Kraftwerk" / "Autobahn" / "Autobahn.m4a"
    assert target.read_bytes() == b"audio"
    assert not source.exists()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Ärger & Freude", "Ärger & Freude.m4a"),
        ("AC/DC - Thunderstruck", "AC-DC - Thunderstruck.m4a"),
        ("..", "Track.m4a"),
        ("   ", "Track.m4a"),
        ("a\nb", "a b.m4a"),
    ],
)
def test_track_names_are_safe(title, expected):
    assert track_filename(title, ".m4a") == expected


def test_a_long_title_is_shortened_to_fit():
    name = track_filename("ä" * 300, ".opus")
    assert len(name.encode()) <= 255 - len(" (99)")
    assert name.endswith(".opus")


@pytest.mark.security
def test_nothing_is_overwritten_and_symlinks_do_not_lead_out(tmp_path):
    library = tmp_path / "Musik"
    library.mkdir()
    first = tmp_path / "a.part"
    first.write_bytes(b"one")
    place_track(first, library, "A", "B", "T", ".m4a")
    second = tmp_path / "b.part"
    second.write_bytes(b"second")
    assert place_track(second, library, "A", "B", "T", ".m4a").name == "T (2).m4a"
    assert (library / "A" / "B" / "T.m4a").read_bytes() == b"one"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (library / "Evil").symlink_to(outside)
    third = tmp_path / "c.part"
    third.write_bytes(b"x")
    with pytest.raises(PodcastError):
        place_track(third, library, "Evil", "B", "T", ".m4a")
    assert list(outside.iterdir()) == [], "the symlinked artist folder was not written through"


# --- the job -----------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def submit(
    world, audio_only=True, artist="Blender", album="Big Buck Bunny", title="Big Buck Bunny"
):
    meta = YoutubeJobMetadata(
        video_id=ID, channel=artist, title=title, target="music", audio_only=audio_only, album=album
    )
    return jobs.submit_job(world.db, JOB_TYPE, job_external_id(ID, "music"), meta.to_json())[0]


def music_tree(world):
    root = world.data / "Musik"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture
def tagged(monkeypatch):
    calls = []

    def fake_embed(path, tags, cover=None, **kwargs):
        calls.append((path.name, tags, cover.name if cover else None))
        return True

    monkeypatch.setattr("ytp.youtube_jobs.embed_tags", fake_embed)
    return calls


def test_audio_only_becomes_a_tagged_track(world, tagged):
    job = submit(world)
    world.worker(download=fake_download(("m4a", "jpg"))).run_one()
    assert world.status(job.id) == ("done", None)
    assert music_tree(world) == ["Blender/Big Buck Bunny/Big Buck Bunny.m4a"]
    assert world.library() == []
    (name, tags, cover) = tagged[0]
    assert (name, cover) == (f"{ID}.m4a", f"{ID}.jpg")
    assert (tags.title, tags.artist, tags.album, tags.album_artist) == (
        "Big Buck Bunny",
        "Blender",
        "Big Buck Bunny",
        "Blender",
    )
    assert tags.genre == ""
    assert world.requested == ["Musik"]


def test_the_video_as_music_keeps_the_picture_beside_it(world, tagged):
    submit(world, audio_only=False)
    world.worker().run_one()
    assert music_tree(world) == [
        "Blender/Big Buck Bunny/Big Buck Bunny.jpg",
        "Blender/Big Buck Bunny/Big Buck Bunny.mp4",
    ]
    assert tagged == []


def test_a_thumbnail_that_cannot_be_filed_does_not_fail_the_track(
    world, tagged, monkeypatch, caplog
):
    """The track is already in the library; the sidecar must not fail the job (#69)."""
    import errno
    import logging
    import shutil

    real_move = shutil.move

    def broken(source, target, *args, **kwargs):
        if str(target).endswith(".jpg"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_move(source, target, *args, **kwargs)

    monkeypatch.setattr("ytp.youtube_jobs.shutil.move", broken)
    job = submit(world, audio_only=False)
    with caplog.at_level(logging.WARNING, logger="ytp.youtube_jobs"):
        world.worker().run_one()
    assert world.status(job.id) == ("done", None)
    assert music_tree(world) == ["Blender/Big Buck Bunny/Big Buck Bunny.mp4"]
    assert "thumbnail not filed" in caplog.text


def test_several_tracks_of_one_album_share_the_folder(world, tagged):
    first = submit(world, album="YouTube", title="One")
    other = "bbbbbbbbbbb"
    meta = YoutubeJobMetadata(
        video_id=other,
        channel="Blender",
        title="Two",
        target="music",
        audio_only=True,
        album="YouTube",
    )
    jobs.submit_job(world.db, JOB_TYPE, job_external_id(other, "music"), meta.to_json())
    worker = world.worker(download=fake_download(("m4a", "jpg")))
    worker.run_one()
    worker.run_one()
    assert world.status(first.id)[0] == "done"
    assert music_tree(world) == ["Blender/YouTube/One.m4a", "Blender/YouTube/Two.m4a"]


def test_a_missing_music_library_fails_with_the_fix(world, tagged):
    (world.data / "Musik").rmdir()
    job = submit(world)
    world.worker(download=fake_download(("m4a", "jpg"))).run_one()
    status, reason = world.status(job.id)
    assert status == "failed"
    assert "Musik" in reason
    assert "make layout" in reason


def test_a_failed_tagging_still_files_the_track(world, monkeypatch):
    monkeypatch.setattr("ytp.youtube_jobs.embed_tags", lambda *a, **k: False)
    job = submit(world)
    world.worker(download=fake_download(("m4a", "jpg"))).run_one()
    assert world.status(job.id) == ("done", None)
    assert music_tree(world) == ["Blender/Big Buck Bunny/Big Buck Bunny.m4a"]


def test_the_three_targets_of_one_video_are_three_jobs(world, tagged):
    keys = {job_external_id(ID, t) for t in ("video", "podcast", "music")}
    assert keys == {ID, f"{ID}@podcast", f"{ID}@music"}


def test_music_needs_an_album_and_only_music_has_one():
    with pytest.raises(ValueError, match="album"):
        YoutubeJobMetadata(video_id=ID, channel="A", title="T", target="music", audio_only=True)
    with pytest.raises(ValueError, match="album"):
        YoutubeJobMetadata(video_id=ID, channel="A", title="T", target="podcast", album="X")
    with pytest.raises(ValueError, match="album"):
        YoutubeJobMetadata(video_id=ID, channel="A", title="T", album="X")


def test_the_stored_music_metadata():
    meta = YoutubeJobMetadata(
        video_id=ID, channel="A", title="T", target="music", audio_only=True, album="Al"
    )
    assert (
        meta.to_json()
        == '{"type":"youtube","video_id":"aqz-KE-bpKQ","channel":"A","title":"T","target":"music","audio_only":true,"album":"Al"}'
    )


# --- the endpoint ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    test_client = make_client(tmp_path, download=fake_download(("mp4", "m4a", "jpg")))
    yield test_client
    test_client.__exit__(None, None, None)


def test_the_endpoint_queues_a_track_and_the_worker_files_it(client, tagged):
    response = post(
        client,
        target="music",
        audio_only=True,
        channel="Kraftwerk",
        title="Autobahn",
        album="Autobahn",
    )
    assert response.status_code == 201
    assert response.json()["target"] == "music"
    wait_for(client, "done")
    (job,) = jobs.list_jobs(client.app.state.db)
    assert job.external_id == f"{ID}@music"
    assert job.metadata["album"] == "Autobahn"
    root = client.app.state.settings.data_root / "Musik"
    assert [str(p.relative_to(root)) for p in root.rglob("*.m4a")] == [
        "Kraftwerk/Autobahn/Autobahn.m4a"
    ]


def test_without_an_album_the_track_is_a_single(client, tagged):
    post(client, target="music", audio_only=True, channel="Kraftwerk", title="Das Model")
    (job,) = jobs.list_jobs(client.app.state.db)
    assert job.metadata["album"] == "Das Model"


def test_the_same_video_as_video_podcast_and_music(client, tagged):
    codes = [
        post(client).status_code,
        post(client, target="podcast", audio_only=True).status_code,
        post(client, target="music", audio_only=True).status_code,
        post(client, target="music", audio_only=True).status_code,
    ]
    assert codes == [201, 201, 201, 200]
    assert len(jobs.list_jobs(client.app.state.db)) == 3


@pytest.mark.parametrize(
    ("extra", "loc"),
    [
        ({"album": "X"}, "body"),  # an album without the music target
        ({"target": "podcast", "album": "X"}, "body"),
        ({"target": "music", "album": "a/b"}, "album"),
        ({"target": "music", "album": ""}, "album"),
        ({"target": "music", "album": "x" * 201}, "album"),
        ({"target": "music", "audio_only": "yes"}, "audio_only"),
    ],
)
def test_invalid_albums_are_field_errors(client, extra, loc):
    response = post(client, **extra)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == loc
    assert jobs.list_jobs(client.app.state.db) == []


def test_the_job_list_labels_a_track_with_artist_and_title(client, tagged):
    post(client, target="music", audio_only=True, channel="Kraftwerk", title="Autobahn")
    wait_for(client, "done")
    (listed,) = client.get("/api/jobs").json()["jobs"]
    assert listed["label"] == "Kraftwerk - Autobahn"
