"""scripts/ops/autodeploy.py: pull-based automatic deployment with rollback (issue #42).

Real git (a bare "GitLab" repository, the install folder as a clone, a developer clone that pushes)
and a real SQLite database; fake docker, GitLab API, backup and host checks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "autodeploy.py"
spec = importlib.util.spec_from_file_location("ytp_autodeploy", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["ytp_autodeploy"] = mod
spec.loader.exec_module(mod)

sys.path.insert(0, str(ROOT / "app"))
from ytp.db import Database, load_migrations  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}  # fmt: skip
REAL_MIGRATIONS = [m.name for m in load_migrations()]
TOKEN = "glpat-test-token-12345"  # a test value, low entropy on purpose
SERVICES = ("gluetun", "qbittorrent", "app", "proxy", "watchdog")


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
        check=True,
    )  # fmt: skip
    return done.stdout.strip()


# --------------------------------------------------------------------------- the fake world


class World:
    """GitLab (bare repo + pipelines), the install folder, and what docker does."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.remote = tmp_path / "gitlab.git"
        self.dev = tmp_path / "dev"
        self.stack = tmp_path / "stack"
        self.clock = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        self.said: list[str] = []
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.pipelines: dict[str, str] = {}  # sha -> status
        self.pipeline_http = 200
        self.healthy_by_sha: dict[str, bool] = {}  # what the stack does when started at that sha
        self.default_healthy = True
        self.up_fails = False
        self.running_sha: str | None = None
        self.backup_rc = 0
        self.checks_rc = 0
        self.stop_rc = 0
        self.migrated_by_up = False
        self.ups = 0

        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(tmp_path, "clone", "-q", str(self.remote), str(self.dev))
        self.write_dev(".env.example", "TZ=Europe/Berlin\nPUID=1000\n")
        for name in REAL_MIGRATIONS:
            self.write_dev(f"app/ytp/migrations/{name}", f"-- {name}\n")
        self.v1 = self.commit("first")
        git(self.dev, "push", "-q", "origin", "main")
        git(tmp_path, "clone", "-q", str(self.remote), str(self.stack))
        (self.stack / ".env").write_text("TZ=Europe/Berlin\nPUID=1000\n")
        (self.stack / ".env").chmod(0o600)
        (self.stack / "config" / "app").mkdir(parents=True)
        Database.open(self.stack / "config" / "app" / "app.db").close()
        self.running_sha = self.v1
        self.pipelines[self.v1] = "success"

    # -- git
    def write_dev(self, path: str, text: str) -> None:
        target = self.dev / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str) -> str:
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", message)
        return git(self.dev, "rev-parse", "HEAD")

    def publish(
        self, message: str = "change", *, pipeline: str | None = "success", **files: str
    ) -> str:
        for path, text in files.items():
            self.write_dev(path.replace("__", "/"), text)
        if not files:
            self.write_dev("note.txt", message)
        sha = self.commit(message)
        git(self.dev, "push", "-q", "origin", "main")
        if pipeline is not None:
            self.pipelines[sha] = pipeline
        return sha

    def head(self) -> str:
        return git(self.stack, "rev-parse", "HEAD")

    # -- the pieces handed to autodeploy
    @property
    def cfg(self):
        return mod.Config(
            root=self.stack, remote=str(self.remote), branch="main", health_wait=30,
            api_base="http://gitlab.test", project="web/youtube-premium",
        )  # fmt: skip

    def env(self):
        return mod.Env(
            shell=FakeShell(self), http=self.http, now=lambda: self.clock, sleep=self.sleep,
            say=self.said.append, python="python3",
        )  # fmt: skip

    def sleep(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    def http(self, url: str, headers: dict[str, str], timeout: float):
        self.requests = getattr(self, "requests", [])
        self.requests.append((url, dict(headers)))
        if self.pipeline_http != 200:
            return self.pipeline_http, b""
        sha = url.split("sha=")[1].split("&")[0]
        if sha not in self.pipelines:
            return 200, b"[]"
        other = [{"id": 1, "sha": "f" * 40, "status": "failed"}]
        return 200, json.dumps(
            [{"id": 9, "sha": sha, "status": self.pipelines[sha]}, *other]
        ).encode()

    def token(self, mode: int = 0o600) -> None:
        directory = self.stack / "config" / "autodeploy"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "token").write_text(TOKEN + "\n")
        (directory / "token").chmod(mode)

    # -- the app's database
    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.stack / "config" / "app" / "app.db")
        conn.row_factory = sqlite3.Row
        return conn

    def alerts(self) -> list[str]:
        conn = self.db()
        try:
            return [r["kind"] for r in conn.execute("SELECT kind FROM alerts ORDER BY id")]
        finally:
            conn.close()

    def state(self) -> dict:
        return json.loads((self.stack / "config" / "autodeploy" / "state.json").read_text())

    def docker_calls(self, *words: str) -> list[list[str]]:
        return [c for c in self.commands if c[:1] == ["docker"] and all(w in c for w in words)]


