"""Every shell script in the repo follows the same safety conventions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((REPO_ROOT / "scripts").rglob("*.sh"))


def test_there_are_scripts():
    assert SCRIPTS


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
class TestScript:
    def test_has_bash_shebang(self, script):
        assert script.read_text().startswith("#!/usr/bin/env bash\n")

    def test_is_executable(self, script):
        assert os.access(script, os.X_OK), f"chmod +x {script.relative_to(REPO_ROOT)}"

    def test_uses_strict_mode(self, script):
        text = script.read_text()
        assert "set -euo pipefail" in text or "set -uo pipefail" in text

    def test_has_a_header_explaining_it(self, script):
        second_line = script.read_text().splitlines()[1]
        assert second_line.startswith(f"# {script.name}:")

    def test_parses(self, script):
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
