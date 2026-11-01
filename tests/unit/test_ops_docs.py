"""docs/operations.md names real commands, targets and folders (issue #30)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
PHONY = " ".join(re.findall(r"^\.PHONY:(.*?)(?:\n\n)", MAKEFILE, re.MULTILINE | re.DOTALL)).replace(
    "\\\n", " "
)


def test_every_script_the_document_names_exists_and_is_executable():
    names = set(re.findall(r"scripts/(?:ops|host)/[\w.-]+\.(?:py|sh)", DOC))
    assert {"scripts/ops/backup.py", "scripts/ops/update_ytdlp.py"} <= names
    for name in names:
        path = ROOT / name
        assert path.is_file(), name
        assert path.stat().st_mode & 0o111, f"{name} is not executable"


def test_every_make_target_the_document_names_exists():
    targets = set(re.findall(r"(?:^\s*|`)make ([a-z][a-z-]*)", DOC, re.MULTILINE))
    assert {"backup", "backup-list", "update-check", "test-unit"} <= targets
    for target in targets:
        assert re.search(rf"(?<![\w-]){target}(?![\w-])", PHONY), target


def test_backups_are_ignored_by_git():
    assert "backups/" in (ROOT / ".gitignore").read_text().splitlines()


def test_the_default_backup_folder_is_inside_an_ignored_path():
    script = (ROOT / "scripts" / "ops" / "backup.py").read_text()
    assert 'args.repo_root / "backups"' in script
