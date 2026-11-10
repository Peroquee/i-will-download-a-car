#!/usr/bin/env python3
"""setup_key.py: give the VPN watchdog its own read-only key for gluetun's control server (#21).

Gluetun keeps every route of its control server private. The watchdog only needs
`GET /v1/vpn/status`, `GET /v1/publicip/ip` (the egress check and the tunnel check, #81) and, so
that it can restart a VPN whose tunnel stays dead for a long time (#81, WATCHDOG_RESTART_AFTER),
`PUT /v1/vpn/status`. This script creates a role for exactly those three routes and nothing else
(no other setting of the server can be changed; the PUT can only stop or start the VPN, and a
stopped VPN blocks all traffic, so it can never open a leak):

* writes `config/gluetun/auth/config.toml` (mode 600), which gluetun reads at start as
  `/gluetun/auth/config.toml` (the folder is already mounted at /gluetun);
* puts the same key into .env as GLUETUN_CONTROL_APIKEY, which the watchdog container reads.

Usage:
    scripts/watchdog/setup_key.py [--env-file PATH] [--auth-file PATH] [--rotate] [--dry-run]

Safe to re-run: an existing key in .env is kept (the TOML file is rewritten from it, so they
cannot drift apart, and a file written by an older version gains new routes); --rotate
makes a new one. The key is never printed. Gluetun reads the file only at start: run
`docker compose up -d --force-recreate gluetun qbittorrent watchdog` afterwards (qBittorrent lives
in gluetun's network namespace and must be recreated with it, or it stays down).
"""

from __future__ import annotations

import argparse
import contextlib
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
# One role for the VPN watchdog: read the VPN status and the public address, and stop/start the VPN
# (only used to restart a VPN whose tunnel has stayed dead for a long time).
[[roles]]
name = "watchdog"
routes = ["GET /v1/vpn/status", "GET /v1/publicip/ip", "PUT /v1/vpn/status"]
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


def _nearest_existing(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _copy_owner(path: Path, reference: os.stat_result) -> None:
    """Give `path` the owner and group of `reference`. Only root can give a file to somebody else;
    for everyone else (who writes files they own anyway) a refusal is fine."""
    with contextlib.suppress(OSError):
        os.chown(path, reference.st_uid, reference.st_gid)


def write_private(path: Path, text: str) -> None:
    """Write via a temp file in the same folder; keep an existing file's mode, owner and group.

    Run with sudo, a rewrite must not turn `.env` into root's file: the VM user could not read it
    any more and `docker compose` without sudo would fail (#81). A NEW file gets mode 600 and the
    owner and group of the folder it is created in (so do the folders this creates)."""
    if path.exists():
        reference = path.stat()
        mode = reference.st_mode & 0o777
    else:
        reference = _nearest_existing(path.parent).stat()
        mode = 0o600
    missing = []
    folder = path.parent
    while not folder.exists() and folder.parent != folder:
        missing.append(folder)
        folder = folder.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for made in missing:
        _copy_owner(made, reference)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.chmod(mode)
    _copy_owner(tmp, reference)  # before the rename: the file is never visible with another owner
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
    print("Apply it: docker compose up -d --force-recreate gluetun qbittorrent watchdog")
    return 0


if __name__ == "__main__":
    sys.exit(main())
