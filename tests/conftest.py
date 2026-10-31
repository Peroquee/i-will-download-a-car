"""Shared fixtures and the test-level rules for the whole suite.

Every test lives in tests/<level>/ and gets that level as a marker automatically,
so `pytest -m unit` and friends select by folder without anyone remembering to
decorate tests. Capability markers (docker, tun, network) skip cleanly with a
reason when the capability is missing, unless REQUIRE_CAPABILITIES=1 is set
(CI does that on the ytp-host runner), in which case a missing capability fails
the run instead of silently skipping it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers import docker as docker_helpers
from tests.helpers.compose_cli import compose_cli_available

REPO_ROOT = Path(__file__).resolve().parent.parent
LEVELS = ("unit", "integration", "smoke", "system", "regression")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.fspath)).relative_to(REPO_ROOT / "tests").parts
        level = parts[0] if parts else ""
        if level not in LEVELS:
            raise pytest.UsageError(
                f"{item.nodeid}: tests must live in one of tests/{{{','.join(LEVELS)}}}/"
            )
        item.add_marker(getattr(pytest.mark, level))


def _require_or_skip(available: bool, reason: str) -> None:
    if available:
        return
    if os.environ.get("REQUIRE_CAPABILITIES") == "1":
        pytest.fail(f"required capability missing: {reason}")
    pytest.skip(reason)


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("compose"):
        _require_or_skip(compose_cli_available(), "docker compose CLI not installed")
    if item.get_closest_marker("docker"):
        _require_or_skip(docker_helpers.daemon_available(), "Docker daemon not reachable")
    if item.get_closest_marker("tun"):
        _require_or_skip(docker_helpers.daemon_available(), "Docker daemon not reachable")
        # The device must exist on the Docker *host*, which is not necessarily
        # where pytest runs (docker executor talks to the host daemon via socket).
        _require_or_skip(docker_helpers.daemon_has_tun(), "Docker host has no /dev/net/tun")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
