"""scripts/host/create-layout.sh: creates only missing folders, never touches existing ones."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tests.helpers.shell import ScriptResult, run_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "host" / "create-layout.sh"

LIBRARIES = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
DOWNLOADS = ("movies", "series", "audiobooks", "music", "incomplete")


class Layout:
    def __init__(self, tmp: Path):
        self.root = tmp / "root"
        self.data = self.root / "data"
        self.data.mkdir(parents=True)
        self.env_file = tmp / "host.env"
        # As root (CI) any non-root owner can be created; otherwise only our own ids work.
        if os.geteuid() == 0:
            self.puid = self.pgid = 4242
        else:
            self.puid, self.pgid = os.getuid(), os.getgid()
        self.write_env()

    def write_env(self, **overrides: str | None) -> None:
        values: dict[str, str | None] = {
            "DATA_ROOT": "/data",
            "PUID": str(self.puid),
            "PGID": str(self.pgid),
            **LIBRARIES,
        }
        values.update(overrides)
        self.env_file.write_text(
            "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
        )

    def run(self, *args: str) -> ScriptResult:
        return run_script(
            SCRIPT,
            ["--env-file", str(self.env_file), *args],
            env={"CHECK_ROOT": str(self.root)},
        )


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def test_creates_every_download_and_library_folder(layout):
    result = layout.run()
    assert result.returncode == 0, result.stderr
    for name in DOWNLOADS:
        assert (layout.data / "downloads" / name).is_dir()
    for name in LIBRARIES.values():
        assert (layout.data / name).is_dir()


def test_non_ascii_library_names_are_created_exactly(layout):
    layout.run()
    assert "Hörbücher" in os.listdir(layout.data)


def test_new_folders_are_setgid_group_writable(layout):
    layout.run()
    mode = stat.S_IMODE((layout.data / "Filme").stat().st_mode)
    assert mode == 0o2775
    assert stat.S_IMODE((layout.data / "downloads" / "movies").stat().st_mode) == 0o2775


def test_new_folders_get_the_configured_owner(layout):
    layout.run()
    info = (layout.data / "Serien").stat()
    assert (info.st_uid, info.st_gid) == (layout.puid, layout.pgid)


def test_is_safe_to_run_twice(layout):
    layout.run()
    second = layout.run()
    assert second.returncode == 0
    assert "creating" not in second.stdout
    assert second.stdout.count("exists") == 1 + len(DOWNLOADS) + len(LIBRARIES)


@pytest.mark.security
def test_existing_folders_and_their_contents_are_left_alone(layout):
    movies = layout.data / "Filme"
    movie = movies / "Film (2020)" / "film.mkv"
    movie.parent.mkdir(parents=True)
    movie.write_text("x")
    movies.chmod(0o755)
    movie.chmod(0o640)
    before = (movies.stat().st_mode, movie.stat().st_mode, movie.stat().st_mtime_ns)
    layout.run()
    assert (movies.stat().st_mode, movie.stat().st_mode, movie.stat().st_mtime_ns) == before
    assert stat.S_IMODE(movies.stat().st_mode) == 0o755  # not forced to 2775
    assert movie.read_text() == "x"


def test_creates_only_what_is_missing(layout):
    (layout.data / "Filme").mkdir()
    (layout.data / "downloads").mkdir()
    result = layout.run()
    assert "exists   " + str(layout.data / "Filme") in result.stdout
    assert "creating " + str(layout.data / "Filme") not in result.stdout
    assert (layout.data / "Musik").is_dir()


def test_dry_run_changes_nothing(layout):
    result = layout.run("--dry-run")
    assert result.returncode == 0
    assert "+ install -d -m 2775" in result.stdout
    assert list(layout.data.iterdir()) == []


def test_a_missing_data_root_is_refused(layout):
    layout.write_env(DATA_ROOT="/nowhere")
    result = layout.run()
    assert result.returncode == 1
    assert "mount the media volume first" in result.stderr


@pytest.mark.parametrize("missing", ["MEDIA_DIR_MOVIES", "MEDIA_DIR_YOUTUBE", "DATA_ROOT"])
def test_a_missing_setting_is_refused(layout, missing):
    layout.write_env(**{missing: None})
    result = layout.run()
    assert result.returncode == 1
    assert missing in result.stderr
    assert list(layout.data.iterdir()) == []


@pytest.mark.security
@pytest.mark.parametrize("name", ["../escape", "a/b", "..", ".", "-rf", "/abs"])
def test_library_names_cannot_leave_data_root(layout, name):
    layout.write_env(MEDIA_DIR_MOVIES=name)
    result = layout.run()
    assert result.returncode == 1
    assert "single folder name" in result.stderr
    assert not (layout.root.parent / "escape").exists()


def test_two_libraries_cannot_share_a_folder(layout):
    layout.write_env(MEDIA_DIR_SERIES="Filme")
    result = layout.run()
    assert result.returncode == 1
    assert "used twice" in result.stderr


def test_an_existing_file_in_the_way_is_refused(layout):
    (layout.data / "Musik").write_text("not a folder")
    result = layout.run()
    assert result.returncode == 1
    assert "not a folder" in result.stderr


def test_root_ids_are_refused(layout):
    layout.write_env(PUID="0")
    assert layout.run().returncode == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root may create folders for anyone")
def test_other_owners_need_root(layout):
    layout.write_env(PUID=str(os.getuid() + 1), PGID=str(os.getgid() + 1))
    result = layout.run()
    assert result.returncode == 1
    assert "sudo" in result.stderr
    assert list(layout.data.iterdir()) == []


def test_the_env_file_is_read_not_executed(layout, tmp_path):
    marker = tmp_path / "pwned"
    layout.write_env(UMASK=f"$(touch {marker})")
    layout.run()
    assert not marker.exists()


def test_the_download_folder_names_match_the_qbittorrent_categories():
    # create-layout.sh and the category script must agree on the four torrent types.
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'DOWNLOAD_DIRS="movies series audiobooks music incomplete"' in text
