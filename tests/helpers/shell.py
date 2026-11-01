"""Run the repo's shell scripts under controlled conditions.

Unit tests never touch the real system: commands like docker, mountpoint or
systemd-detect-virt are replaced by stub executables in a temp dir that is put
first on PATH. Each stub prints fixed output and exits with a fixed code.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StubBin:
    """A directory of fake commands, prepended to PATH."""

    path: Path

    def add(self, name: str, script: str) -> Path:
        """Create a stub whose body is a bash snippet; "$@" holds its arguments.

        To make a command *missing* (so `command -v` fails), don't stub it and
        run with isolate_path=isolated_bin(tmp_path) instead.
        """
        target = self.path / name
        target.write_text(f"#!/usr/bin/env bash\n{script}\n", encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return target


@dataclass
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str

    def lines_starting(self, prefix: str) -> list[str]:
        return [line for line in self.stdout.splitlines() if line.startswith(prefix)]


# Minimal set of real tools the scripts need when PATH is isolated.
_BASE_TOOLS = (
    "bash",
    "env",
    "sed",
    "grep",
    "stat",
    "tail",
    "printf",
    "cat",
    "id",
    "dirname",
    "dpkg",
    "getent",
    "sh",
    "touch",
    "mkdir",
    "ln",
    "rm",
)


def isolated_bin(tmp_path: Path) -> Path:
    """A PATH dir containing only symlinks to basic tools (no docker, no systemd tools)."""
    base = tmp_path / "basebin"
    base.mkdir(exist_ok=True)
    for tool in _BASE_TOOLS:
        real = _which(tool)
        if real and not (base / tool).exists():
            (base / tool).symlink_to(real)
    return base


def _which(tool: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_script(
    script: Path,
    args: list[str] | None = None,
    *,
    stubs: StubBin | None = None,
    env: dict[str, str] | None = None,
    isolate_path: Path | None = None,
    cwd: Path | None = None,
    timeout: int = 30,
) -> ScriptResult:
    """Run a script with stubs first on PATH.

    isolate_path: if given, PATH is ONLY stubs + this dir, so real docker etc. are invisible.
    """
    full_env = {k: v for k, v in os.environ.items() if k not in {"CHECK_ROOT", "DRY_RUN"}}
    path_parts = []
    if stubs is not None:
        path_parts.append(str(stubs.path))
    if isolate_path is not None:
        path_parts.append(str(isolate_path))
    else:
        path_parts.append(full_env.get("PATH", ""))
    full_env["PATH"] = os.pathsep.join(path_parts)
    full_env.update(env or {})
    proc = subprocess.run(
        ["bash", str(script), *(args or [])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
    return ScriptResult(proc.returncode, proc.stdout, proc.stderr)
