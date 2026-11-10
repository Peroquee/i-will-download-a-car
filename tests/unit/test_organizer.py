"""app/ytp/organizer.py on real folders: naming, hardlinks, junk, packs, safety, repeatability."""

from __future__ import annotations

import errno
import logging
import os
import unicodedata
from pathlib import Path

import pytest

from ytp import organizer as org
from ytp.clients.jellyfin import LibraryRefresher
from ytp.clients.qbittorrent import Torrent
from ytp.config import Settings
from ytp.jobs import Job
from ytp.reconciler import OrganizeError

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
MOVIE = {"type": "movies", "title": "Big Buck Bunny", "year": 2008}
EPISODE = {"type": "series", "title": "Tatort", "season": 3, "episode": 7}
PACK = {"type": "series", "title": "Tatort", "season": 3}
BOOK = {"type": "audiobooks", "author": "Michael Ende", "title": "Momo"}
ALBUM = {"type": "music", "artist": "Kraftwerk", "album": "Autobahn"}


class Refresher(LibraryRefresher):
    def __init__(self) -> None:
        super().__init__(None)
        self.requested: list[str | None] = []
        self.immediate: list[bool] = []

    def request_refresh(self, folder: str | None = None, *, immediate: bool = False) -> None:
        self.requested.append(folder)
        self.immediate.append(immediate)


class World:
    """A fake /data with downloads/ and the five library folders."""

    def __init__(self, tmp: Path):
        self.data = tmp / "data"
        self.downloads = self.data / "downloads" / "movies"
        self.downloads.mkdir(parents=True)
        for name in ENV.values():
            (self.data / name).mkdir()
        self.settings = Settings.from_env({**ENV, "DATA_ROOT": str(self.data)})
        self.refresher = Refresher()
        self.organizer = org.FileOrganizer(self.settings, self.refresher)

    def make(self, relative: str, size: int = 1000, content: bytes | None = None) -> Path:
        path = self.downloads / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if content is not None else b"x" * size)
        return path

    def run(self, metadata: dict, content: str, priority: int = 0) -> str | None:
        job = Job(1, "magnet", "a" * 40, metadata, "organizing", None, "", "", priority=priority)
        path = str(self.downloads / content)
        torrent = Torrent.from_api(
            {
                "hash": "a" * 40,
                "name": content,
                "state": "uploading",
                "progress": 1,
                "content_path": path,
            }
        )
        return self.organizer(job, torrent)

    def tree(self, library: str) -> list[str]:
        root = self.data / library
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# --- movies ----------------------


def test_a_movie_folder_becomes_title_year_folder_and_file(world):
    world.make("Big.Buck.Bunny.2008.1080p/Big.Buck.Bunny.2008.1080p.mkv", 5000)
    world.run(MOVIE, "Big.Buck.Bunny.2008.1080p")
    assert world.tree("Filme") == ["Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"]


def test_a_single_file_torrent(world):
    world.make("Movie.mp4", 100)
    world.run(MOVIE, "Movie.mp4")
    assert world.tree("Filme") == ["Big Buck Bunny (2008)/Big Buck Bunny (2008).mp4"]


def test_extension_is_lower_cased_and_only_the_largest_video_is_used(world):
    world.make("rel/Movie.MKV", 9000)
    world.make("rel/Movie.CD2.avi", 500)
    world.run(MOVIE, "rel")
    assert world.tree("Filme") == ["Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"]


def test_subtitles_next_to_the_video_keep_their_language_suffix(world):
    world.make("rel/Movie.2008.mkv", 9000)
    world.make("rel/Movie.2008.de.srt", 10)
    world.make("rel/Movie.2008.en.forced.srt", 10)
    world.make("rel/Subs/other.srt", 10)
    world.run(MOVIE, "rel")
    assert world.tree("Filme") == [
        "Big Buck Bunny (2008)/Big Buck Bunny (2008).de.srt",
        "Big Buck Bunny (2008)/Big Buck Bunny (2008).en.forced.srt",
        "Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv",
        "Big Buck Bunny (2008)/Big Buck Bunny (2008).srt",
    ]