class FakeShell:
    """git is real; docker, systemctl, the backup script and the host checks are scripted."""

    def __init__(self, world: World):
        self.world = world

    def run(self, argv, *, cwd=None, env=None, timeout=None):
        world = self.world
        world.commands.append(list(argv))
        world.envs.append(dict(env or {}))
        if argv[0] == "git":
            done = subprocess.run(
                argv, cwd=cwd, env={**os.environ, **GIT_ENV, **(env or {})},
                capture_output=True, text=True, check=False,
            )  # fmt: skip
            return mod.Result(done.returncode, done.stdout, done.stderr)
        if argv[0] == "docker":
            return self.docker(argv)
        if argv[-2:] == ["backup.py", "create"] or argv[-1] == "create":
            return mod.Result(world.backup_rc, "", "disk full" if world.backup_rc else "")
        if argv[0].endswith("check-host.sh"):
            return mod.Result(world.checks_rc)
        if argv[0] == "systemctl":
            return mod.Result(0)
        return mod.Result(0)

    def docker(self, argv):
        world = self.world
        if argv[1:3] == ["compose", "ps"]:
            # Before the first `up` of this run the stack is as it was (default); afterwards it
            # depends on the version that was started.
            healthy = (
                world.default_healthy
                if world.ups == 0
                else world.healthy_by_sha.get(world.running_sha, world.default_healthy)
            )
            items = [
                {"Service": s, "State": "running", "Health": "healthy" if healthy else "starting"}
                for s in SERVICES
            ]
            return mod.Result(0, "\n".join(json.dumps(i) for i in items))
        if argv[1:3] == ["compose", "up"]:
            world.ups += 1
            if world.up_fails:
                return mod.Result(1, "", "build failed: boom")
            world.running_sha = git(world.stack, "rev-parse", "HEAD")
            if world.migrated_by_up and (world.stack / "app/ytp/migrations/0099_new.sql").exists():
                conn = world.db()
                with conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations (version, name, checksum) "
                        "VALUES (99, '0099_new.sql', 'x')"
                    )
                    conn.execute(
                        "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC"
                        + "n" * 22
                        + "', 'made by new code')"
                    )
                conn.close()
            return mod.Result(0)
        if argv[1:3] == ["compose", "stop"]:
            world.running_sha = None
            return mod.Result(world.stop_rc)
        return mod.Result(0)


@pytest.fixture
def world(tmp_path) -> World:
    if shutil.which("git") is None:
        reason = (
            "git is NOT installed: about 60 autodeploy tests that need a real repository are "
            "SKIPPED and prove nothing. Install git (apt install git)."
        )
        if os.environ.get("CI"):  # GitLab sets it: a pipeline must never silently skip these
            pytest.fail(reason + " The test:unit job installs it in its before_script.")
        pytest.skip(reason)
    w = World(tmp_path)
    w.token()
    return w


def run(world: World):
    return mod.run_once(world.cfg, world.env())


# --------------------------------------------------------------------------- config


def make_env_file(tmp_path: Path, text: str) -> Path:
    (tmp_path / ".env").write_text(text)
    return tmp_path


def test_the_config_comes_from_env_and_derives_api_and_project(tmp_path):
    root = make_env_file(
        tmp_path, "AUTODEPLOY_REMOTE=http://192.168.178.194/web-projects/youtube-premium.git\n"
    )
    cfg = mod.Config.load(root, {})
    assert cfg.api_base == "http://192.168.178.194"
    assert cfg.project == "web-projects/youtube-premium"
    assert (cfg.branch, cfg.health_wait) == ("main", 600)


