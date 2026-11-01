"""A real audio-only download as a podcast episode, inside the real app image (issue #35).
Needs Docker AND internet. Opt-in:

    YTP_NETWORK_TESTS=1 make test-system

Builds app/Dockerfile (ffmpeg and ffprobe are in it), then, as an arbitrary non-root user like
docker-compose.yml does, downloads the audio of the Blender Foundation's Creative Commons film
"Big Buck Bunny", tags it, files it like a podcast episode, and reads the result back with ffprobe.
Proves what the unit tests (which fake yt-dlp and ffmpeg) cannot: that yt-dlp's audio extraction
keeps AAC/Opus WITHOUT re-encoding, that the ffmpeg tag command is accepted and the tags are
really in the file, that the thumbnail is attached as the cover, and the file name.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest

from tests.helpers import docker
from tests.helpers.compose_cli import REPO_ROOT

pytestmark = [
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("YTP_NETWORK_TESTS") != "1",
        reason="set YTP_NETWORK_TESTS=1 (needs internet)",
    ),
]

SCRIPT = """
from pathlib import Path
from ytp.audiotags import Tags, embed_tags
from ytp.podcasts import place_episode
from ytp.youtube import parse_video_url
from ytp.youtube_download import YoutubeDownloader, upload_date

ref = parse_video_url("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
done = YoutubeDownloader(min_free_bytes=0).download(ref, Path("/data/work"), audio_only=True)
published = upload_date(done.info)
tags = Tags(title="Big Buck Bunny", artist="Blender Cast", album="Blender Cast",
            album_artist="Blender Cast", published=published, genre="Podcast")
print("tagged", embed_tags(done.video, tags, done.thumbnail))
target = place_episode(done.video, Path("/data/Podcasts"), "Blender Cast", published,
                       "Big Buck Bunny", done.video.suffix.lower())
print("filed", target.name)
"""


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = docker.unique_name("app-audio")
    built = docker.run(["build", "-t", tag, str(REPO_ROOT / "app")], timeout=1800)
    assert built.returncode == 0, built.stdout + built.stderr
    yield tag
    docker.run(["rmi", "-f", tag])


def test_a_real_video_becomes_a_tagged_audio_episode(image, tmp_path):
    data = tmp_path / "data"
    (data / "Podcasts").mkdir(parents=True)
    data.chmod(0o777)
    (data / "Podcasts").chmod(0o777)
    base = ["run", "--rm", "--user", "4321:4321", "-v", f"{data}:/data", image]
    result = docker.run([*base, "python", "-c", SCRIPT], timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tagged True" in result.stdout, result.stdout + result.stderr

    folder = data / "Podcasts" / "Blender Cast"
    files = sorted(p.name for p in folder.iterdir())
    assert len(files) == 1, files
    episode = files[0]
    assert episode.startswith("2014-02-10 - Big Buck Bunny.")
    assert episode.endswith((".m4a", ".opus"))
    size = (folder / episode).stat().st_size
    assert 100_000 < size < 60_000_000

    probe = docker.run(
        [
            *base,
            "ffprobe", "-v", "error", "-of", "json", "-show_format", "-show_streams",
            f"/data/Podcasts/Blender Cast/{episode}",
        ]
    )  # fmt: skip
    assert probe.returncode == 0, probe.stdout + probe.stderr
    info = json.loads(probe.stdout)
    tags = {k.lower(): v for k, v in info["format"]["tags"].items()}
    assert tags["title"] == "Big Buck Bunny"
    assert tags["album"] == "Blender Cast"
    assert tags["artist"] == "Blender Cast"
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["codec_name"] in {"aac", "opus"}, "copied, not converted to something else"
    if episode.endswith(".m4a"):
        covers = [s for s in info["streams"] if s["codec_type"] == "video"]
        assert covers, "the thumbnail is attached as the cover"
        assert covers[0]["disposition"]["attached_pic"] == 1