def test_no_video_is_an_organize_error(world):
    world.make("rel/readme.txt")
    with pytest.raises(OrganizeError, match="no video file"):
        world.run(MOVIE, "rel")
    assert world.tree("Filme") == []


# --- hardlinks: never move ----------------------


def test_the_library_file_is_a_hardlink_and_the_original_is_untouched(world):
    source = world.make("rel/Movie.mkv", content=b"movie data")
    inode = source.stat().st_ino
    world.run(MOVIE, "rel")
    target = world.data / "Filme/Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"
    assert os.path.samestat(source.stat(), target.stat())
    assert source.stat().st_nlink == 2
    assert source.exists()
    assert source.stat().st_ino == inode
    assert source.read_bytes() == b"movie data"
    assert (world.downloads / "rel").is_dir(), "the torrent's folder stays as it was"


def test_running_it_again_is_fine_and_changes_nothing(world):
    world.make("rel/Movie.mkv", 100)
    world.run(MOVIE, "rel")
    world.run(MOVIE, "rel")
    assert len(world.tree("Filme")) == 1
    assert (world.downloads / "rel/Movie.mkv").stat().st_nlink == 2


def test_a_different_file_at_the_target_is_never_overwritten(world):
    world.make("rel/Movie.mkv", content=b"new")
    existing = world.data / "Filme/Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"the owner's own copy")
    with pytest.raises(OrganizeError, match="already exist in the library with other content"):
        world.run(MOVIE, "rel")
    assert existing.read_bytes() == b"the owner's own copy"


def test_a_conflict_among_other_files_is_skipped_not_fatal(world, caplog):
    world.make("rel/Movie.mkv", 100)
    world.make("rel/Movie.de.srt", 10)
    clash = world.data / "Filme/Big Buck Bunny (2008)/Big Buck Bunny (2008).de.srt"
    clash.parent.mkdir(parents=True)
    clash.write_text("mine")
    with caplog.at_level(logging.WARNING, logger="ytp.organizer"):
        world.run(MOVIE, "rel")
    assert clash.read_text() == "mine"
    assert "Big Buck Bunny (2008).mkv" in "".join(world.tree("Filme"))
    assert "not overwriting" in caplog.text


def test_files_that_were_not_overwritten_are_reported_in_the_note(world):
    """A conflict does not fail the job, but the note says a file was left out (#69)."""
    world.make("rel/Movie.mkv", 100)
    world.make("rel/Movie.de.srt", 10)
    clash = world.data / "Filme/Big Buck Bunny (2008)/Big Buck Bunny (2008).de.srt"
    clash.parent.mkdir(parents=True)
    clash.write_text("mine")
    note = world.run(MOVIE, "rel")
    assert note == (
        "1 of 2 files already in the library with other content, not overwritten: "
        "Big Buck Bunny (2008)/Big Buck Bunny (2008).de.srt"
    )


def test_a_pack_that_skips_files_says_so_in_the_note(world):
    """The job stays `done`, but what was left out is visible without reading the log (#69)."""
    world.make("pack/Tatort.S03E01.mkv", 100)
    world.make("pack/Tatort.S03E02.mkv", 100)
    world.make("pack/Tatort.Behind.The.Scenes.mkv", 100)
    note = world.run(PACK, "pack")
    assert note == (
        "1 of 3 files skipped: Tatort.Behind.The.Scenes.mkv: no episode number in the name"
    )


def test_a_completely_filed_job_has_no_note(world):
    world.make("rel/Movie.mkv", 100)
    assert world.run(MOVIE, "rel") is None


def test_a_symlink_at_the_target_is_not_followed(world, tmp_path):
    world.make("rel/Movie.mkv", 100)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    target = world.data / "Filme/Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"
    target.parent.mkdir(parents=True)
    target.symlink_to(victim)
    with pytest.raises(OrganizeError):
        world.run(MOVIE, "rel")
    assert victim.read_text() == "precious"


