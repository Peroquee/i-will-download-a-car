#!/usr/bin/env python3
"""set_admin.py: set the app's first admin account in .env (issue #44).

Asks for a password (twice), hashes it with scrypt (the app's own `ytp.passwords`, standard
library only, so no Docker and no extra tool) and writes APP_ADMIN_USER and
APP_ADMIN_PASSWORD_HASH into .env. The password itself is never written, logged or put on a
command line.

Usage:
    scripts/app/set_admin.py [--user NAME] [--env-file PATH] [--dry-run] [--password-stdin]

The password is asked for on the terminal, or read from stdin with --password-stdin (for
automation). Safe to re-run: it replaces only the two lines it owns and keeps everything else in
the file, including the file's permissions (a new file gets mode 600).

Then run `docker compose up -d`. At start the app creates the admin account from these two
settings if no account has that name yet. If a hash that is different from the one it applied
last is set, the app resets that account's password and ends its sessions: this is also the way
back in when the admin password is lost. Existing subscriptions and jobs belong to the first
admin. The other accounts are managed in the app (Accounts page), not here.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

sys.path.insert(0, str(REPO_ROOT / "app"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "proxy"))
from set_password import apply_settings, env_value, write_private  # noqa: E402

from ytp import accounts, passwords  # noqa: E402


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password for the admin account: ")
        if getpass.getpass("Repeat it: ") != password:
            raise SystemExit("error: the two entries differ")
    problem = passwords.check_strength(password)
    if problem:
        raise SystemExit(f"error: {problem}")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set the app's admin user and password hash.")
    parser.add_argument(
        "--user", default=None, help="account name (default: APP_ADMIN_USER or admin)"
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help=".env path")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin"
    )
    args = parser.parse_args(argv)

    if not args.env_file.exists():
        raise SystemExit(f"error: {args.env_file} not found (copy .env.example to .env first)")
    existing = args.env_file.read_text(encoding="utf-8")

    user = args.user or env_value(existing, "APP_ADMIN_USER") or "admin"
    problem = accounts.check_username(user)
    if problem:
        raise SystemExit(f"error: {problem}")

    if args.dry_run:
        print(f"dry run: would ask for a password, hash it with scrypt and set in {args.env_file}:")
        print(f"  APP_ADMIN_USER={user}")
        print("  APP_ADMIN_PASSWORD_HASH=<scrypt hash>")
        return 0

    hashed = passwords.hash_password(read_password(args.password_stdin))
    updated = apply_settings(existing, {"APP_ADMIN_USER": user, "APP_ADMIN_PASSWORD_HASH": hashed})
    write_private(args.env_file, updated)
    print(f"wrote APP_ADMIN_USER={user} and APP_ADMIN_PASSWORD_HASH to {args.env_file}")
    print("Apply it: docker compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
