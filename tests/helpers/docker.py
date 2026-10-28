"""Thin wrappers around the docker CLI for integration, smoke and system tests."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import uuid
from collections.abc import Sequence

# Small, pinned images for throwaway containers. Override to use a registry
# mirror or pre-pulled images, e.g. TEST_IMAGE_BUSYBOX=registry.lan/busybox:1.37.
BUSYBOX = os.environ.get("TEST_IMAGE_BUSYBOX", "busybox:1.37")
PYTHON = os.environ.get("TEST_IMAGE_PYTHON", "python:3.12-alpine")


def docker_cli_available() -> bool:
    return shutil.which("docker") is not None


@functools.cache
def daemon_available() -> bool:
    if not docker_cli_available():
        return False
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


@functools.cache
def daemon_has_tun() -> bool:
    """True if the daemon's host can hand /dev/net/tun to a container."""
    if not daemon_available():
        return False
    result = run(
        ["run", "--rm", "--device", "/dev/net/tun", BUSYBOX, "test", "-c", "/dev/net/tun"],
        timeout=180,
    )
    return result.returncode == 0


def run(
    args: Sequence[str],
    *,
    timeout: int = 120,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check, env=env
    )


def unique_name(prefix: str) -> str:
    """Name for throwaway containers/volumes so parallel jobs never collide."""
    return f"ytp-test-{prefix}-{uuid.uuid4().hex[:8]}"