def test_the_process_environment_wins_over_env(tmp_path):
    root = make_env_file(tmp_path, "AUTODEPLOY_REMOTE=http://a/b/c.git\nAUTODEPLOY_BRANCH=x\n")
    cfg = mod.Config.load(root, {"AUTODEPLOY_BRANCH": "main", "AUTODEPLOY_HEALTH_WAIT": "120"})
    assert (cfg.branch, cfg.health_wait) == ("main", 120)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "AUTODEPLOY_REMOTE=ssh://git@gitlab/g/p.git\n",
        "AUTODEPLOY_REMOTE=http://user:secret@gitlab/g/p.git\n",
        "AUTODEPLOY_REMOTE=http://gitlab/project.git\n",
        "AUTODEPLOY_REMOTE=http://gitlab/g/p.git\nAUTODEPLOY_BRANCH=-evil\n",
        "AUTODEPLOY_REMOTE=http://gitlab/g/p.git\nAUTODEPLOY_BRANCH=a b\n",
    ],
)
def test_a_bad_config_is_refused_with_a_reason(tmp_path, text):
    root = make_env_file(tmp_path, text)
    with pytest.raises(mod.DeployError):
        mod.Config.load(root, {})


def test_a_silly_health_wait_falls_back_to_the_default(tmp_path):
    root = make_env_file(tmp_path, "AUTODEPLOY_REMOTE=http://a/b/c.git\nAUTODEPLOY_HEALTH_WAIT=5\n")
    assert mod.Config.load(root, {}).health_wait == 600


# --------------------------------------------------------------------------- nothing to do


def test_without_a_new_commit_nothing_happens(world):
    outcome = run(world)
    assert outcome.result == "current"
    assert world.docker_calls("up") == []
    assert world.state()["last_result"] == "current"


def test_the_status_file_for_the_dashboard_holds_no_secret_and_no_address(world):
    run(world)
    public = (world.stack / "config" / "app" / "autodeploy-status.json").read_text()
    assert TOKEN not in public
    assert "gitlab" not in public.lower()
    assert json.loads(public)["deployed"] == world.v1[:12]


# --------------------------------------------------------------------------- a good deploy


def test_a_new_green_commit_is_deployed_after_both_backups(world):
    sha = world.publish("feature")
    outcome = run(world)
    assert outcome.result == "deployed"
    assert world.head() == sha
    order = [c[0] + " " + " ".join(c[1:3]) for c in world.commands]
    backup_at = next(i for i, c in enumerate(world.commands) if c[-1] == "create")
    merge_at = next(i for i, c in enumerate(world.commands) if c[:2] == ["git", "merge"])
    up_at = next(i for i, c in enumerate(world.commands) if c[:3] == ["docker", "compose", "up"])
    assert backup_at < merge_at < up_at, order
    snapshot = world.stack / "config" / "autodeploy" / "predeploy.db"
    assert snapshot.is_file()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    state = world.state()
    assert (state["deployed"], state["previous"]) == (sha, world.v1)
    assert state["history"][-1]["result"] == "ok"
    assert world.alerts() == ["update_done"]


def test_the_deploy_alert_names_the_versions_and_nothing_secret(world):
    sha = world.publish("feature")
    run(world)
    conn = world.db()
    row = conn.execute("SELECT * FROM alerts").fetchone()
    conn.close()
    params = json.loads(row["params"])
    assert params == {"sha": sha[:12], "from_sha": world.v1[:12], "migrations": 0}
    assert TOKEN not in json.dumps(dict(row))


def test_new_defaults_of_the_template_are_added_to_env(world):
    world.publish("setting", **{".env.example": "TZ=Europe/Berlin\nPUID=1000\nNEW_THING=5\n"})
    assert run(world).result == "deployed"
    text = (world.stack / ".env").read_text()
    assert "NEW_THING=5" in text
    assert "TZ=Europe/Berlin" in text
    assert (world.stack / ".env").stat().st_mode & 0o777 == 0o600


def test_a_new_migration_is_counted_and_the_deploy_still_works(world):
    world.publish("migration", **{"app__ytp__migrations__0099_new.sql": "-- new\n"})
    assert run(world).result == "deployed"
    conn = world.db()
    params = json.loads(conn.execute("SELECT params FROM alerts").fetchone()[0])
    conn.close()
    assert params["migrations"] == 1


def test_failing_host_checks_are_reported_but_do_not_undo_a_healthy_stack(world):
    world.checks_rc = 1
    sha = world.publish("feature")
    assert run(world).result == "deployed"
    assert world.head() == sha
    assert world.alerts() == ["update_done", "update_checks"]


def test_nothing_is_deployed_twice(world):
    world.publish("feature")
    run(world)
    ups = len(world.docker_calls("up"))
    assert run(world).result == "current"
    assert len(world.docker_calls("up")) == ups


# --------------------------------------------------------------------------- waiting and blocking