def test_across_filesystems_the_file_is_copied_not_linked(world, monkeypatch, caplog):
    source = world.make("rel/Movie.mkv", content=b"payload")

    def cross_device(src, dst, *a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(org.os, "link", cross_device)
    with caplog.at_level(logging.WARNING, logger="ytp.organizer"):
        world.run(MOVIE, "rel")
    target = world.data / "Filme/Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"
    assert target.read_bytes() == b"payload"
    assert not os.path.samestat(source.stat(), target.stat())
    assert source.exists(), "the original stays for seeding"
    assert "copied instead of hardlinked" in caplog.text
    assert [p.name for p in target.parent.iterdir()] == [target.name], "no temp file left"


def test_other_link_errors_are_errors_not_silent_copies(world, monkeypatch):
    world.make("rel/Movie.mkv", 100)

    def refused(src, dst, *a, **k):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(org.os, "link", refused)
    with pytest.raises(OrganizeError, match=r"cannot link Movie\.mkv"):
        world.run(MOVIE, "rel")
    assert world.tree("Filme") == []


def test_a_failing_copy_leaves_no_partial_file(world, monkeypatch):
    world.make("rel/Movie.mkv", 100)
    monkeypatch.setattr(
        org.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "x"))
    )

    def broken_copy(src, dst, *a, **k):
        Path(dst).write_bytes(b"half")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(org.shutil, "copyfile", broken_copy)
    with pytest.raises(OrganizeError, match="No space left"):
        world.run(MOVIE, "rel")
    assert world.tree("Filme") == []
    assert [p for p in (world.data / "Filme").rglob("*") if p.is_file()] == []


# --- junk ----------------------


def test_junk_samples_and_screenshots_are_left_out(world):
    world.make("rel/Movie.mkv", 9000)
    world.make("rel/Movie.nfo")
    world.make("rel/RARBG.txt")
    world.make("rel/Torrent downloaded from site.url")
    world.make("rel/Movie.sfv")
    world.make("rel/Sample/sample-movie.mkv", 800)
    world.make("rel/movie-sample.mkv", 700)
    world.make("rel/Screens/shot1.jpg")
    world.make("rel/poster.jpg")
    world.make("rel/setup.exe")
    world.run(MOVIE, "rel")
    assert world.tree("Filme") == ["Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"]


def test_a_sample_looking_word_inside_a_real_name_is_not_junk(world):
    world.make("rel/Examples.of.Samplers.2008.mkv", 9000)
    world.run(MOVIE, "rel")
    assert len(world.tree("Filme")) == 1


# --- series ----------------------


def test_a_single_episode(world):
    world.make("Tatort.S03E07.720p.mkv", 5000)
    world.make("Tatort.S03E07.720p.de.srt", 10)
    world.run(EPISODE, "Tatort.S03E07.720p.mkv")
    # A single-file torrent: only the file itself is in play.
    assert world.tree("Serien") == ["Tatort/Season 03/Tatort - S03E07.mkv"]


def test_a_single_episode_folder_uses_the_metadata_not_the_file_name(world):
    world.make("rel/some.weird.name.mkv", 5000)
    world.make("rel/some.weird.name.de.srt", 10)
    world.run(EPISODE, "rel")
    assert world.tree("Serien") == [
        "Tatort/Season 03/Tatort - S03E07.de.srt",
        "Tatort/Season 03/Tatort - S03E07.mkv",
    ]


