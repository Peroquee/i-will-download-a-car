"""A real YouTube video as a tagged music track, inside the real app image (issue #36).
Needs Docker AND internet. Opt-in:

    YTP_NETWORK_TESTS=1 make test-system

Like test_c35_audio_only, for the music target: downloads the audio of the Blender Foundation's
Creative Commons film "Big Buck Bunny" (one track as a single), tags it, files it as
`Musik/<artist>/<album>/<title>.<ext>` and reads the tags back with ffprobe.
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
from ytp.music import place_track
from ytp.youtube import parse_video_url
from ytp.youtube_download import YoutubeDownloader, upload_date

ref = parse_video_url("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
done = YoutubeDownloader(min_free_bytes=0).download(ref, Path("/data/work"), audio_only=True)
tags = Tags(title="Big Buck Bunny", artist="Blender Foundation", album="Big Buck Bunny",
            album_artist="Blender Foundation", published=upload_date(done.info))
print("tagged", embed_tags(done.video, tags, done.thumbnail))
target = place_track(done.video, Path("/data/Musik"), "Blender Foundation", "Big Buck Bunny",
                     "Big Buck Bunny", done.video.suffix.lower())
print("filed", target.name)
"""


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = docker.unique_name("app-music")
    built = docker.run(["build", "-t", tag, str(REPO_ROOT / "app")], timeout=1800)
    assert built.returncode == 0, built.stdout + built.stderr
    yield tag
    docker.run(["rmi", "-f", tag])


def test_a_real_video_becomes_a_tagged_track(image, tmp_path):
    data = tmp_path / "data"
    (data / "Musik").mkdir(parents=True)
    data.chmod(0o777)
    (data / "Musik").chmod(0o777)
    base = ["run", "--rm", "--user", "4321:4321", "-v", f"{data}:/data", image]
    result = docker.run([*base, "python", "-c", SCRIPT], timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tagged True" in result.stdout, result.stdout + result.stderr

    album = data / "Musik" / "Blender Foundation" / "Big Buck Bunny"
    files = sorted(p.name for p in album.iterdir())
    assert len(files) == 1, files
    track = files[0]
    assert track in {"Big Buck Bunny.m4a", "Big Buck Bunny.opus"}, track
    assert 100_000 < (album / track).stat().st_size < 60_000_000

    probe = docker.run(
        [
            *base,
            "ffprobe", "-v", "error", "-of", "json", "-show_format", "-show_streams",
            f"/data/Musik/Blender Foundation/Big Buck Bunny/{track}",
        ]
    )  # fmt: skip
    assert probe.returncode == 0, probe.stdout + probe.stderr
    info = json.loads(probe.stdout)
    tags = {k.lower(): v for k, v in info["format"].get("tags", {}).items()}
    for stream in info["streams"]:
        tags |= {k.lower(): v for k, v in stream.get("tags", {}).items() if k.lower() not in tags}
    assert tags["title"] == "Big Buck Bunny"
    assert tags["artist"] == "Blender Foundation"
    assert tags["album"] == "Big Buck Bunny"
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["codec_name"] in {"aac", "opus"}