def test_a_pipeline_that_is_still_running_means_wait(world):
    sha = world.publish("feature", pipeline="running")
    assert run(world).result == "waiting"
    assert world.head() == world.v1
    world.pipelines[sha] = "success"
    assert run(world).result == "deployed"


def test_a_commit_without_a_pipeline_yet_means_wait(world):
    world.publish("feature", pipeline=None)
    assert run(world).result == "waiting"
    assert world.head() == world.v1


@pytest.mark.parametrize("status", ["failed", "canceled", "skipped", "manual"])
def test_a_pipeline_that_is_not_green_blocks_and_is_announced_once(world, status):
    world.publish("feature", pipeline=status)
    assert run(world).result == "blocked"
    assert run(world).result == "blocked"
    assert world.head() == world.v1
    assert world.alerts() == ["update_blocked"], "once per version and reason"
    assert world.docker_calls("up") == []


def test_only_the_pipeline_of_that_exact_commit_counts(world):
    world.publish("feature", pipeline=None)
    world.pipelines["e" * 40] = "success"
    assert run(world).result == "waiting"


def test_an_unhealthy_stack_is_not_updated(world):
    world.publish("feature")
    world.default_healthy = False
    outcome = run(world)
    assert outcome.result == "blocked"
    assert "not healthy" in outcome.message
    assert world.head() == world.v1


def test_a_new_required_setting_without_a_default_stops_the_update_and_names_it(world):
    world.publish(
        "needs a person",
        **{".env.example": "TZ=Europe/Berlin\nPUID=1000\nNEW_SECRET=changeme\nOTHER=changeme\n"},
    )
    outcome = run(world)
    assert outcome.result == "blocked"
    assert "NEW_SECRET" in outcome.message
    assert "OTHER" in outcome.message
    assert world.head() == world.v1
    assert "update_blocked" in world.alerts()
    # once the owner has answered, the update goes through
    (world.stack / ".env").write_text("TZ=Europe/Berlin\nPUID=1000\nNEW_SECRET=x\nOTHER=y\n")
    assert run(world).result == "deployed"


def test_local_changes_in_the_install_folder_block_the_update(world):
    world.publish("feature")
    (world.stack / "note.txt").write_text("edited by hand")
    (world.stack / ".env.example").write_text("changed")
    outcome = run(world)
    assert outcome.result == "blocked"
    assert "local changes" in outcome.message


def test_a_history_that_was_rewritten_is_not_followed(world):
    world.publish("feature")
    run(world)
    git(world.dev, "reset", "-q", "--hard", world.v1)
    world.write_dev("other.txt", "different")
    sha = world.commit("different history")
    git(world.dev, "push", "-q", "--force", "origin", "main")
    world.pipelines[sha] = "success"
    outcome = run(world)
    assert outcome.result == "blocked"
    assert "does not continue" in outcome.message


def test_a_failed_backup_changes_nothing(world):
    world.publish("feature")
    world.backup_rc = 1
    outcome = run(world)
    assert outcome.result == "blocked"
    assert "backup" in outcome.message
    assert world.head() == world.v1
    assert world.docker_calls("up") == []


def test_a_broken_database_blocks_the_update_before_anything_is_changed(world):
    world.publish("feature")
    (world.stack / "config" / "app" / "app.db").write_bytes(b"not a database")
    assert run(world).result == "blocked"
    assert world.head() == world.v1


# --------------------------------------------------------------------------- pause, halt, lock


def test_paused_updates_do_nothing_until_resumed(world):
    world.publish("feature")
    mod.pause(world.cfg, world.env(), "holiday")
    assert run(world).result == "paused"
    assert world.head() == world.v1
    public = json.loads((world.stack / "config" / "app" / "autodeploy-status.json").read_text())
    assert public["paused"] is True
    mod.resume(world.cfg, world.env())
    assert run(world).result == "deployed"


def test_another_run_in_progress_means_busy(world):
    world.publish("feature")
    with mod.locked(world.cfg) as got:
        assert got
        assert run(world).result == "busy"
    assert world.head() == world.v1


# --------------------------------------------------------------------------- failure and rollback


def test_an_update_that_never_becomes_healthy_is_rolled_back_in_code(world):
    bad = world.publish("bad feature")
    world.healthy_by_sha[bad] = False
    outcome = run(world)
    assert outcome.result == "rolled_back"
    assert world.head() == world.v1
    assert world.running_sha == world.v1
    assert len(world.docker_calls("up")) == 2
    assert world.docker_calls("stop") == [], "no database restore for a code-only rollback"
    assert world.state()["failed_sha"] == bad
    assert world.alerts() == ["update_rolled_back"]
    assert not list((world.stack / "config" / "app").glob("app.db.failed-*"))


