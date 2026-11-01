#!/usr/bin/env python3
"""install.py: the installer's entry point, started by install.sh (issue #37).

Not meant to be run by hand: `./install.sh` checks the system, becomes root and then runs this.
The conversation and the steps are in the package `ytp_install` next to this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ytp_install.cli import run_cli

if __name__ == "__main__":
    sys.exit(run_cli())