def test_a_season_pack_maps_every_episode_into_the_season_folder(world):
    for n in (1, 2, 3):
        world.make(f"Tatort.S03.1080p/Tatort.S03E0{n}.1080p.mkv", 1000 + n)
    world.make("Tatort.S03.1080p/Tatort.S03E02.1080p.de.srt", 10)
    world.make("Tatort.S03.1080p/info.nfo")
    world.run(PACK, "Tatort.S03.1080p")
    assert world.tree("Serien") == [
        "Tatort/Season 03/Tatort - S03E01.mkv",
        "Tatort/Season 03/Tatort - S03E02.de.srt",
        "Tatort/Season 03/Tatort - S03E02.mkv",
        "Tatort/Season 03/Tatort - S03E03.mkv",
    ]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Show.S03E07.mkv", "S03E07"),
        ("show.s03e07.mkv", "S03E07"),
        ("Show.S3E7.mkv", "S03E07"),
        ("Show 3x07 title.mkv", "S03E07"),
        ("Show - S03E07E08 - double.mkv", "S03E07-E08"),
        ("Show.S03E07-E09.mkv", "S03E07-E09"),
        ("Show.S03E07.1080p.x264.mkv", "S03E07"),
    ],
)
def test_episode_numbers_are_read_from_the_file_name(world, filename, expected):
    world.make(f"pack/{filename}", 1000)
    world.run(PACK, "pack")
    assert world.tree("Serien") == [f"Tatort/Season 03/Tatort - {expected}.mkv"]


def test_episode_only_names_use_the_season_folder_or_the_metadata(world):
    world.make("pack/Season 3/E05 - Title.mkv", 1000)
    world.make("pack/Staffel 4/Episode 06.mkv", 1000)
    world.make("pack/Ep08.mkv", 1000)
    world.run(PACK, "pack")
    assert world.tree("Serien") == [
        "Tatort/Season 03/Tatort - S03E05.mkv",
        "Tatort/Season 03/Tatort - S03E08.mkv",
        "Tatort/Season 04/Tatort - S04E06.mkv",
    ]


def test_a_season_in_the_file_name_wins_over_the_metadata(world):
    world.make("pack/Tatort.S04E01.mkv", 1000)
    world.run(PACK, "pack")
    assert world.tree("Serien") == ["Tatort/Season 04/Tatort - S04E01.mkv"]


# --- several seasons in one pack (#52) ----------------------


def test_a_multi_season_pack_files_each_file_into_its_own_season(world):
    world.make("pack/Tatort.S01E01.mkv", 1000)
    world.make("pack/Tatort.S02E01.mkv", 1000)
    world.run({"type": "series", "title": "Tatort", "season": "1-2"}, "pack")
    assert world.tree("Serien") == [
        "Tatort/Season 01/Tatort - S01E01.mkv",
        "Tatort/Season 02/Tatort - S02E01.mkv",
    ]


def test_a_multi_season_pack_keeps_files_from_other_seasons(world):
    """Packs often carry extras: a parsed season outside the chosen list is filed, not dropped."""
    world.make("pack/Tatort.S01E01.mkv", 1000)
    world.make("pack/Tatort.S04E01.mkv", 1000)
    world.run({"type": "series", "title": "Tatort", "seasons": [1, 2]}, "pack")
    assert world.tree("Serien") == [
        "Tatort/Season 01/Tatort - S01E01.mkv",
        "Tatort/Season 04/Tatort - S04E01.mkv",
    ]


def test_a_multi_season_pack_reads_the_season_from_a_folder(world):
    world.make("pack/Season 1/E01.mkv", 1000)
    world.make("pack/Season 2/E02.mkv", 1000)
    world.run({"type": "series", "title": "Tatort", "seasons": [1, 2]}, "pack")
    assert world.tree("Serien") == [
        "Tatort/Season 01/Tatort - S01E01.mkv",
        "Tatort/Season 02/Tatort - S02E02.mkv",
    ]


@pytest.mark.parametrize("name", ["pack/Bonus featurette.mkv", "pack/E01.mkv"])
def test_a_multi_season_pack_never_guesses_a_missing_season(world, name):
    world.make("pack/Tatort.S01E01.mkv", 1000)
    world.make(name, 1000)
    with pytest.raises(OrganizeError, match="could not find episode numbers"):
        world.run({"type": "series", "title": "Tatort", "season": "1-2"}, "pack")
    assert world.tree("Serien") == [], "nothing is filed from a pack whose season is unclear"


