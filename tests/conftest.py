"""Shared fixtures and the test-level rules for the whole suite.

Every test lives in tests/<level>/ and gets that level as a marker automatically,
so `pytest -m unit` and friends select by folder without anyone remembering to
decorate tests. Capability markers (docker, tun, network) skip cleanly with a
reason when the capability is missing, unless REQUIRE_CAPABILITIES=1 is set
(CI does that on the ytp-host runner), in which case a missing capability fails
the run instead of silently skipping it.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import docker as docker_helpers
from tests.helpers.compose_cli import compose_cli_available

REPO_ROOT = Path(__file__).resolve().parent.parent
LEVELS = ("unit", "ui", "integration", "smoke", "system", "regression")

# --- the test admin (#44, #76) ---------------------------------------------------------------
# Most tests are about something else, so by default every request counts as a test admin. The
# hook is `create_app(test_account=...)` and app.state; production has no global switch. Tests
# import `create_app` from ytp.main at module import time, so it is wrapped here ONCE, before
# the test modules are imported, and the wrapper reads the account of the running test.
try:
    from ytp import main as _ytp_main
    from ytp.accounts import Account as _Account
except ModuleNotFoundError:  # the integration job has no app dependencies: no app to build
    _ytp_main = None

_TEST_ADMIN = (
    None
    if _ytp_main is None
    else _Account(1, "test-admin", "admin", False, "2026-01-01T00:00:00.000Z", None)
)
_TEST_ACCOUNT: contextvars.ContextVar = contextvars.ContextVar("test_account", default=None)
_UNSET = object()

if _ytp_main is not None:
    _real_create_app = _ytp_main.create_app

    def _create_app_with_test_account(settings=None, *, test_account=_UNSET, **kwargs):
        if test_account is _UNSET:
            test_account = _TEST_ACCOUNT.get()
        return _real_create_app(settings, test_account=test_account, **kwargs)

    _ytp_main.create_app = _create_app_with_test_account


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


@pytest.fixture(autouse=True)
def _no_real_channel_listing(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """The channel poller asks yt-dlp (YouTube) for a channel's videos. A test must never do that
    by accident (an app made with `create_app` has a real lister): it fails like a network error
    unless the test passes its own `extract`. Tests of the real thing are gated system tests."""
    if request.node.get_closest_marker("real_ytdlp"):
        return

    def refuse(url, options, limit):
        raise OSError("tests do not list channels on YouTube")

    with contextlib.suppress(ModuleNotFoundError):  # a job without the app's packages: no app
        monkeypatch.setattr("ytp.channel_videos._list_with_ytdlp", refuse)


@pytest.fixture(autouse=True)
def _test_admin(request: pytest.FixtureRequest) -> Iterator[None]:
    """Issue #44: the app needs a login for nearly everything. Tests of other things should not
    each have to log in, so by default every request counts as a test admin, injected into the
    apps the test builds through `create_app(test_account=...)` (#76). A test of the real login
    is marked `real_auth` and gets no such account."""
    if _ytp_main is None:
        yield
        return
    account = None if request.node.get_closest_marker("real_auth") else _TEST_ADMIN
    token = _TEST_ACCOUNT.set(account)
    try:
        yield
    finally:
        _TEST_ACCOUNT.reset(token)
