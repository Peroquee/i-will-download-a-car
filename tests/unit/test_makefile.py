"""Makefile: the local venv is rebuilt whenever any requirements file it installs changes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def included_requirements(start: str) -> set[str]:
    """`start` and every file it pulls in with `-r`, as paths relative to the repository root."""
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        folder = (ROOT / name).parent
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*-r\s+(\S+)", line)
            if match:
                todo.append(str((folder / match.group(1)).resolve().relative_to(ROOT)))
    return seen


def venv_prerequisites() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^\$\(VENV\)/\.installed:(.*)$", text, re.MULTILINE)
    assert match, "no $(VENV)/.installed rule"
    return set(match.group(1).split())


def test_the_venv_depends_on_every_requirements_file_it_installs():
    needed = included_requirements("requirements-dev.txt")
    missing = needed - venv_prerequisites()
    assert not missing, f"changing {sorted(missing)} would not rebuild a stale venv"


def test_the_nested_app_requirements_are_among_them():
    """The case that bit: yt-dlp was added to app/requirements.txt (included via -r)."""
    assert "app/requirements.txt" in included_requirements("requirements-dev.txt")
    assert "app/requirements.txt" in venv_prerequisites()