def test_files_without_an_episode_number_are_skipped_and_none_at_all_is_an_error(world):
    world.make("pack/Tatort.S03E01.mkv", 1000)
    world.make("pack/Bonus featurette.mkv", 1000)
    world.run(PACK, "pack")
    assert world.tree("Serien") == ["Tatort/Season 03/Tatort - S03E01.mkv"]
    other = World(world.data.parent / "second")
    other.make("pack/Bonus featurette.mkv", 1000)
    with pytest.raises(OrganizeError, match="could not find episode numbers"):
        other.run(PACK, "pack")


def test_two_files_for_one_episode_keep_the_larger(world):
    world.make("pack/Tatort.S03E01.720p.mkv", 1000)
    world.make("pack/Tatort.S03E01.1080p.REPACK.mkv", 5000)
    world.run(PACK, "pack")
    (only,) = world.tree("Serien")
    assert only == "Tatort/Season 03/Tatort - S03E01.mkv"
    linked = world.data / "Serien" / only
    assert linked.stat().st_size == 5000


def test_a_second_run_of_a_pack_is_a_no_op(world):
    for n in (1, 2):
        world.make(f"pack/Tatort.S03E0{n}.mkv", 1000)
    world.run(PACK, "pack")
    world.run(PACK, "pack")
    assert len(world.tree("Serien")) == 2


# --- audiobooks and music ----------------------


def test_an_audiobook_keeps_its_files_under_author_and_title(world):
    world.make("Momo [MP3]/01 - Kapitel 1.mp3")
    world.make("Momo [MP3]/02 - Kapitel 2.mp3")
    world.make("Momo [MP3]/cover.jpg")
    world.make("Momo [MP3]/info.nfo")
    world.make("Momo [MP3]/Random Image.jpg")
    world.run(BOOK, "Momo [MP3]")
    assert world.tree("Hörbücher") == [
        "Michael Ende/Momo/01 - Kapitel 1.mp3",
        "Michael Ende/Momo/02 - Kapitel 2.mp3",
        "Michael Ende/Momo/cover.jpg",
    ]


def test_an_album_keeps_disc_folders(world):
    world.make("Autobahn (1974)/CD1/01 Autobahn.flac")
    world.make("Autobahn (1974)/CD2/01 Bonus.flac")
    world.make("Autobahn (1974)/folder.jpg")
    world.make("Autobahn (1974)/Scans/back.jpg")
    world.run(ALBUM, "Autobahn (1974)")
    assert world.tree("Musik") == [
        "Kraftwerk/Autobahn/CD1/01 Autobahn.flac",
        "Kraftwerk/Autobahn/CD2/01 Bonus.flac",
        "Kraftwerk/Autobahn/folder.jpg",
    ]


def test_a_single_audio_file_torrent(world):
    world.make("track.mp3")
    world.run(ALBUM, "track.mp3")
    assert world.tree("Musik") == ["Kraftwerk/Autobahn/track.mp3"]


def test_no_audio_is_an_error(world):
    world.make("rel/cover.jpg")
    with pytest.raises(OrganizeError, match="no audio file"):
        world.run(ALBUM, "rel")


def test_audio_with_umlauts_in_the_names(world):
    world.make("Größe Hörbücher/Kapitel Ä.mp3")
    world.run(BOOK, "Größe Hörbücher")
    assert world.tree("Hörbücher") == ["Michael Ende/Momo/Kapitel Ä.mp3"]


# --- safety ----------------------


@pytest.mark.security
def test_a_symlink_inside_the_torrent_is_never_followed(world, tmp_path):
    secret = tmp_path / "secret.mkv"
    secret.write_bytes(b"not for the library")
    world.make("rel/Movie.mkv", 100)
    (world.downloads / "rel/Link.mkv").symlink_to(secret)
    (world.downloads / "rel/Dir").symlink_to(tmp_path)
    world.run(MOVIE, "rel")
    assert world.tree("Filme") == ["Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"]
    assert secret.stat().st_nlink == 1