def test_the_failed_commit_is_not_tried_again_until_a_newer_one_exists(world):
    bad = world.publish("bad feature")
    world.healthy_by_sha[bad] = False
    run(world)
    ups = len(world.docker_calls("up"))
    assert run(world).result == "skipped"
    assert len(world.docker_calls("up")) == ups
    fixed = world.publish("the fix")
    outcome = run(world)
    assert outcome.result == "deployed"
    assert world.head() == fixed
    assert world.state()["failed_sha"] is None


def test_a_failing_build_is_rolled_back_too(world):
    world.publish("does not build")
    world.up_fails = True
    # the rollback build fails as well: the app must be left stopped, not half running
    outcome = run(world)
    assert outcome.result == "halted"
    assert world.docker_calls("stop", "app")


def test_a_database_that_was_migrated_by_the_failed_version_is_restored(world):
    conn = world.db()
    with conn:
        conn.execute(
            "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC"
            + "o" * 22
            + "', 'before the update')"
        )
    conn.close()
    bad = world.publish("migrates", **{"app__ytp__migrations__0099_new.sql": "-- new\n"})
    world.migrated_by_up = True
    world.healthy_by_sha[bad] = False
    outcome = run(world)
    assert outcome.result == "rolled_back"
    assert world.head() == world.v1
    conn = world.db()
    names = {r[0] for r in conn.execute("SELECT channel_name FROM subscriptions")}
    migrations = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    conn.close()
    assert names == {"before the update"}, "the changes of the new version are gone"
    assert "0099_new.sql" not in migrations
    assert world.docker_calls("stop", "app")
    aside = list((world.stack / "config" / "app").glob("app.db.failed-*"))
    assert len(aside) == 1
    assert aside[0].stat().st_mode & 0o777 == 0o600
    assert bad[:12] in aside[0].name
    kept = sqlite3.connect(aside[0])
    assert (
        kept.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = '0099_new.sql'"
        ).fetchone()[0]
        == 1
    )
    kept.close()
    assert world.alerts() == ["update_rolled_back_db"]


def test_stale_wal_files_are_removed_when_the_database_is_restored(world):
    bad = world.publish("migrates", **{"app__ytp__migrations__0099_new.sql": "-- new\n"})
    world.migrated_by_up = True
    world.healthy_by_sha[bad] = False
    db = world.stack / "config" / "app" / "app.db"
    db.with_name("app.db-wal").write_bytes(b"stale")
    db.with_name("app.db-shm").write_bytes(b"stale")
    run(world)
    assert not db.with_name("app.db-wal").exists()
    assert not db.with_name("app.db-shm").exists()


def test_a_migration_file_the_database_never_got_does_not_cost_a_restore(world):
    """The new version failed before it could migrate: the database is untouched, keep it."""
    bad = world.publish("migrates", **{"app__ytp__migrations__0099_new.sql": "-- new\n"})
    world.healthy_by_sha[bad] = False  # migrated_by_up stays False: the app never got to it
    conn = world.db()
    with conn:
        conn.execute(
            "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC"
            + "p" * 22
            + "', 'kept')"
        )
    conn.close()
    assert run(world).result == "rolled_back"
    conn = world.db()
    names = {r[0] for r in conn.execute("SELECT channel_name FROM subscriptions")}
    conn.close()
    assert "kept" in names
    assert world.alerts() == ["update_rolled_back"]
    assert not list((world.stack / "config" / "app").glob("app.db.failed-*"))


def test_when_the_rollback_fails_too_the_app_is_left_stopped_and_nothing_is_tried_again(world):
    bad = world.publish("bad feature")
    world.healthy_by_sha[bad] = False
    world.healthy_by_sha[world.v1] = False  # the old version does not come up either
    outcome = run(world)
    assert outcome.result == "halted"
    assert world.running_sha is None
    assert world.state()["halted"] is True
    assert "update_halted" in world.alerts()
    ups = len(world.docker_calls("up"))
    again = run(world)
    assert again.result == "halted"
    assert len(world.docker_calls("up")) == ups, "no retry loop"
    mod.resume(world.cfg, world.env())
    assert world.state()["halted"] is False


