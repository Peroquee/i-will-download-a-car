#!/usr/bin/env python3
"""setup_key.py: give the VPN watchdog its own read-only key for gluetun's control server (#21).

Gluetun keeps every route of its control server private. The watchdog only needs
`GET /v1/vpn/status` and, for the egress check, `GET /v1/publicip/ip`, so this script creates a role
that may do exactly those two reads and nothing else (not stop the VPN, not change the server):

* writes `config/gluetun/auth/config.toml` (mode 600), which gluetun reads at start as
  `/gluetun/auth/config.toml` (the folder is already mounted at /gluetun);
* puts the same key into .env as GLUETUN_CONTROL_APIKEY, which the watchdog container reads.

Usage:
    scripts/watchdog/setup_key.py [--env-file PATH] [--auth-file PATH] [--rotate] [--dry-run]

Safe to re-run: an existing key in .env is kept (the TOML file is rewritten from it, so they
cannot drift apart, and a file written by an older version gains new routes); --rotate
makes a new one. The key is never printed. Gluetun reads the file only at start: run `docker compose up -d --force-recreate gluetun watchdog` afterwards.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_AUTH_FILE = REPO_ROOT / "config" / "gluetun" / "auth" / "config.toml"
KEY_NAME = "GLUETUN_CONTROL_APIKEY"
PLACEHOLDER = "changeme"
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,}")

TOML = """# Written by scripts/watchdog/setup_key.py: do not edit, do not commit.
# One role for the VPN watchdog, allowed to read the VPN status and the public address only.
[[roles]]
name = "watchdog"
routes = ["GET /v1/vpn/status", "GET /v1/publicip/ip"]
auth = "apikey"
apikey = "{key}"
"""


def env_value(text: str, key: str) -> str | None:
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("'\"")
    return None


def apply_setting(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    indexes = [i for i, ln in enumerate(lines) if ln.partition("=")[0].strip() == key]
    if indexes:
        lines[indexes[0]] = f"{key}={value}"
        for i in reversed(indexes[1:]):
            del lines[i]
    else:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> None:
    """Write via a temp file in the same folder; keep an existing mode, else 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.chmod(mode)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the watchdog's gluetun control key.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    parser.add_argument("--rotate", action="store_true", help="make a new key even if one exists")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    args = parser.parse_args(argv)

    if not args.env_file.exists():
        raise SystemExit(f"error: {args.env_file} not found (copy .env.example to .env first)")
    existing = args.env_file.read_text(encoding="utf-8")
    current = env_value(existing, KEY_NAME)
    keep = bool(current) and current != PLACEHOLDER and bool(KEY_PATTERN.fullmatch(current or ""))
    action = "keep the key in .env" if keep and not args.rotate else "create a new key"

    if args.dry_run:
        print(f"dry run: would {action}, write {args.env_file} ({KEY_NAME}) and {args.auth_file}")
        return 0

    key = current if keep and not args.rotate else secrets.token_urlsafe(24)
    assert key is not None
    write_private(args.auth_file, TOML.format(key=key))
    write_private(args.env_file, apply_setting(existing, KEY_NAME, key))
    print(f"{action}; wrote {args.auth_file} (mode 600) and {KEY_NAME} in {args.env_file}")
    print("Apply it: docker compose up -d --force-recreate gluetun watchdog")
    return 0


if __name__ == "__main__":
    sys.exit(main())
