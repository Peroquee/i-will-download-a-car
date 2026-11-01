"""Regression guards for issues #5 and #6 (C2): the storage layout and qBittorrent categories.

Same rule as for C0 and C1: a failing test here means a deliberate decision is being
changed, which needs its own issue and a conscious update, not a quick fix to the test.
"""

from __future__ import annotations

import re

import pytest

from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT

LAYOUT_SCRIPT = REPO_ROOT / "scripts" / "host" / "create-layout.sh"


def test_c2_library_folders_are_the_owners_existing_german_ones():
    """Decision: the media folders already exist directly under /data with these names and
    Jellyfin's libraries point at them. There is NO /data/media/ level, and nothing may
    rename or move them. The names are configurable, and these are the defaults."""
    env = parse_env_file(ENV_EXAMPLE)
    assert env["MEDIA_DIR_MOVIES"] == "Filme"
    assert env["MEDIA_DIR_SERIES"] == "Serien"
    assert env["MEDIA_DIR_AUDIOBOOKS"] == "Hörbücher"
    assert env["MEDIA_DIR_MUSIC"] == "Musik"
    assert env["MEDIA_DIR_PODCASTS"] == "Podcasts"
    assert env["MEDIA_DIR_YOUTUBE"] == "Youtube"


def test_c2_downloads_live_in_their_own_tree_that_jellyfin_never_sees():
    """Decision: unfinished and finished torrents sit in /data/downloads/<type>, outside the
    library folders, so half-finished files never appear in Jellyfin."""
    text = LAYOUT_SCRIPT.read_text(encoding="utf-8")
    assert 'DOWNLOAD_DIRS="movies series audiobooks music incomplete"' in text
    assert "/media" not in text.replace("MEDIA", "")


def script_code() -> str:
    return "\n".join(
        line
        for line in LAYOUT_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def split_app_config(code: str) -> tuple[str, str]:
    """(the rest of the script, the body of ensure_app_config)."""
    match = re.search(r"^ensure_app_config\(\) \{\n(.*?)^\}\n", code, re.S | re.M)
    assert match, "ensure_app_config() is gone"
    return code.replace(match.group(0), ""), match.group(1)


@pytest.mark.security
def test_c2_layout_script_never_touches_existing_folders_or_files():
    """Decision: for the MEDIA folders the script only creates what is missing. It must never
    chmod/chown existing folders or recurse into the media (irreplaceable, owned by others).

    The one deliberate exception is the app's own state folder, see the next test."""
    rest, _ = split_app_config(script_code())
    for forbidden in ("chmod", "chown", "rm ", "mv ", "find ", " -R", " -r "):
        assert forbidden not in rest, forbidden
    # The only change it makes to the data tree is creating a folder, owned and moded as configured.
    assert len(re.findall(r"\binstall -d\b", rest)) == 1


@pytest.mark.security
def test_c2_only_the_app_state_folder_may_be_handed_over_and_never_recursively():
    """Decision (#9): ./config/app, the app's database folder, is the one existing folder the
    script fixes (Docker creates it as root:root, the app runs as PUID:PGID and cannot write).
    Only that folder and the database files, non-recursively; never anything under DATA_ROOT."""
    _, body = split_app_config(script_code())
    for forbidden in ("rm ", "mv ", "find ", " -R", " -r ", "DATA_ROOT", "data_root", "$root"):
        assert forbidden not in body, forbidden
    targets = re.findall(r"run (?:chown -h|chmod) \S+ (\S+)", body)
    assert targets == ['"$dir"', '"$dir"', '"$dir/$file"']
    # Every chown is -h (never follows a link), and links are refused before any change.
    assert len(re.findall(r"\bchown\b", body)) == len(re.findall(r"\bchown -h\b", body)) == 2
    assert body.count('[ -L "$dir" ]') == 1
    assert body.count('[ -L "$dir/$file" ]') == 1
    assert "for file in app.db app.db-wal app.db-shm" in body
