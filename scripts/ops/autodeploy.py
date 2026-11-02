#!/usr/bin/env python3
"""autodeploy.py: install a new version of youtube-premium by itself, safely (issue #42).

A systemd timer on the media VM runs `autodeploy.py run` every 5 minutes. Nothing connects to the
VM from outside: the VM LOOKS (pull), with a read-only credential, and installs a new commit of
`main` only if all of this is true:

* there is a new commit, and it continues the installed one (a fast-forward);
* GitLab's pipeline for exactly that commit is green;
* the stack is healthy right now (no update on top of something broken);
* no other run is going on (a lock), updates are not paused, and an earlier failed update has
  not stopped everything (`halted`);
* the new `.env.example` has no new required setting without a default (a person has to answer
  that, so the update waits and says so);
* the install folder has no local changes.

**The credential** is ONE GitLab project access token (role Reporter, scopes `read_api` and
`read_repository`) in `config/autodeploy/token` (mode 600). Git reads it through `GIT_ASKPASS`
(`git-askpass.sh`), the API call sends it in a header; it is never on a command line, in a URL or
in a log. It can read the repository and pipelines and change nothing.

**The deploy** is the update path of `./update.sh`: a full backup first (`backup.py create`), a
verified online snapshot of the database, `git merge --ff-only`, the new defaults of
`.env.example` into `.env`, `docker compose up -d --build`, wait until every service is healthy,
then the host checks (their result is reported, but only an unhealthy stack triggers a rollback).

**Rolling back** when the stack does not become healthy (decision of issue #42):

* code only (no new migration file, or the database does not have one of the new migrations): the
  previous commit is checked out and rebuilt;
* with a migration that the database already has: the app is stopped, the failed database is kept
  aside as `config/app/app.db.failed-<sha>-<time>` (mode 600, deleted after 14 days), the snapshot
  taken before the deploy is put back (stale -wal/-shm removed), the code is rolled back and
  started. The app refuses a database that is newer than itself, so old code cannot run on the new
  schema; the few minutes of job changes in between are safe to lose (jobs are keyed by
  infohash / video id / episode key, so doing them again is harmless; downloaded files stay);
* if the rollback itself fails the app is left STOPPED (never a half state), the state becomes
  `halted`, an alert says so, and nothing is tried again until a person runs `resume`.

The failed commit is remembered and not tried again until a newer commit exists.

Commands (all safe to repeat; `--dry-run` where it matters):
    autodeploy.py run               what the timer does
    autodeploy.py status [--json]   what is installed, the last result, paused or not
    autodeploy.py pause [--reason TEXT] / resume
    autodeploy.py check             can this machine do it? (token, GitLab, git, docker, folders)
    autodeploy.py rollback          go back to the previous version by hand (see docs/autodeploy.md)
    autodeploy.py install-units --user NAME [--dry-run]    (root) install and start the timer
    autodeploy.py remove-units [--dry-run]                 (root) stop and remove it

Settings (in `.env`, process environment wins): AUTODEPLOY_REMOTE (the GitLab repository address,
`http(s)://host/group/project.git`, required), AUTODEPLOY_BRANCH (main), AUTODEPLOY_HEALTH_WAIT
(seconds to wait for a healthy stack, 600). Everything stays on the LAN.

Alerts go into the app's alert log (#38) and a small status file for the dashboard; both are best
effort and never stop a deploy.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "install"))
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "app"))

import backup  # noqa: E402
from ytp_install.envfile import (  # noqa: E402
    apply_settings,
    missing_settings,
    parse_env,
    write_env,
)
from ytp_install.steps import PLACEHOLDER, parse_compose_ps, unhealthy_services  # noqa: E402

from ytp import alertlog  # noqa: E402

UNIT_NAME = "youtube-premium-autodeploy"
DEFAULT_BRANCH = "main"
DEFAULT_HEALTH_WAIT = 600
POLL_SECONDS = 5
FAILED_DB_DAYS = 14
KEEP_HISTORY = 10
MIGRATIONS_DIR = "app/ytp/migrations"
PIPELINE_WAIT = {"created", "pending", "running", "preparing", "waiting_for_resource", "scheduled"}
TOKEN_RE = re.compile(r"[A-Za-z0-9_.=-]{8,200}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
SAFE_PATH = re.compile(r"[A-Za-z0-9_./-]+")
UNIT_DIR = Path("/etc/systemd/system")


class DeployError(Exception):
    """Something went wrong that stops this run; the message is for people and safe to show."""


# --------------------------------------------------------------------------- environment


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Shell:
    """Runs commands. Replaced in tests."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Result:
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout} seconds: {argv[0]}")
        except OSError as exc:
            return Result(127, "", f"cannot run {argv[0]}: {exc}")
        return Result(done.returncode, done.stdout, done.stderr)


