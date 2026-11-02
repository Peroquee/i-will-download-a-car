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
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
DOWNLOADS = ("movies", "series", "audiobooks", "music", "incomplete")


class Layout:
    def __init__(self, tmp: Path):
        self.root = tmp / "root"
        self.data = self.root / "data"
        self.data.mkdir(parents=True)
        self.env_file = tmp / "host.env"
        self.config = tmp / "repo" / "config" / "app"  # stands for ./config/app
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
            env={"CHECK_ROOT": str(self.root), "APP_CONFIG_DIR": str(self.config)},
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
    assert (
        second.stdout.count("exists") == 1 + len(DOWNLOADS) + len(LIBRARIES) + 1 + 1
    )  # +1: the app state folder, +1: the backup folder


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


# --------------------------------------------------------------------------- app state folder


def owner(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_uid, info.st_gid


def test_creates_the_app_state_folder_owned_by_the_app_user(layout):
    layout.config.parent.mkdir(parents=True)
    result = layout.run()
    assert result.returncode == 0, result.stderr
    assert owner(layout.config) == (layout.puid, layout.pgid)
    assert stat.S_IMODE(layout.config.stat().st_mode) == 0o2775


def test_creates_the_config_parent_for_the_repository_owner(layout):
    """config/ is not the app's: it stays with whoever owns the repository."""
    assert not layout.config.parent.exists()
    layout.run()
    repo = Path(__file__).resolve().parents[2]
    assert owner(layout.config.parent) == (repo.stat().st_uid, repo.stat().st_gid)
    assert stat.S_IMODE(layout.config.parent.stat().st_mode) == 0o755


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create a root-owned folder")
def test_hands_a_root_owned_state_folder_to_the_app_user(layout):
    """The VM case: Docker created ./config/app as root:root 755 before the app existed."""
    layout.config.mkdir(parents=True)
    layout.config.chmod(0o755)
    (layout.config / "app.db").write_text("db")
    (layout.config / "app.db-wal").write_text("wal")
    result = layout.run()
    assert result.returncode == 0, result.stderr
    assert "fixing" in result.stdout
    for path in (layout.config, layout.config / "app.db", layout.config / "app.db-wal"):
        assert owner(path) == (layout.puid, layout.pgid), path
    assert stat.S_IMODE(layout.config.stat().st_mode) == 0o2775


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create a root-owned folder")
def test_the_fix_is_not_recursive(layout):
    layout.config.mkdir(parents=True)
    sub = layout.config / "keep"
    sub.mkdir()
    (sub / "file").write_text("x")
    layout.run()
    assert owner(sub) == (0, 0)
    assert owner(sub / "file") == (0, 0)


def test_a_correct_state_folder_is_left_alone(layout):
    layout.run()
    (layout.config / "app.db").write_text("db")
    before = layout.config.stat().st_mtime_ns
    result = layout.run()
    assert f"exists   {layout.config}" in result.stdout
    assert "fixing" not in result.stdout
    assert layout.config.stat().st_mtime_ns == before
    assert (layout.config / "app.db").read_text() == "db"


def test_dry_run_changes_nothing_for_the_state_folder(layout):
    result = layout.run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert not layout.config.exists()
    assert not layout.config.parent.exists()
    assert (
        f"+ install -d -m 2775 -o {layout.puid} -g {layout.pgid} {layout.config}" in result.stdout
    )


def test_a_file_in_place_of_the_state_folder_is_an_error(layout):
    layout.config.parent.mkdir(parents=True)
    layout.config.write_text("x")
    result = layout.run()
    assert result.returncode != 0
    assert "not a folder" in result.stderr


# --------------------------------------------------------------------------- symbolic links


def make_victim(tmp_path: Path) -> Path:
    """A folder with a file that must never be changed: stands for /etc."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "passwd").write_text("root:x:0:0\n")
    victim.chmod(0o750)
    return victim


def snapshot(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


def test_a_symlinked_state_folder_is_refused_and_its_target_untouched(layout, tmp_path):
    victim = make_victim(tmp_path)
    before = snapshot(victim), snapshot(victim / "passwd")
    layout.config.parent.mkdir(parents=True)
    layout.config.symlink_to(victim)
    result = layout.run()
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert (snapshot(victim), snapshot(victim / "passwd")) == before
    assert layout.config.is_symlink()


def test_a_dangling_symlink_is_refused_not_created_through(layout, tmp_path):
    layout.config.parent.mkdir(parents=True)
    layout.config.symlink_to(tmp_path / "does-not-exist")
    result = layout.run()
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert not (tmp_path / "does-not-exist").exists()


def test_the_refusal_also_applies_to_a_dry_run(layout, tmp_path):
    layout.config.parent.mkdir(parents=True)
    layout.config.symlink_to(make_victim(tmp_path))
    assert layout.run("--dry-run").returncode != 0


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create a root-owned folder")
@pytest.mark.parametrize("name", ["app.db", "app.db-wal", "app.db-shm"])
def test_a_symlinked_database_file_is_refused_before_anything_is_changed(layout, tmp_path, name):
    victim = make_victim(tmp_path)
    layout.config.mkdir(parents=True)
    (layout.config / name).symlink_to(victim / "passwd")
    result = layout.run()
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert owner(victim / "passwd") == (0, 0)
    assert owner(layout.config) == (0, 0), "nothing was changed before the refusal"


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create a root-owned folder")
def test_a_hardlinked_database_file_is_refused(layout, tmp_path):
    victim = make_victim(tmp_path)
    layout.config.mkdir(parents=True)
    os.link(victim / "passwd", layout.config / "app.db")
    result = layout.run()
    assert result.returncode != 0
    assert "hard link" in result.stderr
    assert owner(victim / "passwd") == (0, 0)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create a root-owned folder")
def test_a_normal_database_file_is_still_handed_over(layout):
    layout.config.mkdir(parents=True)
    (layout.config / "app.db").write_text("db")
    assert layout.run().returncode == 0
    assert owner(layout.config / "app.db") == (layout.puid, layout.pgid)


# --- the backup folder (issue #41) --------------------------------------------------------------

BACKUPS = ".youtube-premium-backups"


def test_the_backup_folder_is_created_private_and_owned_by_the_media_user(layout):
    result = layout.run()
    assert result.returncode == 0, result.stderr
    folder = layout.data / BACKUPS
    assert folder.is_dir()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700, "no setgid, no group or other bits"
    info = folder.stat()
    assert (info.st_uid, info.st_gid) == (layout.puid, layout.pgid)


def test_the_backup_folder_is_outside_downloads_and_every_library(layout):
    layout.run()
    folder = layout.data / BACKUPS
    assert folder.parent == layout.data
    assert BACKUPS.startswith("."), "hidden"
    assert BACKUPS not in {*LIBRARIES.values(), "downloads"}


def test_the_name_is_the_one_backup_py_uses():
    backup_py = (Path(__file__).resolve().parents[2] / "scripts" / "ops" / "backup.py").read_text()
    assert f'BACKUP_DIR_NAME = "{BACKUPS}"' in backup_py


@pytest.mark.security
def test_an_existing_backup_folder_and_its_archives_are_left_alone(layout):
    folder = layout.data / BACKUPS
    folder.mkdir()
    folder.chmod(0o750)
    archive = folder / "ytp-backup-20261102T090000Z.tar.gz"
    archive.write_text("precious")
    archive.chmod(0o640)
    before = (folder.stat().st_mode, archive.stat().st_mode, archive.stat().st_mtime_ns)
    result = layout.run()
    assert result.returncode == 0, result.stderr
    assert "contents and mode left as they are" in result.stdout
    assert (folder.stat().st_mode, archive.stat().st_mode, archive.stat().st_mtime_ns) == before
    assert archive.read_text() == "precious"


@pytest.mark.security
def test_a_symlinked_backup_folder_is_refused(layout):
    elsewhere = layout.data.parent / "elsewhere"
    elsewhere.mkdir()
    (layout.data / BACKUPS).symlink_to(elsewhere)
    result = layout.run()
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert list(elsewhere.iterdir()) == []


def test_a_file_where_the_backup_folder_should_be_is_refused(layout):
    (layout.data / BACKUPS).write_text("not a folder")
    result = layout.run()
    assert result.returncode != 0
    assert "not a folder" in result.stderr


def test_a_custom_backup_folder_in_the_env_file_creates_nothing_here(layout):
    layout.write_env(YTP_BACKUP_DIR="/mnt/nas/ytp-backups")
    result = layout.run()
    assert result.returncode == 0, result.stderr
    assert not (layout.data / BACKUPS).exists()
    assert "YTP_BACKUP_DIR" in result.stdout


def test_a_dry_run_does_not_create_the_backup_folder(layout):
    result = layout.run("--dry-run")
    assert result.returncode == 0
    assert not (layout.data / BACKUPS).exists()
    assert "install -d -m 700" in result.stdout
