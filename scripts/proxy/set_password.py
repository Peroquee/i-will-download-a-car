#!/usr/bin/env python3
"""set_password.py: set the reverse proxy's login in .env (issue #29).

Asks for a password (twice), has Caddy hash it with bcrypt (`caddy hash-password`, run in
the pinned Caddy image, so no extra tool is needed on the VM), and writes PROXY_USER and
PROXY_PASSWORD_HASH into .env. The password itself is never written, logged or put on a
command line: it goes to Caddy through stdin.

Usage:
    scripts/proxy/set_password.py [--user NAME] [--env-file PATH] [--dry-run]
                                  [--password-stdin]

The password is asked for on the terminal, or read from stdin with --password-stdin (for
automation). Safe to re-run: it replaces only the two lines it owns and keeps everything
else in the file, including the file's permissions (a new file gets mode 600). Then run
`docker compose up -d` to apply it.

The hash is written in single quotes: it contains "$" and Compose would otherwise treat
"$2a" and the like as variables.

Test hook: the `docker` command is looked up on PATH like any other.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

MIN_PASSWORD_LENGTH = 12
USER_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,64}")
# Caddy's `hash-password` prints a bcrypt hash: $2a$<cost>$<53 characters>.
BCRYPT_PATTERN = re.compile(r"\$2[abxy]\$\d\d\$[./A-Za-z0-9]{53}")


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password for the proxy login: ")
        if getpass.getpass("Repeat it: ") != password:
            raise SystemExit("error: the two entries differ")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"error: the password needs at least {MIN_PASSWORD_LENGTH} characters")
    return password


def env_value(text: str, key: str) -> str | None:
    """The value of KEY in a dotenv text, without surrounding quotes; None if absent."""
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("'\"")
    return None


def hash_password(password: str, image: str) -> str:
    """bcrypt hash of the password, made by `caddy hash-password` with the password on stdin."""
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "-i", image, "caddy", "hash-password"],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit("error: docker is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise SystemExit(f"error: hashing timed out (is {image} being pulled?)") from None
    if result.returncode != 0:
        # Docker's own message (pull failure and the like) never contains the password.
        raise SystemExit(f"error: `caddy hash-password` failed:\n{result.stderr.strip()}")
    hashed = result.stdout.strip()
    if not BCRYPT_PATTERN.fullmatch(hashed):
        raise SystemExit("error: caddy did not return a bcrypt hash")
    return hashed


def apply_settings(text: str, settings: dict[str, str]) -> str:
    """Set KEY=VALUE lines in a dotenv text, keeping every other line as it is."""
    lines = text.splitlines()
    for key, value in settings.items():
        new = f"{key}={value}"
        indexes = [i for i, ln in enumerate(lines) if ln.partition("=")[0].strip() == key]
        if indexes:
            lines[indexes[0]] = new
            for i in reversed(indexes[1:]):
                del lines[i]
        else:
            lines.append(new)
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> None:
    """Write via a temp file in the same folder; keep the old mode, or use 600 for a new file."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.chmod(mode)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set the reverse proxy's user and password hash.")
    parser.add_argument("--user", default=None, help="login name (default: PROXY_USER or admin)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help=".env path")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin"
    )
    args = parser.parse_args(argv)

    if not args.env_file.exists():
        raise SystemExit(f"error: {args.env_file} not found (copy .env.example to .env first)")
    existing = args.env_file.read_text(encoding="utf-8")

    user = args.user or env_value(existing, "PROXY_USER") or "admin"
    if not USER_PATTERN.fullmatch(user):
        raise SystemExit("error: the user name must be 3 to 64 characters of A-Z a-z 0-9 . _ -")
    tag = env_value(existing, "CADDY_TAG")
    if not tag:
        raise SystemExit(f"error: CADDY_TAG is not set in {args.env_file}")
    image = f"caddy:{tag}"

    if args.dry_run:
        print(
            f"dry run: would ask for a password, hash it with {image} and set in {args.env_file}:"
        )
        print(f"  PROXY_USER={user}")
        print("  PROXY_PASSWORD_HASH='<bcrypt hash>'")
        return 0

    password = read_password(args.password_stdin)
    hashed = hash_password(password, image)
    updated = apply_settings(existing, {"PROXY_USER": user, "PROXY_PASSWORD_HASH": f"'{hashed}'"})
    write_private(args.env_file, updated)
    print(f"wrote PROXY_USER={user} and PROXY_PASSWORD_HASH to {args.env_file}")
    print("Apply it: docker compose up -d   (then: make proxy-check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
