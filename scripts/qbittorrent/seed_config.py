#!/usr/bin/env python3
"""seed_config.py: give qBittorrent a real Web UI login before its first start (issue #3).

The linuxserver image otherwise starts with a temporary random password that is
printed to the log. This script writes the Web UI user and a PBKDF2 hash of a
password you type (never the password itself) into qBittorrent.conf, and binds
qBittorrent to the VPN interface tun0 as an extra guard on top of the shared
network namespace: if the tunnel is down there is no tun0 and nothing connects.

Usage:
    scripts/qbittorrent/seed_config.py [--user NAME] [--config PATH] [--port N] [--dry-run]
                                       [--password-stdin]
    scripts/qbittorrent/seed_config.py --fix-host-header [--config PATH] [--dry-run]

The password is asked for twice on the terminal, or read from stdin with
--password-stdin (for automation; never put it on the command line). Run it on
the host, with the qbittorrent container STOPPED: qBittorrent rewrites its config
on exit and would overwrite the change. Safe to re-run; it replaces only the keys
it owns and keeps everything else in the file.

**Host header check off, CSRF protection on.** qBittorrent answers 401 to any request whose `Host`
header names another port than its own (8080 inside gluetun). Through the proxy on 8081 the
browser's `Host` is `<lan address>:8081`, so the Web UI could never be reached that way. The key
`WebUI\\HostHeaderValidation` is therefore set to false, and `WebUI\\CSRFProtection` is set to
true explicitly. Only Caddy (LAN check, then login) and localhost on the VM can reach the port;
see docs/proxy.md. `--fix-host-header` applies just these two keys to an EXISTING config (no
password asked, nothing else touched); stop the qbittorrent container first, it rewrites the file
when it exits.

Test hook: none needed, --config points it at any file.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import os
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"

# qBittorrent's own parameters (Utils::Password::PBKDF2): HMAC-SHA512, 100000 rounds,
# 16-byte salt, 64-byte key, stored as base64(salt):base64(key) inside @ByteArray().
PBKDF2_ITERATIONS = 100_000
SALT_BYTES = 16
KEY_BYTES = 64

MIN_PASSWORD_LENGTH = 10
USER_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,64}")

# The proxy on :8081 sends Host "<lan address>:8081" to a Web UI on :8080, which qBittorrent
# refuses (401) while host header validation is on. CSRF protection (Origin against Host) stays on.
PROXY_SETTINGS = {
    "WebUI\\HostHeaderValidation": "false",
    "WebUI\\CSRFProtection": "true",
}

VPN_INTERFACE = "tun0"
WEBUI_PORT = "8080"  # the container-side port gluetun publishes (docker-compose.yml)


def pbkdf2_value(password: str, salt: bytes | None = None) -> str:
    """The value qBittorrent stores for WebUI\\Password_PBKDF2 (without the quotes)."""
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, PBKDF2_ITERATIONS, KEY_BYTES)
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"


def wanted_settings(user: str, password: str, port: str = WEBUI_PORT) -> dict[str, dict[str, str]]:
    """section -> {key: value} that this script owns, in qBittorrent's INI syntax."""
    return {
        # Without this the headless qbittorrent-nox waits for an answer on stdin.
        "LegalNotice": {"Accepted": "true"},
        "Preferences": {
            "WebUI\\Username": user,
            "WebUI\\Password_PBKDF2": f'"{pbkdf2_value(password)}"',
            "WebUI\\Port": port,
            # Ask for the login from every client, localhost and LAN included.
            "WebUI\\LocalHostAuth": "true",
            "WebUI\\AuthSubnetWhitelistEnabled": "false",
            **PROXY_SETTINGS,
        },
        "BitTorrent": {
            "Session\\Interface": VPN_INTERFACE,
            "Session\\InterfaceName": VPN_INTERFACE,
        },
    }


def apply_settings(text: str, settings: dict[str, dict[str, str]]) -> str:
    """Set keys inside their sections of an INI text, keeping all other lines as they are."""
    lines = text.splitlines()
    for section, keys in settings.items():
        header = f"[{section}]"
        if header not in lines:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(header)
        start = lines.index(header) + 1
        end = start
        while end < len(lines) and not lines[end].startswith("["):
            end += 1
        # Drop existing lines for our keys, then add them back at the end of the section.
        body = [ln for ln in lines[start:end] if ln.split("=", 1)[0] not in keys]
        while body and not body[-1].strip():
            body.pop()
        body += [f"{key}={value}" for key, value in keys.items()]
        if end < len(lines):
            body.append("")
        lines[start:end] = body
    return "\n".join(lines) + "\n"


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New qBittorrent Web UI password: ")
        if getpass.getpass("Repeat it: ") != password:
            raise SystemExit("error: the two entries differ")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"error: the password needs at least {MIN_PASSWORD_LENGTH} characters")
    return password


def write_private(path: Path, text: str) -> None:
    """Write via a temp file in the same folder so a crash never leaves half a config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)


def host_header_fixed(text: str) -> bool:
    """Does this config already have the proxy settings (as written by this script)?"""
    lines = {ln.strip() for ln in text.splitlines()}
    return all(f"{key}={value}" in lines for key, value in PROXY_SETTINGS.items())


def fix_host_header(config: Path, dry_run: bool) -> int:
    """Set the proxy keys in an existing config and nothing else. 0 also if it was fine already."""
    if not config.exists():
        print(f"error: {config} does not exist (run the script without --fix-host-header)")
        return 1
    existing = config.read_text(encoding="utf-8")
    if host_header_fixed(existing):
        print(f"{config} already has the proxy settings, nothing to do")
        return 0
    if dry_run:
        print(f"dry run: would set {', '.join(PROXY_SETTINGS)} in {config}")
        return 0
    write_private(config, apply_settings(existing, {"Preferences": PROXY_SETTINGS}))
    print(f"set {', '.join(PROXY_SETTINGS)} in {config}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed qBittorrent's Web UI login and VPN binding.")
    parser.add_argument("--user", default="admin", help="Web UI user name (default: admin)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="qBittorrent.conf path")
    parser.add_argument(
        "--port",
        type=int,
        default=int(WEBUI_PORT),
        choices=range(1, 65536),
        metavar="PORT",
        help=f"Web UI port (default {WEBUI_PORT}, as docker-compose.yml; only tests change it)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change, write nothing"
    )
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin"
    )
    parser.add_argument(
        "--fix-host-header",
        action="store_true",
        help="only set the two proxy-related keys in an existing config (no password asked)",
    )
    args = parser.parse_args(argv)

    if args.fix_host_header:
        return fix_host_header(args.config, args.dry_run)

    if not USER_PATTERN.fullmatch(args.user):
        raise SystemExit("error: --user must be 3 to 64 characters of A-Z a-z 0-9 . _ -")
    password = read_password(args.password_stdin)

    existing = args.config.read_text(encoding="utf-8") if args.config.exists() else ""
    updated = apply_settings(existing, wanted_settings(args.user, password, str(args.port)))

    if args.dry_run:
        print(f"dry run: would write {args.config}")
        for section, keys in wanted_settings(args.user, "x" * MIN_PASSWORD_LENGTH).items():
            for key in keys:
                shown = "<hash>" if "Password" in key else keys[key]
                print(f"  [{section}] {key}={shown}")
        return 0

    write_private(args.config, updated)
    print(f"wrote {args.config} (user {args.user}, bound to {VPN_INTERFACE}, mode 600)")
    print("Start the stack: docker compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