def real_http(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


@dataclass
class Config:
    root: Path
    remote: str
    branch: str = DEFAULT_BRANCH
    health_wait: int = DEFAULT_HEALTH_WAIT
    api_base: str = ""
    project: str = ""

    @property
    def state_dir(self) -> Path:
        return self.root / "config" / "autodeploy"

    @property
    def token_file(self) -> Path:
        return self.state_dir / "token"

    @property
    def db_path(self) -> Path:
        return self.root / "config" / "app" / "app.db"

    @property
    def status_file(self) -> Path:
        return self.root / "config" / "app" / "autodeploy-status.json"

    @classmethod
    def load(cls, root: Path, environ: dict[str, str] | None = None) -> Config:
        environ = os.environ if environ is None else environ
        env_file = root / ".env"
        values = parse_env(env_file.read_text(encoding="utf-8")) if env_file.is_file() else {}

        def get(name: str, default: str = "") -> str:
            return environ.get(name) or values.get(name) or default

        remote = get("AUTODEPLOY_REMOTE").strip()
        if not remote:
            raise DeployError(
                "AUTODEPLOY_REMOTE is not set: automatic updates are not configured "
                "(docs/autodeploy.md)"
            )
        parts = urlsplit(remote)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            raise DeployError(
                "AUTODEPLOY_REMOTE must be the repository address without a login, "
                "like http://gitlab.example/group/project.git"
            )
        project = parts.path.strip("/").removesuffix(".git")
        if not project or "/" not in project:
            raise DeployError("AUTODEPLOY_REMOTE must name the project, like .../group/project.git")
        wait = get("AUTODEPLOY_HEALTH_WAIT", str(DEFAULT_HEALTH_WAIT))
        branch = get("AUTODEPLOY_BRANCH", DEFAULT_BRANCH)
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or branch.startswith("-"):
            raise DeployError("AUTODEPLOY_BRANCH is not a plain branch name")
        return cls(
            root=root,
            remote=remote,
            branch=branch,
            health_wait=int(wait) if wait.isdigit() and 30 <= int(wait) <= 3600 else 600,
            api_base=f"{parts.scheme}://{parts.netloc}",
            project=project,
        )


@dataclass
class Env:
    """Everything a run needs from the outside; tests replace the parts."""

    shell: Shell = field(default_factory=Shell)
    http: Callable[[str, dict[str, str], float], tuple[int, bytes]] = real_http
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = lambda text: print(text, flush=True)
    python: str = sys.executable


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- state


def load_state(cfg: Config) -> dict[str, Any]:
    try:
        data = json.loads((cfg.state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.chmod(mode)
    tmp.replace(path)


def save_state(cfg: Config, env: Env, state: dict[str, Any]) -> None:
    """The private state, and the small public status the dashboard shows (no secrets in it)."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(cfg.state_dir / "state.json", json.dumps(state, indent=2, sort_keys=True))
    public = {
        "enabled": True,
        "paused": is_paused(cfg),
        "halted": bool(state.get("halted")),
        "deployed": (state.get("deployed") or "")[:12],
        "deployed_at": state.get("deployed_at"),
        "result": state.get("last_result"),
        "message": state.get("last_message"),
        "checked_at": state.get("checked_at"),
    }
    with contextlib.suppress(OSError):
        if cfg.status_file.parent.is_dir():
            write_private(cfg.status_file, json.dumps(public, sort_keys=True), 0o644)


def is_paused(cfg: Config) -> bool:
    return (cfg.state_dir / "paused").exists()


def remember(cfg: Config, env: Env, state: dict[str, Any], result: str, message: str) -> None:
    state["last_result"] = result
    state["last_message"] = message[:300]
    state["checked_at"] = stamp(env.now())
    save_state(cfg, env, state)


def add_history(state: dict[str, Any], env: Env, before: str, after: str, result: str) -> None:
    history = state.setdefault("history", [])
    history.append(
        {"time": stamp(env.now()), "from": before[:12], "to": after[:12], "result": result}
    )
    del history[:-KEEP_HISTORY]


# --------------------------------------------------------------------------- alerts


def notify(
    cfg: Config,
    kind: str,
    severity: str,
    title: str,
    detail: str,
    params: dict[str, Any] | None = None,
    key: str | None = None,
) -> None:
    """Write an alert into the app's alert log. Best effort: no database, no table (an older app),
    or a locked file is only printed."""
    try:
        conn = sqlite3.connect(cfg.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'alerts'").fetchone():
                return
            with conn:
                alertlog.insert(conn, kind, severity, title, detail, params, key=key)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"could not record the alert {kind}: {exc}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- git and GitLab


def git(cfg: Config, env: Env, *args: str, extra_env: dict[str, str] | None = None) -> Result:
    return env.shell.run(["git", *args], cwd=cfg.root, env=extra_env, timeout=600)


def read_token(cfg: Config) -> str:
    try:
        mode = cfg.token_file.stat().st_mode & 0o777
        lines = cfg.token_file.read_text(encoding="utf-8").strip().splitlines()
        text = lines[0].strip() if lines else ""  # the first line, like git-askpass.sh
    except OSError:
        raise DeployError(
            f"the GitLab token is missing: put it in {cfg.token_file} (mode 600), see "
            "docs/autodeploy.md"
        ) from None
    if mode & 0o077:
        raise DeployError(f"{cfg.token_file} must not be readable by others (chmod 600)")
    if not TOKEN_RE.fullmatch(text):
        raise DeployError(f"{cfg.token_file} does not hold a GitLab token")
    return text


def head_sha(cfg: Config, env: Env) -> str:
    result = git(cfg, env, "rev-parse", "HEAD")
    sha = result.stdout.strip()
    if not result.ok or not SHA_RE.fullmatch(sha):
        raise DeployError("cannot read the installed version (is this a git checkout?)")
    return sha


def fetch_remote(cfg: Config, env: Env) -> str:
    """Fetch the branch with the read-only token; returns the sha of its tip."""
    read_token(cfg)  # fail early with a clear message
    ref = f"refs/autodeploy/{cfg.branch}"
    result = git(
        cfg, env, "fetch", "--no-tags", "--quiet", cfg.remote, f"+refs/heads/{cfg.branch}:{ref}",
        extra_env={
            "GIT_ASKPASS": str(cfg.root / "scripts" / "ops" / "git-askpass.sh"),
            "AUTODEPLOY_TOKEN_FILE": str(cfg.token_file),
            "GIT_TERMINAL_PROMPT": "0",
        },
    )  # fmt: skip
    if not result.ok:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["?"]
        raise DeployError(f"cannot fetch {cfg.branch} from GitLab: {tail[0][:200]}")
    sha = git(cfg, env, "rev-parse", ref).stdout.strip()
    if not SHA_RE.fullmatch(sha):
        raise DeployError("GitLab answered with something that is not a commit")
    return sha


def pipeline_status(cfg: Config, env: Env, sha: str) -> str:
    """`success`, `wait` (not finished or not there yet), or the red status (`failed`, ...)."""
    token = read_token(cfg)
    url = (
        f"{cfg.api_base}/api/v4/projects/{quote(cfg.project, safe='')}/pipelines"
        f"?sha={sha}&ref={quote(cfg.branch, safe='')}&order_by=id&sort=desc&per_page=10"
    )
    status, body = env.http(url, {"PRIVATE-TOKEN": token, "Accept": "application/json"}, 20.0)
    if status == 401 or status == 403:
        raise DeployError("GitLab refused the token (expired, or missing the read_api scope)")
    if status != 200:
        raise DeployError(f"GitLab did not answer the pipeline question (HTTP {status or 'none'})")
    try:
        items = json.loads(body)
    except ValueError:
        raise DeployError("GitLab sent a pipeline list that is not JSON") from None
    mine = [p for p in items if isinstance(p, dict) and p.get("sha") == sha]
    if not mine:
        return "wait"
    newest = str(mine[0].get("status", ""))
    if newest == "success":
        return "success"
    return "wait" if newest in PIPELINE_WAIT else newest or "unknown"


def migrations_at(cfg: Config, env: Env, rev: str) -> set[str]:
    result = git(cfg, env, "ls-tree", "--name-only", rev, f"{MIGRATIONS_DIR}/")
    if not result.ok:
        return set()
    return {Path(line).name for line in result.stdout.splitlines() if line.endswith(".sql")}


def migrations_applied(db: Path) -> set[str] | None:
    """The migrations the live database has, or None if that cannot be read."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        finally:
            conn.close()
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------- the stack


def stack_unhealthy(cfg: Config, env: Env) -> list[str]:
    result = env.shell.run(
        ["docker", "compose", "ps", "--all", "--format", "json"], cwd=cfg.root, timeout=120
    )
    if not result.ok:
        return ["docker"]
    return unhealthy_services(parse_compose_ps(result.stdout))


def wait_healthy(cfg: Config, env: Env) -> bool:
    waited = 0
    while True:
        if not stack_unhealthy(cfg, env):
            return True
        if waited >= cfg.health_wait:
            return False
        env.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def compose_up(cfg: Config, env: Env) -> None:
    env_text = (cfg.root / ".env").read_text(encoding="utf-8")
    custom = parse_env(env_text).get("VPN_SERVICE_PROVIDER") == "custom"
    if custom and (cfg.root / "config" / "expressvpn.ovpn").exists():
        env.shell.run([env.python, str(cfg.root / "scripts/vpn/render_ovpn.py")], cwd=cfg.root)
    result = env.shell.run(["docker", "compose", "up", "-d", "--build"], cwd=cfg.root, timeout=3600)
    if not result.ok:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["?"]
        raise DeployError(f"docker compose up failed: {tail[0][:200]}")


def apply_env_defaults(cfg: Config, env: Env) -> list[str]:
    example = (cfg.root / ".env.example").read_text(encoding="utf-8")
    path = cfg.root / ".env"
    current = path.read_text(encoding="utf-8")
    added = missing_settings(example, current)
    if added:
        write_env(path, apply_settings(current, added), env.now())
    return sorted(added)


# --------------------------------------------------------------------------- decisions


@dataclass
class Outcome:
    """What a run did. `result` is a short word; `message` is for people."""

    result: str
    message: str = ""


def blocked(cfg: Config, env: Env, state: dict[str, Any], sha: str, why: str, code: str) -> Outcome:
    """The new version cannot be installed now. Said once per version and reason."""
    short = sha[:12]
    env.say(f"not updating to {short}: {why}")
    remember(cfg, env, state, "blocked", f"{short}: {why}")
    notify(
        cfg, "update_blocked", "warning", f"Version {short} was not installed",
        f"Version {short} was not installed: {why}", {"sha": short, "reason": why},
        key=f"deploy:blocked:{code}:{sha}",
    )  # fmt: skip
    return Outcome("blocked", why)


def decide_and_deploy(cfg: Config, env: Env, state: dict[str, Any]) -> Outcome:
    head = head_sha(cfg, env)
    state.setdefault("deployed", head)
    if git(cfg, env, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return blocked(cfg, env, state, head, "the install folder has local changes", "dirty")
    try:
        sha = fetch_remote(cfg, env)
    except DeployError as error:
        remember(cfg, env, state, "offline", str(error))
        env.say(str(error))
        return Outcome("offline", str(error))
    if sha == head:
        remember(cfg, env, state, "current", f"up to date at {head[:12]}")
        return Outcome("current")
    if sha == state.get("failed_sha"):
        env.say(f"{sha[:12]} failed before; waiting for a newer commit")
        remember(
            cfg, env, state, "failed", f"{sha[:12]} failed earlier; waiting for a newer commit"
        )
        return Outcome("skipped", "failed earlier")
    if git(cfg, env, "merge-base", "--is-ancestor", head, sha).returncode != 0:
        return blocked(
            cfg, env, state, sha, "the new version does not continue the installed one", "diverged"
        )
    try:
        pipeline = pipeline_status(cfg, env, sha)
    except DeployError as error:
        remember(cfg, env, state, "offline", str(error))
        env.say(str(error))
        return Outcome("offline", str(error))
    if pipeline == "wait":
        env.say(f"waiting for the pipeline of {sha[:12]}")
        remember(cfg, env, state, "waiting", f"waiting for the pipeline of {sha[:12]}")
        return Outcome("waiting", "pipeline not finished")
    if pipeline != "success":
        return blocked(cfg, env, state, sha, f"its pipeline is not green ({pipeline})", "pipeline")
    bad = stack_unhealthy(cfg, env)
    if bad:
        return blocked(
            cfg, env, state, sha,
            "the stack is not healthy right now (" + ", ".join(bad) + "); it is not changed on top "
            "of a problem", "unhealthy",
        )  # fmt: skip
    example = git(cfg, env, "show", f"{sha}:.env.example")
    if example.ok:
        added = missing_settings(example.stdout, (cfg.root / ".env").read_text(encoding="utf-8"))
        new_required = sorted(k for k, v in added.items() if v == PLACEHOLDER)
        if new_required:
            return blocked(
                cfg, env, state, sha,
                "it needs new settings that only a person can fill in: " + ", ".join(new_required),
                "settings",
            )  # fmt: skip
    return deploy(cfg, env, state, head, sha)


# --------------------------------------------------------------------------- the deploy


def snapshot_path(cfg: Config) -> Path:
    return cfg.state_dir / "predeploy.db"


def deploy(cfg: Config, env: Env, state: dict[str, Any], prev: str, sha: str) -> Outcome:
    short, prev_short = sha[:12], prev[:12]
    env.say(f"updating {prev_short} -> {short}")
    old_migrations = migrations_at(cfg, env, prev)
    new_migrations = sorted(migrations_at(cfg, env, sha) - old_migrations)

    # 1. Nothing is changed unless both backups are good.
    if backup_failed := _backups(cfg, env):
        return blocked(cfg, env, state, sha, backup_failed, "backup")

    # 2. The code.
    merged = git(cfg, env, "merge", "--ff-only", sha)
    if not merged.ok:
        return blocked(cfg, env, state, sha, "git could not move to the new version", "merge")
    state["snapshot_for"] = prev
    state["snapshot_old_migrations"] = sorted(old_migrations)
    save_state(cfg, env, state)

    # 3. Build, start, wait.
    problem = ""
    try:
        added = apply_env_defaults(cfg, env)
        if added:
            env.say("new settings with defaults added to .env: " + ", ".join(added))
        compose_up(cfg, env)
        if not wait_healthy(cfg, env):
            problem = "the stack did not become healthy: " + ", ".join(stack_unhealthy(cfg, env))
    except (DeployError, OSError) as error:
        problem = str(error)
    if problem:
        return rollback(cfg, env, state, prev, sha, problem, old_migrations)

    # 4. Done. The host checks are reported, they do not undo a healthy stack.
    checks = env.shell.run(
        [str(cfg.root / "scripts/host/check-host.sh"), "--env-file", ".env"],
        cwd=cfg.root, timeout=300,
    )  # fmt: skip
    now = stamp(env.now())
    state.update(
        previous=prev, deployed=sha, deployed_at=now, failed_sha=None, halted=False,
        blocked=None,
    )  # fmt: skip
    add_history(state, env, prev, sha, "ok")
    remember(cfg, env, state, "deployed", f"updated {prev_short} -> {short}")
    notify(
        cfg, "update_done", "info", f"Updated to {short}",
        f"The app was updated from {prev_short} to {short}.",
        {"sha": short, "from_sha": prev_short, "migrations": len(new_migrations)},
        key=f"deploy:done:{sha}",
    )  # fmt: skip
    if not checks.ok:
        notify(
            cfg, "update_checks", "warning", "The host checks report problems after the update",
            f"The stack is healthy after the update to {short}, but the host checks complain.",
            {"sha": short}, key=f"deploy:checks:{sha}",
        )  # fmt: skip
    prune_failed_databases(cfg, env)
    env.say(f"updated to {short}")
    return Outcome("deployed", short)


def _backups(cfg: Config, env: Env) -> str:
    """Make the full backup and the verified database snapshot; returns a problem or ''."""
    full = env.shell.run([env.python, str(cfg.root / "scripts/ops/backup.py"), "create"],
                         cwd=cfg.root, timeout=1800)  # fmt: skip
    if not full.ok:
        tail = (full.stderr or full.stdout).strip().splitlines()[-1:] or ["?"]
        return f"the backup before the update failed ({tail[0][:150]})"
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = snapshot_path(cfg)
    for leftover in (snapshot, snapshot.with_name(snapshot.name + "-wal"),
                     snapshot.with_name(snapshot.name + "-shm")):  # fmt: skip
        leftover.unlink(missing_ok=True)
    try:
        backup.backup_database(cfg.db_path, snapshot)
    except backup.BackupError as error:
        return f"the database snapshot before the update failed ({error})"
    snapshot.chmod(0o600)
    return ""


def rollback(
    cfg: Config,
    env: Env,
    state: dict[str, Any],
    prev: str,
    sha: str,
    problem: str,
    old_migrations: set[str],
) -> Outcome:
    """Go back to `prev`. See the module description for the three cases."""
    short, prev_short = sha[:12], prev[:12]
    env.say(f"{problem}; rolling back to {prev_short}")
    applied = migrations_applied(cfg.db_path)
    database_ahead = applied is None or bool(applied - old_migrations)
    restored = False
    try:
        if database_ahead:
            snapshot = snapshot_path(cfg)
            if not snapshot.is_file():
                raise DeployError("there is no database snapshot to go back to")
            stop = env.shell.run(["docker", "compose", "stop", "app"], cwd=cfg.root, timeout=300)
            if not stop.ok:
                raise DeployError("the app could not be stopped before the database was restored")
            restore_database(cfg, env, sha)
            restored = True
        reset = git(cfg, env, "reset", "--hard", prev)
        if not reset.ok:
            raise DeployError("git could not go back to the previous version")
        compose_up(cfg, env)
        if not wait_healthy(cfg, env):
            raise DeployError("the previous version did not become healthy either")
    except (DeployError, OSError) as error:
        return halt(cfg, env, state, prev, sha, f"{problem}; and the rollback failed: {error}")
    state.update(failed_sha=sha, halted=False)
    add_history(state, env, prev, sha, "rolled back" + (" with database" if restored else ""))
    remember(cfg, env, state, "rolled_back", f"{short} failed ({problem}); back at {prev_short}")
    kind = "update_rolled_back_db" if restored else "update_rolled_back"
    notify(
        cfg, kind, "error", f"The update to {short} failed and was undone",
        f"The update to {short} failed ({problem}). The previous version {prev_short} runs again"
        + (" with the database from before the update." if restored else "."),
        {"sha": short, "from_sha": prev_short, "reason": problem}, key=f"deploy:failed:{sha}",
    )  # fmt: skip
    env.say(f"rolled back to {prev_short}")
    return Outcome("rolled_back", problem)


def restore_database(cfg: Config, env: Env, sha: str) -> None:
    """Keep the failed database aside and put the pre-deploy snapshot in its place."""
    db = cfg.db_path
    aside = db.with_name(f"app.db.failed-{sha[:12]}-{env.now().strftime('%Y%m%dT%H%M%SZ')}")
    owner = db.parent.stat()
    if db.exists():
        shutil.move(str(db), str(aside))
        aside.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    shutil.copy2(snapshot_path(cfg), db)
    db.chmod(0o600)
    if os.geteuid() == 0:
        os.chown(db, owner.st_uid, owner.st_gid)
        if aside.exists():
            os.chown(aside, owner.st_uid, owner.st_gid)


def prune_failed_databases(cfg: Config, env: Env) -> None:
    limit = env.now() - timedelta(days=FAILED_DB_DAYS)
    for path in cfg.db_path.parent.glob("app.db.failed-*"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < limit:
                path.unlink()
        except OSError:
            continue


def halt(cfg: Config, env: Env, state: dict[str, Any], prev: str, sha: str, why: str) -> Outcome:
    """The rollback failed: stop the app, say so loudly, and do nothing more by itself."""
    short = sha[:12]
    env.shell.run(["docker", "compose", "stop", "app"], cwd=cfg.root, timeout=300)
    state.update(failed_sha=sha, halted=True)
    add_history(state, env, prev, sha, "halted")
    remember(cfg, env, state, "halted", why)
    notify(
        cfg, "update_halted", "error", "The update failed and the app is stopped",
        f"The update to {short} failed and could not be undone ({why}). The app was stopped.",
        {"sha": short, "reason": why}, key=f"deploy:halted:{sha}",
    )  # fmt: skip
    env.say(f"HALTED: {why}")
    return Outcome("halted", why)


# --------------------------------------------------------------------------- commands


@contextlib.contextmanager
def locked(cfg: Config) -> Iterator[bool]:
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (cfg.state_dir / "lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_once(cfg: Config, env: Env) -> Outcome:
    with locked(cfg) as got:
        if not got:
            env.say("another run is in progress")
            return Outcome("busy")
        state = load_state(cfg)
        if state.get("halted"):
            env.say("halted after a failed update; run `autodeploy.py resume` after looking at it")
            return Outcome("halted", "waiting for a person")
        if is_paused(cfg):
            remember(cfg, env, state, "paused", "updates are paused")
            return Outcome("paused")
        try:
            return decide_and_deploy(cfg, env, state)
        except DeployError as error:
            remember(cfg, env, state, "error", str(error))
            env.say(str(error))
            return Outcome("error", str(error))


def pause(cfg: Config, env: Env, reason: str) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(cfg.state_dir / "paused", (reason or "paused by hand")[:200] + "\n")
    state = load_state(cfg)
    remember(cfg, env, state, "paused", reason or "updates are paused")


def resume(cfg: Config, env: Env) -> None:
    (cfg.state_dir / "paused").unlink(missing_ok=True)
    state = load_state(cfg)
    state["halted"] = False
    remember(cfg, env, state, "resumed", "updates are on again")


def check(cfg: Config, env: Env) -> list[tuple[bool, str]]:
    """Can this machine do it? One (ok, text) line per question."""
    lines: list[tuple[bool, str]] = []

    def record(ok: bool, text: str) -> bool:
        lines.append((ok, text))
        return ok

    try:
        read_token(cfg)
        record(True, f"token file {cfg.token_file} is there, mode 600")
        token_ok = True
    except DeployError as error:
        token_ok = record(False, str(error))
    git_dir = git(cfg, env, "rev-parse", "--git-dir")
    record(
        git_dir.ok, "the install folder is a git checkout" if git_dir.ok else "not a git checkout"
    )
    if token_ok:
        try:
            sha = fetch_remote(cfg, env)
            record(True, f"GitLab answers, {cfg.branch} is at {sha[:12]}")
            record(True, f"the pipeline of that commit is: {pipeline_status(cfg, env, sha)}")
        except DeployError as error:
            record(False, str(error))
    docker = env.shell.run(["docker", "compose", "version"], cwd=cfg.root, timeout=60)
    if docker.ok:
        record(True, "docker compose works for this user")
    else:
        record(False, "docker compose does not work for this user (is it in the docker group?)")
    if cfg.db_path.exists():
        record(True, "the app database exists")
    else:
        record(False, f"{cfg.db_path} does not exist yet (start the stack first)")
    try:
        target = backup.default_target(cfg.root)
        backup.check_target(target, cfg.root)
        record(True, f"backups go to {target}")
    except backup.BackupError as error:
        record(False, f"the backup folder is not usable: {error}")
    return lines


def rollback_by_hand(cfg: Config, env: Env) -> Outcome:
    with locked(cfg) as got:
        if not got:
            return Outcome("busy", "another run is in progress")
        state = load_state(cfg)
        prev = state.get("previous")
        head = head_sha(cfg, env)
        if not prev or not SHA_RE.fullmatch(prev) or prev == head:
            raise DeployError("there is no previous version recorded to go back to")
        old = set(state.get("snapshot_old_migrations") or [])
        if state.get("snapshot_for") != prev or not snapshot_path(cfg).is_file():
            # Without the snapshot of exactly this deploy, a database that has moved on cannot be
            # taken back safely.
            applied = migrations_applied(cfg.db_path)
            if applied is None or applied - migrations_at(cfg, env, prev):
                raise DeployError(
                    "the database has migrations the previous version does not know and there is "
                    "no snapshot from before that update; restore a backup by hand "
                    "(docs/autodeploy.md)"
                )
            old = migrations_at(cfg, env, prev)
        write_private(cfg.state_dir / "paused", "paused after a rollback by hand\n")
        return rollback(cfg, env, state, prev, head, "rolled back by hand", old)


# ------------------------------------------------------------------------ the systemd units


def render_unit(text: str, user: str, root: Path) -> str:
    return text.replace("@USER@", user).replace("@ROOT@", str(root))


def install_units(
    cfg_root: Path, user: str, env: Env, *, dry_run: bool = False, unit_dir: Path = UNIT_DIR
) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise DeployError("that is not a plain user name")
    if not SAFE_PATH.fullmatch(str(cfg_root)):
        raise DeployError("the install folder has characters a unit file cannot hold safely")
    for name in (f"{UNIT_NAME}.service", f"{UNIT_NAME}.timer"):
        source = cfg_root / "scripts" / "ops" / "systemd" / name
        text = render_unit(source.read_text(encoding="utf-8"), user, cfg_root)
        target = unit_dir / name
        if dry_run:
            env.say(f"+ write {target}")
            continue
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            target.chmod(0o644)
    for argv in (
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", f"{UNIT_NAME}.timer"],
    ):
        if dry_run:
            env.say("+ " + " ".join(argv))
        elif not env.shell.run(argv).ok:
            raise DeployError(f"`{' '.join(argv)}` failed")


def remove_units(env: Env, *, dry_run: bool = False, unit_dir: Path = UNIT_DIR) -> None:
    steps = [["systemctl", "disable", "--now", f"{UNIT_NAME}.timer"]]
    for argv in steps:
        if dry_run:
            env.say("+ " + " ".join(argv))
        else:
            env.shell.run(argv)  # not installed is fine
    for name in (f"{UNIT_NAME}.service", f"{UNIT_NAME}.timer"):
        if dry_run:
            env.say(f"+ remove {unit_dir / name}")
        else:
            (unit_dir / name).unlink(missing_ok=True)
    if not dry_run:
        env.shell.run(["systemctl", "daemon-reload"])


def status_lines(cfg: Config) -> list[str]:
    state = load_state(cfg)
    lines = [
        f"installed:   {(state.get('deployed') or '?')[:12]}"
        + (f" since {state['deployed_at']}" if state.get("deployed_at") else ""),
        f"updates:     {'PAUSED' if is_paused(cfg) else 'on'}"
        + (
            ", HALTED after a failed update (run `resume` after looking at it)"
            if state.get("halted")
            else ""
        ),
        f"last run:    {state.get('last_result', 'never')} - {state.get('last_message', '')}"
        + (f" ({state['checked_at']})" if state.get("checked_at") else ""),
    ]
    if state.get("failed_sha"):
        lines.append(
            f"failed:      {state['failed_sha'][:12]} (not tried again until a newer commit)"
        )
    for item in state.get("history", [])[-5:]:
        lines.append(f"  {item['time']}  {item['from']} -> {item['to']}  {item['result']}")
    return lines


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodeploy.py", description=__doc__.split("\n")[0])
    parser.add_argument("--repo-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    pausing = sub.add_parser("pause")
    pausing.add_argument("--reason", default="")
    sub.add_parser("resume")
    sub.add_parser("check")
    sub.add_parser("rollback")
    units = sub.add_parser("install-units")
    units.add_argument("--user", required=True)
    units.add_argument("--dry-run", action="store_true")
    removing = sub.add_parser("remove-units")
    removing.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    env = env or Env()
    root = args.repo_root.resolve()
    try:
        if args.command == "remove-units":
            remove_units(env, dry_run=args.dry_run)
            return 0
        if args.command == "install-units":
            install_units(root, args.user, env, dry_run=args.dry_run)
            return 0
        cfg = Config.load(root)
        if args.command == "run":
            outcome = run_once(cfg, env)
            return 1 if outcome.result in ("halted", "error") else 0
        if args.command == "status":
            if args.json:
                print(json.dumps(load_state(cfg), indent=2, sort_keys=True))
            else:
                print("\n".join(status_lines(cfg)))
            return 0
        if args.command == "pause":
            pause(cfg, env, args.reason)
            env.say("updates are paused (resume with: autodeploy.py resume)")
            return 0
        if args.command == "resume":
            resume(cfg, env)
            env.say("updates are on again")
            return 0
        if args.command == "check":
            lines = check(cfg, env)
            for ok, text in lines:
                env.say(("ok    " if ok else "FAIL  ") + text)
            return 0 if all(ok for ok, _ in lines) else 1
        if args.command == "rollback":
            outcome = rollback_by_hand(cfg, env)
            env.say(f"{outcome.result}: {outcome.message}")
            return 0 if outcome.result == "rolled_back" else 1
    except DeployError as error:
        print(f"autodeploy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
