"""Regression guards for issue #7 (C2): Jellyfin is external.

Same rule as before: a failing test here means a deliberate decision is being changed,
which needs its own issue and a conscious update, not a quick fix to the test.
"""

from __future__ import annotations

import re

import pytest

from tests.helpers.compose import load_compose, parse_env_file, services
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT


def test_c2_jellyfin_is_external():
    """Decision: Jellyfin runs in its own privileged LXC (192.168.178.108) and keeps running
    there. The compose stack never starts, pins or mounts a Jellyfin of its own."""
    compose = load_compose(REPO_ROOT / "docker-compose.yml")
    assert "jellyfin" not in services(compose)
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "jellyfin/jellyfin" not in text
    assert "JELLYFIN_TAG" not in ENV_EXAMPLE.read_text(encoding="utf-8")


@pytest.mark.security
def test_c2_the_jellyfin_api_key_is_a_secret_that_never_lands_in_git():
    """Decision: the key is created in Jellyfin's dashboard and lives only in .env on the VM."""
    assert parse_env_file(ENV_EXAMPLE)["JELLYFIN_API_KEY"] == ""
    for path in (REPO_ROOT / "docs").rglob("*.md"):
        # Jellyfin API keys are 32 hex characters; none may appear in the docs.
        assert not re.search(r"\b[0-9a-f]{32}\b", path.read_text(encoding="utf-8")), path
