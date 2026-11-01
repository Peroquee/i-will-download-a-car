"""Reading, merging and safely writing `.env`.

The rules, all for a file that holds the VPN password:

* **Never overwritten without a backup.** If the new text differs from what is there, the old file
  is first copied to `.env.bak-<date>-<time>` (mode 600, never deleted by this program).
* **Mode 600, written through a temp file** in the same folder, so a crash never leaves half a file.
* **Only the settings the installer owns are changed.** Every other line, comment and the owner's
  manual additions stay exactly as they were.
* **Values are quoted so Docker Compose reads them back exactly:** a value with anything but
  plain characters goes in single quotes (no `$` expansion inside them). A single quote itself
  cannot be written that way; `answers` refuses it before it gets here.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

PLAIN = re.compile(r"[A-Za-z0-9_./:@,+=%-]*")


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines of a dotenv text (the last one wins), without surrounding quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def format_value(value: str) -> str:
    if "'" in value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("a value with a quote or a line break cannot be written to .env")
    return value if PLAIN.fullmatch(value) else f"'{value}'"


def apply_settings(text: str, settings: dict[str, str]) -> str:
    """Set KEY=VALUE lines, keeping every other line. A key that appears twice keeps its first
    line; a new key is appended at the end."""
    lines = text.splitlines()
    for key, value in settings.items():
        new = f"{key}={format_value(value)}"
        indexes = [i for i, ln in enumerate(lines) if ln.partition("=")[0].strip() == key]
        if indexes:
            lines[indexes[0]] = new
            for i in reversed(indexes[1:]):
                del lines[i]
        else:
            lines.append(new)
    return "\n".join(lines) + "\n"


def missing_settings(example_text: str, current_text: str) -> dict[str, str]:
    """The settings `.env.example` has and the current `.env` lacks, with the example's value.

    After an update the template can have new required settings (compose stops without them);
    this is how they get their defaults without touching anything the owner set.
    """
    current = parse_env(current_text)
    return {k: v for k, v in parse_env(example_text).items() if k not in current}


def backup_path(path: Path, now: datetime) -> Path:
    return path.with_name(f"{path.name}.bak-{now:%Y%m%d-%H%M%S}")


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.chmod(mode)
    tmp.replace(path)


def write_env(path: Path, text: str, now: datetime) -> Path | None:
    """Write `.env`; returns the backup's path if an older, different file was backed up."""
    backup: Path | None = None
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return None
        backup = backup_path(path, now)
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    write_private(path, text)
    return backup
