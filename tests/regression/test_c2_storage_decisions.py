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
    assert env["MEDIA_DIR_YOUTUBE"] == "Youtube"


def test_c2_downloads_live_in_their_own_tree_that_jellyfin_never_sees():
    """Decision: unfinished and finished torrents sit in /data/downloads/<type>, outside the
    library folders, so half-finished files never appear in Jellyfin."""
    text = LAYOUT_SCRIPT.read_text(encoding="utf-8")
    assert 'DOWNLOAD_DIRS="movies series audiobooks music incomplete"' in text
    assert "/media" not in text.replace("MEDIA", "")


@pytest.mark.security
def test_c2_layout_script_never_touches_existing_folders_or_files():
    """Decision: the layout script only creates what is missing. It must never chmod/chown
    existing folders or recurse into the media (irreplaceable, owned by other users)."""
    code = "\n".join(
        line
        for line in LAYOUT_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in ("chmod", "chown", "rm ", "mv ", "find ", " -R", " -r "):
        assert forbidden not in code, forbidden
    # The only change it makes is creating a folder, owned and moded as configured.
    assert len(re.findall(r"\binstall -d\b", code)) == 1
