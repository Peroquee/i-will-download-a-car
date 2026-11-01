"""A real YouTube download inside the real app image (issue #18). Needs Docker AND internet.

    YTP_NETWORK_TESTS=1 make test-system

Builds app/Dockerfile (so ffmpeg is really in the image), then runs the downloader in it, as an
arbitrary non-root user like docker-compose.yml does, for the Blender Foundation's Creative
Commons film "Big Buck Bunny" at the lowest quality (144 px, a few MB), and files it into a
temporary library. Proves what the unit tests (which fake yt-dlp and ffmpeg) cannot: the merge to
mp4, the thumbnail conversion, the file names and that the summary has no stream addresses.
Directly from this host; add `-e YTDLP_PROXY=...` below to try the VPN route by hand.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

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
from ytp.youtube import parse_video_url
from ytp.youtube_download import YoutubeDownloader, place

ref = parse_video_url("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
done = YoutubeDownloader(max_height=144, min_free_bytes=0).download(ref, Path("/data/work"))
place(done, Path("/data/Youtube"), "Blender", "Big Buck Bunny", ref.video_id)
print("filed")
"""


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = docker.unique_name("app-download")
    built = docker.run(["build", "-t", tag, str(REPO_ROOT / "app")], timeout=1800)
    assert built.returncode == 0, built.stdout + built.stderr
    yield tag
    docker.run(["rmi", "-f", tag])


def test_ffmpeg_is_in_the_image(image):
    result = docker.run(["run", "--rm", image, "ffmpeg", "-version"])
    assert result.returncode == 0
    assert result.stdout.startswith("ffmpeg version")


def test_a_real_video_is_downloaded_merged_and_filed(image, tmp_path):
    data = tmp_path / "data"
    (data / "Youtube").mkdir(parents=True)
    data.chmod(0o777)
    (data / "Youtube").chmod(0o777)
    result = docker.run(
        [
            "run",
            "--rm",
            "--user",
            "4321:4321",
            "-v",
            f"{data}:/data",
            image,
            "python",
            "-c",
            SCRIPT,
        ],
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    folder = data / "Youtube" / "Blender"
    names = sorted(p.name for p in folder.iterdir())
    assert names == [
        "Big Buck Bunny [aqz-KE-bpKQ].info.json",
        "Big Buck Bunny [aqz-KE-bpKQ].jpg",
        "Big Buck Bunny [aqz-KE-bpKQ].mp4",
    ]
    assert (folder / "Big Buck Bunny [aqz-KE-bpKQ].mp4").stat().st_size > 100_000
    info = json.loads((folder / "Big Buck Bunny [aqz-KE-bpKQ].info.json").read_text())
    assert info["id"] == "aqz-KE-bpKQ"
    assert "googlevideo" not in json.dumps(info)
    assert not Path(data / "work").exists() or not list((data / "work").glob("*.part"))
