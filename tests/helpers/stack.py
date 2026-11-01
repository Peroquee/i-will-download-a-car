"""Start the real compose stack under a throwaway project name, for system tests.

The stack gets its own working folder: its .env (with DATA_ROOT redirected), the
rendered VPN config and the qbittorrent watcher script, so the running production
stack, the media volume and the repo's config/ folder are never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tests.helpers import docker
from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import COMPOSE_FILE, REPO_ROOT


class Stack:
    def __init__(self, project: str, workdir: Path):
        self.project = project
        self.workdir = workdir

    def compose_prefix(self) -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            self.project,
            "--project-directory",
            str(self.workdir),
            "-f",
            str(COMPOSE_FILE),
            "--env-file",
            str(self.workdir / ".env"),
        ]

    def compose(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.compose_prefix(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def container(self, service: str) -> str:
        return self.compose("ps", "-q", service).stdout.strip()

    def exec(self, service: str, *args: str) -> str:
        return docker.run(["exec", self.container(service), *args], timeout=60).stdout.strip()


def prepare_workdir(env_file: Path, workdir: Path) -> None:
    """Everything the compose file mounts from the project folder, in a temp folder."""
    values = parse_env_file(env_file)
    values["DATA_ROOT"] = str(workdir / "data")  # keep the real media volume out of it
    (workdir / "data").mkdir()
    (workdir / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8"
    )
    (workdir / ".env").chmod(0o600)

    shutil.copytree(
        REPO_ROOT / "scripts" / "qbittorrent" / "services",
        workdir / "scripts" / "qbittorrent" / "services",
    )

    if values.get("VPN_SERVICE_PROVIDER") == "custom":
        source = Path(os.environ.get("YTP_OVPN_SOURCE", REPO_ROOT / "config" / "expressvpn.ovpn"))
        rendered = workdir / "config" / "gluetun" / "custom" / "expressvpn.ovpn"
        subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPO_ROOT / "scripts" / "vpn" / "render_ovpn.py"),
                "--source",
                str(source),
                "--output",
                str(rendered),
            ],
            check=True,
            capture_output=True,
        )


@contextmanager
def running_stack(env_file: Path, workdir: Path) -> Iterator[Stack]:
    """gluetun and qbittorrent up and healthy; removed afterwards, pass or fail."""
    prepare_workdir(env_file, workdir)
    stack = Stack(docker.unique_name("stack"), workdir)
    try:
        started = stack.compose(
            "up", "-d", "--wait", "--wait-timeout", "240", "gluetun", "qbittorrent"
        )
        if started.returncode != 0:
            raise AssertionError(started.stdout + started.stderr)
        yield stack
    finally:
        stack.compose("down", "--volumes", "--remove-orphans")
