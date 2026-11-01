#!/usr/bin/env python3
"""setup_categories.py: qBittorrent categories and save paths, as code (issue #6, C2).

Talks to the running qBittorrent through its Web API (so qBittorrent itself validates and
stores everything) and makes sure that:

  * the categories movies, series, audiobooks and music exist, each with the save path
    /data/downloads/<category> (paths as the qbittorrent container sees them);
  * the default save path is /data/downloads, unfinished torrents sit in
    /data/downloads/incomplete (so half-finished files are never in a category folder);
  * Automatic Torrent Management is on for new torrents, which is what makes a torrent
    added with a category land in that category's folder.

Other categories and all other settings are left alone. Safe to re-run: it changes only
what differs and says so.

Usage:
    scripts/qbittorrent/setup_categories.py [--url URL] [--user NAME] [--password-stdin]
                                            [--dry-run]

    --url             Web UI address (default http://127.0.0.1:8080, what compose publishes)
    --user            Web UI user (default admin; set by scripts/qbittorrent/seed_config.py)
    --password-stdin  read the password from stdin instead of asking on the terminal
    --dry-run         print what would be set, contact nothing

The password is never put on the command line, in a file or in the output.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# The container-side view of DATA_ROOT (docker-compose.yml mounts it at /data).
DOWNLOAD_ROOT = "/data/downloads"
INCOMPLETE_DIR = f"{DOWNLOAD_ROOT}/incomplete"
# Keep in sync with DOWNLOAD_DIRS in scripts/host/create-layout.sh (a unit test checks).
CATEGORIES = ("movies", "series", "audiobooks", "music")

WANTED_PREFERENCES: dict[str, object] = {
    "save_path": DOWNLOAD_ROOT,
    "temp_path_enabled": True,
    "temp_path": INCOMPLETE_DIR,
    # Category folders only apply to torrents in "automatic" management mode.
    "auto_tmm_enabled": True,
    "torrent_changed_tmm_enabled": True,
    "save_path_changed_tmm_enabled": True,
    "category_changed_tmm_enabled": True,
}


class SetupError(Exception):
    """A problem the user can fix; reported without a traceback."""


def category_path(name: str) -> str:
    return f"{DOWNLOAD_ROOT}/{name}"


class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, path: str, form: dict[str, str] | None = None) -> str:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        try:
            with self.opener.open(f"{self.base}/api/v2/{path}", data=data, timeout=20) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            hint = " (too many failed logins? wait, then retry)" if exc.code == 403 else ""
            raise SetupError(f"qBittorrent answered {exc.code} for {path}{hint}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise SetupError(
                f"cannot reach qBittorrent at {self.base}: {exc}. "
                "Is the stack up (docker compose ps) and the Web UI published?"
            ) from exc

    def login(self, user: str, password: str) -> None:
        answer = self.call("auth/login", {"username": user, "password": password}).strip()
        if answer != "Ok.":
            raise SetupError("login refused: wrong user or password")


def plan_categories(existing: dict[str, dict[str, str]]) -> list[tuple[str, str, str]]:
    """(action, category, save path) for each wanted category that is missing or differs."""
    actions = []
    for name in CATEGORIES:
        path = category_path(name)
        if name not in existing:
            actions.append(("createCategory", name, path))
        elif existing[name].get("savePath") != path:
            actions.append(("editCategory", name, path))
    return actions


def plan_preferences(current: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in WANTED_PREFERENCES.items() if current.get(k) != v}


def describe_wanted() -> list[str]:
    lines = [f"category {name}: save path {category_path(name)}" for name in CATEGORIES]
    lines += [f"setting {key} = {value}" for key, value in WANTED_PREFERENCES.items()]
    return lines


def apply(api: Api) -> list[str]:
    changes = []
    existing = json.loads(api.call("torrents/categories") or "{}")
    for action, name, path in plan_categories(existing):
        api.call(f"torrents/{action}", {"category": name, "savePath": path})
        verb = "created" if action == "createCategory" else "updated"
        changes.append(f"{verb} category {name}: {path}")
    current = json.loads(api.call("app/preferences"))
    wanted = plan_preferences(current)
    if wanted:
        api.call("app/setPreferences", {"json": json.dumps(wanted)})
        changes += [f"set {key} = {value}" for key, value in wanted.items()]
    return changes


def read_password(from_stdin: bool) -> str:
    password = sys.stdin.readline().rstrip("\r\n") if from_stdin else getpass.getpass("Password: ")
    if not password:
        raise SetupError("no password given")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up qBittorrent categories and save paths.")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, contact nothing")
    args = parser.parse_args(argv)

    if args.dry_run:
        print("dry run: would make sure that")
        for line in describe_wanted():
            print(f"  {line}")
        return 0
    try:
        api = Api(args.url)
        api.login(args.user, read_password(args.password_stdin))
        changes = apply(api)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if changes:
        print("\n".join(changes))
    else:
        print("nothing to change: categories and save paths are already set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