def test_a_missing_snapshot_halts_instead_of_guessing(world):
    bad = world.publish("migrates", **{"app__ytp__migrations__0099_new.sql": "-- new\n"})
    world.migrated_by_up = True
    world.healthy_by_sha[bad] = False
    original = mod._backups

    def backups_without_snapshot(cfg, env):
        problem = original(cfg, env)
        mod.snapshot_path(cfg).unlink()
        return problem

    mod._backups = backups_without_snapshot
    try:
        outcome = run(world)
    finally:
        mod._backups = original
    assert outcome.result == "halted"
    assert "no database snapshot" in outcome.message


def test_old_failed_databases_are_deleted_after_14_days_and_young_ones_kept(world):
    folder = world.stack / "config" / "app"
    old = folder / "app.db.failed-aaaaaaaaaaaa-20260101T000000Z"
    young = folder / "app.db.failed-bbbbbbbbbbbb-20261101T000000Z"
    for path, age_days in ((old, 30), (young, 2)):
        path.write_bytes(b"x")
        stamp = (world.clock - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))
    world.publish("feature")
    run(world)
    assert not old.exists()
    assert young.exists()


# --------------------------------------------------------------------------- by hand


def test_a_rollback_by_hand_goes_back_pauses_and_remembers_the_version(world):
    sha = world.publish("feature")
    run(world)
    assert world.head() == sha
    outcome = mod.rollback_by_hand(world.cfg, world.env())
    assert outcome.result == "rolled_back"
    assert world.head() == world.v1
    assert mod.is_paused(world.cfg)
    world.sleep(0)
    mod.resume(world.cfg, world.env())
    assert run(world).result == "skipped", "the version that was rolled back is not redeployed"


def test_a_rollback_by_hand_without_a_previous_version_says_so(world):
    with pytest.raises(mod.DeployError, match="no previous version"):
        mod.rollback_by_hand(world.cfg, world.env())


# --------------------------------------------------------------------------- the credential


def test_the_token_never_appears_on_a_command_line_in_the_environment_of_a_log_or_in_files(world):
    world.publish("feature")
    run(world)
    for command in world.commands:
        assert TOKEN not in " ".join(command)
    git_env = next(
        e for c, e in zip(world.commands, world.envs, strict=True) if c[:2] == ["git", "fetch"]
    )
    assert git_env["GIT_ASKPASS"].endswith("scripts/ops/git-askpass.sh")
    assert git_env["AUTODEPLOY_TOKEN_FILE"].endswith("config/autodeploy/token")
    assert git_env["GIT_TERMINAL_PROMPT"] == "0"
    assert TOKEN not in json.dumps(git_env)
    assert all(TOKEN not in line for line in world.said)
    for path in (world.stack / "config").rglob("*"):
        if path.is_file() and path.name != "token" and path.suffix != ".db":
            assert TOKEN not in path.read_text(errors="ignore"), path
    assert all(TOKEN not in url for url, _ in world.requests)


def test_the_api_call_sends_the_token_in_a_header_only(world):
    world.publish("feature")
    run(world)
    url, headers = world.requests[0]
    assert TOKEN not in url
    assert headers["PRIVATE-TOKEN"] == TOKEN
    assert "/api/v4/projects/web%2Fyoutube-premium/pipelines" in url


def test_a_missing_token_is_a_clear_offline_result_and_changes_nothing(world):
    (world.stack / "config" / "autodeploy" / "token").unlink()
    world.publish("feature")
    outcome = run(world)
    assert outcome.result == "offline"
    assert "token is missing" in outcome.message
    assert world.head() == world.v1


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_a_token_that_others_can_read_is_refused(world, mode):
    world.token(mode)
    with pytest.raises(mod.DeployError, match="must not be readable"):
        mod.read_token(world.cfg)


@pytest.mark.parametrize("text", ["", "short", "has space inside token", "bad/char/token-value"])
def test_something_that_is_not_a_token_is_refused(world, text):
    world.token()
    (world.stack / "config" / "autodeploy" / "token").write_text(text)
    with pytest.raises(mod.DeployError):
        mod.read_token(world.cfg)


def test_only_the_first_line_of_the_token_file_counts(world):
    world.token()
    (world.stack / "config" / "autodeploy" / "token").write_text(TOKEN + "\nsecond line\n")
    assert mod.read_token(world.cfg) == TOKEN


@pytest.mark.parametrize(
    ("http", "message"),
    [
        (401, "refused the token"),
        (403, "refused the token"),
        (500, "did not answer"),
        (0, "did not answer"),
    ],
)
def test_gitlab_trouble_is_a_calm_offline_result(world, http, message):
    world.publish("feature")
    world.pipeline_http = http
    outcome = run(world)
    assert outcome.result == "offline"
    assert message in outcome.message
    assert world.head() == world.v1