@pytest.mark.security
def test_a_content_path_that_is_a_symlink_is_refused(world, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Movie.mkv").write_bytes(b"x" * 100)
    (world.downloads / "rel").symlink_to(outside)
    with pytest.raises(OrganizeError, match=r"not inside|symbolic link"):
        world.run(MOVIE, "rel")
    assert world.tree("Filme") == []


@pytest.mark.security
@pytest.mark.parametrize("content_path", ["/etc", "/data/Filme", "relative/path", "", "/"])
def test_files_outside_downloads_are_refused(world, content_path):
    job = org.Job(1, "magnet", "a" * 40, MOVIE, "organizing", None, "", "")
    torrent = Torrent.from_api(
        {
            "hash": "a" * 40,
            "name": "x",
            "state": "uploading",
            "progress": 1,
            "content_path": content_path,
        }
    )
    with pytest.raises(OrganizeError):
        world.organizer(job, torrent)


@pytest.mark.security
def test_a_folder_that_cannot_be_read_fails_the_job(world, monkeypatch):
    """os.walk must not silently drop a folder and plan a job that only looks fine (#69)."""
    world.make("rel/Movie.mkv", 100)

    def unreadable(root, *, followlinks=False, onerror=None):
        assert onerror is not None, "os.walk was called without an onerror"
        onerror(OSError(errno.EACCES, "Permission denied", str(root)))
        return iter(())

    monkeypatch.setattr(org.os, "walk", unreadable)
    with pytest.raises(OrganizeError, match="cannot read the folder"):
        world.run(MOVIE, "rel")


@pytest.mark.security
def test_file_names_from_the_torrent_that_cannot_be_names_are_skipped(world):
    world.make("album/ok.mp3")
    world.make("album/bad\x01name.mp3")
    world.run(ALBUM, "album")
    assert world.tree("Musik") == ["Kraftwerk/Autobahn/ok.mp3"]


def test_a_name_over_the_byte_limit_of_the_file_system_is_not_usable():
    assert org._component("ü" * 120) is not None  # 240 bytes
    assert org._component("ü" * 130) is None  # 260 bytes: too long for ext4/ZFS
    assert org._component("a" * 200) == "a" * 200
    assert org._component("..") is None
    assert org._component("a/b") is None


@pytest.mark.security
def test_titles_cannot_climb_out_of_the_library(world):
    """The metadata is validated again at organize time (the DB could hold anything)."""
    world.make("rel/Movie.mkv", 100)
    for title in ("../../etc", "a/b", "..", "x\nz"):
        with pytest.raises(OrganizeError, match="metadata is not valid"):
            world.run({**MOVIE, "title": title}, "rel")
    assert world.tree("Filme") == []


def test_unknown_metadata_is_an_organize_error(world):
    world.make("rel/Movie.mkv", 100)
    with pytest.raises(OrganizeError, match="metadata"):
        world.run({"type": "podcast"}, "rel")


def test_missing_files_and_a_missing_library_folder_are_readable_errors(world):
    with pytest.raises(OrganizeError, match="missing"):
        world.run(MOVIE, "never-downloaded")
    world.make("rel/Movie.mkv", 100)
    (world.data / "Filme").rmdir()
    with pytest.raises(OrganizeError, match="library folder Filme does not exist"):
        world.run(MOVIE, "rel")


def test_the_library_folder_name_comes_from_settings_not_from_the_type(tmp_path):
    env = {**ENV, "MEDIA_DIR_MOVIES": "Movies"}
    data = tmp_path / "data"
    (data / "downloads/movies/rel").mkdir(parents=True)
    (data / "Movies").mkdir()
    (data / "downloads/movies/rel/Movie.mkv").write_bytes(b"x")
    organizer = org.FileOrganizer(Settings.from_env({**env, "DATA_ROOT": str(data)}), Refresher())
    job = org.Job(1, "magnet", "a" * 40, MOVIE, "organizing", None, "", "")
    torrent = Torrent.from_api(
        {"hash": "a" * 40, "name": "rel", "state": "uploading", "progress": 1,
         "content_path": str(data / "downloads/movies/rel")}
    )  # fmt: skip
    organizer(job, torrent)
    assert (data / "Movies/Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv").exists()


# --- Jellyfin ----------------------


def test_success_asks_for_a_debounced_refresh_of_that_library_folder(world):
    world.make("rel/Movie.mkv", 100)
    world.run(MOVIE, "rel")
    assert world.refresher.requested == ["Filme"]
    assert world.refresher.immediate == [False], "a normal job keeps the debounce"


def test_a_high_priority_job_asks_for_an_immediate_refresh(world):
    world.make("rel/Movie.mkv", 100)
    world.run(MOVIE, "rel", priority=1)
    assert world.refresher.requested == ["Filme"]
    assert world.refresher.immediate == [True], "a high-priority job skips the debounce once (#51)"


def test_the_umlaut_library_is_requested_in_nfc(world):
    world.make("rel/track.mp3")
    world.run(BOOK, "rel")
    assert world.refresher.requested == [unicodedata.normalize("NFC", "Hörbücher")]


def test_no_refresh_is_requested_when_organizing_fails(world):
    world.make("rel/readme.txt")
    with pytest.raises(OrganizeError):
        world.run(MOVIE, "rel")
    assert world.refresher.requested == []


# --- together with the reconciler ----------------------


def test_the_app_wires_the_organizer_into_the_reconciler(tmp_path):
    from starlette.testclient import TestClient

    from ytp.main import create_app

    config = tmp_path / "config"
    config.mkdir()
    env = {
        **ENV,
        "DATA_ROOT": str(tmp_path),
        "CONFIG_DIR": str(config),
        "QBIT_PASSWORD": "a password 12",
        "QBIT_URL": "http://127.0.0.1:9",
    }
    with TestClient(create_app(Settings.from_env(env))) as client:
        assert isinstance(client.app.state.organizer, org.FileOrganizer)
        assert client.app.state.reconciler.organize is client.app.state.organizer


def test_a_finished_download_goes_all_the_way_from_qbittorrent_to_the_library(tmp_path):
    """POST /api/magnet, qBittorrent "finishes", the reconciler runs, hardlink, job done."""
    from starlette.testclient import TestClient

    from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
    from ytp import jobs
    from ytp.main import create_app

    infohash = "0123456789abcdef0123456789abcdef01234567"
    data = tmp_path
    for name in ENV.values():
        (data / name).mkdir()
    release = data / "downloads/movies/Big.Buck.Bunny.2008"
    release.mkdir(parents=True)
    (release / "Big.Buck.Bunny.2008.mkv").write_bytes(b"x" * 500)
    (release / "Big.Buck.Bunny.2008.nfo").write_text("junk")
    config = tmp_path / "config"
    config.mkdir()
    fake = FakeQbittorrent().start()
    try:
        env = {
            **ENV,
            "DATA_ROOT": str(data),
            "CONFIG_DIR": str(config),
            "QBIT_URL": fake.url,
            "QBIT_USER": USER,
            "QBIT_PASSWORD": PASSWORD,
        }
        with TestClient(create_app(Settings.from_env(env))) as client:
            body = {
                "magnet": f"magnet:?xt=urn:btih:{infohash}",
                "metadata": {"type": "movies", "title": "Big Buck Bunny", "year": 2008},
            }
            assert client.post("/api/magnet", json=body).status_code == 201
            fake.torrents = [
                {
                    "hash": infohash,
                    "name": "Big.Buck.Bunny.2008",
                    "state": "uploading",
                    "progress": 1.0,
                    "category": "movies",
                    "content_path": str(release),
                }
            ]
            client.app.state.reconciler.run_once()
            job = jobs.get_job(client.app.state.db, 1)
            assert (job.status, job.error) == ("done", None)
            target = data / "Filme/Big Buck Bunny (2008)/Big Buck Bunny (2008).mkv"
            assert target.exists()
            assert os.path.samestat(target.stat(), (release / "Big.Buck.Bunny.2008.mkv").stat())
            assert client.app.state.jellyfin.pending is False, "no Jellyfin configured: a no-op"
    finally:
        fake.stop()