def test_an_unreachable_remote_is_offline_not_a_crash(world):
    world.publish("feature")
    shutil.rmtree(world.remote)
    outcome = run(world)
    assert outcome.result == "offline"
    assert "cannot fetch" in outcome.message


def test_a_garbled_pipeline_answer_is_an_error_not_a_deploy(world):
    world.publish("feature")
    original = world.http
    world.http = lambda url, headers, timeout: (200, b"<html>")  # type: ignore[method-assign]
    try:
        assert run(world).result == "offline"
    finally:
        world.http = original  # type: ignore[method-assign]
    assert world.head() == world.v1


# --------------------------------------------------------------------------- the helper script


@pytest.mark.parametrize(
    ("prompt", "expected", "code"),
    [
        ("Username for 'http://gitlab.lan': ", "oauth2", 0),
        ("Password for 'http://oauth2@gitlab.lan': ", TOKEN, 0),
        ("something else", "", 1),
    ],
)
def test_the_askpass_helper_answers_from_the_token_file(tmp_path, prompt, expected, code):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\nignored second line\n")
    done = subprocess.run(
        [str(ROOT / "scripts/ops/git-askpass.sh"), prompt],
        env={**os.environ, "AUTODEPLOY_TOKEN_FILE": str(token)},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == code
    assert done.stdout.strip() == expected


def test_the_askpass_helper_fails_without_a_token_file_setting():
    env = {k: v for k, v in os.environ.items() if k != "AUTODEPLOY_TOKEN_FILE"}
    done = subprocess.run(
        [str(ROOT / "scripts/ops/git-askpass.sh"), "Password for 'x': "],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode != 0


# --------------------------------------------------------------------------- check and status


def test_check_reports_every_question(world):
    lines = mod.check(world.cfg, world.env())
    texts = " | ".join(text for _, text in lines)
    assert "token file" in texts
    assert "GitLab answers" in texts
    assert "pipeline of that commit is: success" in texts
    assert "docker compose works" in texts
    assert "the app database exists" in texts
    assert all(ok for ok, _ in lines[:-1]), lines


def test_check_names_a_missing_token_and_a_gitlab_that_refuses(world):
    world.pipeline_http = 401
    lines = mod.check(world.cfg, world.env())
    assert any(not ok and "refused the token" in text for ok, text in lines)
    (world.stack / "config" / "autodeploy" / "token").unlink()
    lines = mod.check(world.cfg, world.env())
    assert any(not ok and "token is missing" in text for ok, text in lines)


def test_status_lines_show_version_pause_halt_and_history(world):
    world.publish("feature")
    run(world)
    mod.pause(world.cfg, world.env(), "x")
    text = "\n".join(mod.status_lines(world.cfg))
    assert "PAUSED" in text
    assert world.head()[:12] in text
    assert "ok" in text


def test_the_command_line_status_pause_resume(world, capsys):
    base = ["--repo-root", str(world.stack)]
    (world.stack / ".env").write_text(
        "AUTODEPLOY_REMOTE=http://gitlab.test/web/youtube-premium.git\n"
    )
    assert mod.main([*base, "pause", "--reason", "x"], env=world.env()) == 0
    assert mod.main([*base, "status"], env=world.env()) == 0
    assert "PAUSED" in capsys.readouterr().out
    assert mod.main([*base, "resume"], env=world.env()) == 0
    capsys.readouterr()
    assert mod.main([*base, "status", "--json"], env=world.env()) == 0
    assert json.loads(capsys.readouterr().out)["last_result"] == "resumed"


def test_without_a_setting_the_command_says_so_and_fails(tmp_path, capsys):
    assert mod.main(["--repo-root", str(tmp_path), "status"]) == 1
    assert "AUTODEPLOY_REMOTE" in capsys.readouterr().err


# --------------------------------------------------------------------------- the systemd units


def test_the_units_are_written_with_user_and_folder_and_the_timer_is_enabled(world, tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    shell_world = world
    shell_world.stack = ROOT  # the templates live in the real repository
    env = shell_world.env()
    mod.install_units(ROOT, "alice", env, unit_dir=unit_dir)
    service = (unit_dir / "youtube-premium-autodeploy.service").read_text()
    assert "User=alice" in service
    assert f"ExecStart=/usr/bin/python3 {ROOT}/scripts/ops/autodeploy.py run" in service
    assert "@USER@" not in service
    assert "@ROOT@" not in service
    assert "NoNewPrivileges=yes" in service
    timer = (unit_dir / "youtube-premium-autodeploy.timer").read_text()
    assert "OnUnitActiveSec=5min" in timer
    systemctl = [c for c in shell_world.commands if c[0] == "systemctl"]
    assert systemctl == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "youtube-premium-autodeploy.timer"],
    ]


def test_installing_the_units_twice_changes_nothing_and_a_dry_run_writes_nothing(world, tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    env = world.env()
    mod.install_units(ROOT, "alice", env, dry_run=True, unit_dir=unit_dir)
    assert list(unit_dir.iterdir()) == []
    assert any(line.startswith("+ ") for line in world.said)
    mod.install_units(ROOT, "alice", env, unit_dir=unit_dir)
    before = {p.name: p.stat().st_mtime_ns for p in unit_dir.iterdir()}
    mod.install_units(ROOT, "alice", env, unit_dir=unit_dir)
    assert {p.name: p.stat().st_mtime_ns for p in unit_dir.iterdir()} == before


@pytest.mark.parametrize("user", ["", "Root", "a b", "a;b", "../x", "x" * 40, "-x"])
def test_a_strange_user_name_is_refused(world, tmp_path, user):
    with pytest.raises(mod.DeployError):
        mod.install_units(ROOT, user, world.env(), unit_dir=tmp_path)


def test_a_folder_with_odd_characters_is_refused(world, tmp_path):
    with pytest.raises(mod.DeployError, match="characters"):
        mod.install_units(Path("/opt/my stack"), "alice", world.env(), unit_dir=tmp_path)
    with pytest.raises(mod.DeployError, match="characters"):
        mod.install_units(Path("/opt/a%b"), "alice", world.env(), unit_dir=tmp_path)


def test_the_units_can_be_removed_again(world, tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    mod.install_units(ROOT, "alice", world.env(), unit_dir=unit_dir)
    mod.remove_units(world.env(), unit_dir=unit_dir)
    assert list(unit_dir.iterdir()) == []
    assert ["systemctl", "disable", "--now", "youtube-premium-autodeploy.timer"] in world.commands
    mod.remove_units(world.env(), unit_dir=unit_dir)  # again: fine


def test_the_unit_templates_have_only_the_two_placeholders():
    import re

    for name in ("service", "timer"):
        text = (ROOT / "scripts/ops/systemd" / f"youtube-premium-autodeploy.{name}").read_text()
        assert set(re.findall(r"@\w+@", text)) <= {"@USER@", "@ROOT@"}


# --------------------------------------------------------------------------- the migration rule


def test_migrations_only_add_in_one_release():
    """Issue #42, rule for future migrations: new tables and new nullable or defaulted columns
    only. Dropping or renaming happens in a LATER release once no code uses it, so the previous
    version can still run on the new schema. 0005 rebuilt the jobs table and is the one exception
    (it keeps every row and id, tested in test_podcast_migration); a new exception must be added
    here on purpose, with a reason. 0013 has a DELETE FROM only inside a trigger that clears the
    new job_dismissals table: it touches no existing data, and the previous version never reads
    that table, so it still runs on the new schema. 0018 (#72) rebuilds jobs, alerts,
    podcast_backlog_items and vpn_events to add CHECK constraints: it keeps every column (same
    names, types, defaults and order), row and id, and only adds CHECKs that the previous
    release already satisfies (priority is written as 0/1 only, params/metadata are json.dumps
    of a dict, vpn events only carry the fixed condition set); it also drops two indexes that
    the previous release never names. Its DELETE FROM is the re-created trigger from 0013."""
    allowed = {"0005_podcasts.sql", "0018_indexes_and_constraints.sql"}
    allowed_words = {
        "0013_job_dismissals.sql": {"DELETE FROM"},
        "0018_indexes_and_constraints.sql": {"DELETE FROM"},
    }
    risky = ("DROP TABLE", "DROP COLUMN", "DROP INDEX", "RENAME TO", "RENAME COLUMN", "DELETE FROM")
    offenders = []
    for path in sorted((ROOT / "app/ytp/migrations").glob("*.sql")):
        if path.name in allowed:
            continue
        code = "\n".join(
            line.split("--")[0] for line in path.read_text(encoding="utf-8").splitlines()
        ).upper()
        offenders += [
            f"{path.name}: {word}"
            for word in risky
            if word in code and word not in allowed_words.get(path.name, ())
        ]
    assert not offenders, offenders
