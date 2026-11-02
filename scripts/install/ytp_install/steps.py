"""The installation steps, one function each.

Every step is safe to run again: it looks at what is already there and only does what is
missing, because the project's own scripts it calls are built that way (bootstrap.sh,
create-layout.sh, render_ovpn.py, set_password.py, seed_config.py, setup_key.py, ...). A step
that cannot finish raises `StepError`; the conversation shows its hint and stops, and the next
run continues with that step (`State` remembers which ones are done).

The order matters and is the reason for the list:

    prepare  Docker must exist before anything else uses it
    config   .env, because every later script reads it
    folders  the media folders (create-layout) and the hardlink proof
    vpn      the .ovpn file for gluetun
    logins   the web login, qBittorrent's login and the watchdog's key: all BEFORE the first
             start, because qBittorrent rewrites its config on exit and gluetun reads its auth
             file only at start
    start    build and start, wait until every service is healthy
    qbit     the categories, which need a running qBittorrent
    checks   check-host, the login through the proxy, Jellyfin, optionally the killswitch test
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import STATE_VERSION
from .answers import Answers
from .envfile import apply_settings, missing_settings, parse_env, write_env, write_private
from .runner import Result, Runner
from .system import LoginUser
from .texts import t

SERVICES = ("gluetun", "qbittorrent", "app", "proxy", "watchdog")
PLACEHOLDER = "changeme"
# Settings that are placeholders until their own step has run: not a reason to warn after `config`.
LATER_SETTINGS = {"PROXY_PASSWORD_HASH", "GLUETUN_CONTROL_APIKEY", "APP_ADMIN_PASSWORD_HASH"}
WAIT_SECONDS = 600
POLL_SECONDS = 5


class StepError(Exception):
    """A step failed. `hint` is the key of the plain-language explanation in `texts`."""

    step_id: str = ""  # set by run_steps: which step failed

    def __init__(self, hint: str, detail: str = ""):
        super().__init__(detail or hint)
        self.hint = hint
        self.detail = detail


@dataclass
class Step:
    id: str
    run: Callable[[Context], None]


@dataclass
class State:
    """Which steps are done, in `config/installer-state.json` (no secrets in it)."""

    path: Path
    completed: dict[str, str] = field(default_factory=dict)
    language: str = ""

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path)
        done = data.get("completed") if isinstance(data, dict) else None
        if not isinstance(done, dict) or data.get("version") != STATE_VERSION:
            return cls(path)
        language = data.get("language", "")
        return cls(
            path,
            {str(k): str(v) for k, v in done.items()},
            language if language in ("de", "en") else "",
        )

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def save(self, dry_run: bool = False) -> None:
        if dry_run:
            return
        write_private(
            self.path,
            json.dumps(
                {"version": STATE_VERSION, "language": self.language, "completed": self.completed},
                indent=2,
            )
            + "\n",
        )

    def mark(self, step_id: str, when: str, dry_run: bool = False) -> None:
        self.completed[step_id] = when
        self.save(dry_run)

    def forget(self, step_ids: list[str], dry_run: bool = False) -> None:
        for step_id in step_ids:
            self.completed.pop(step_id, None)
        self.save(dry_run)

    def first_open(self) -> str | None:
        return next((s.id for s in STEPS if s.id not in self.completed), None)


@dataclass
class Context:
    root: Path
    answers: Answers
    runner: Runner
    say: Callable[[str], None]
    lang: str
    login: LoginUser
    now: Callable[[], datetime]
    sleep: Callable[[float], None] = time.sleep
    previous: Answers | None = None  # the answers before a "change settings" run
    ask_secret: Callable[[str], str] | None = None
    confirm: Callable[[str], bool] | None = None
    http_get: Callable[[str, float], int] | None = None
    dry_run: bool = False
    wait_seconds: int = WAIT_SECONDS

    def tr(self, key: str, **values: object) -> str:
        return t(self.lang, key, **values)

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    def script(self, *parts: str) -> str:
        return str(self.root.joinpath("scripts", *parts))

    def hand_back(self, *paths: Path) -> None:
        """Give files the installer wrote as root back to the person who started it."""
        if self.dry_run or os.getuid() != 0:
            return
        for path in paths:
            if path.exists():
                os.chown(path, self.login.uid, self.login.gid)


def need(result: Result, hint: str) -> Result:
    if not result.ok:
        raise StepError(hint, (result.stdout + result.stderr).strip()[-400:])
    return result


# ------------------------------------------------------------------------------------ steps


def step_prepare(ctx: Context) -> None:
    a = ctx.answers
    apt = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get"]
    need(ctx.runner.run([*apt, "update"], stream=True), "step.prepare.hint")
    need(
        ctx.runner.run(
            [*apt, "install", "-y", "ca-certificates", "curl", "git", "make"], stream=True
        ),
        "step.prepare.hint",
    )
    need(
        ctx.runner.run(
            [
                ctx.script("host", "bootstrap.sh"),
                "--user", ctx.login.name,
                "--data-root", a.data_root,
                "--puid", str(a.puid),
                "--pgid", str(a.pgid),
            ],
            stream=True,
        ),
        "step.prepare.hint",
    )  # fmt: skip


def not_needed_settings(a: Answers) -> set[str]:
    """Settings of the VPN type that is not in use: their template placeholder is no problem."""
    if a.vpn_kind == "builtin" and a.vpn_type == "wireguard":
        return {"OPENVPN_USER", "OPENVPN_PASSWORD"}
    return {"WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES"}


def step_config(ctx: Context) -> None:
    example = (ctx.root / ".env.example").read_text(encoding="utf-8")
    base = ctx.env_path.read_text(encoding="utf-8") if ctx.env_path.exists() else example
    text = apply_settings(base, ctx.answers.env_values())
    added = missing_settings(example, text)
    if added:
        text = apply_settings(text, added)
        ctx.say(ctx.tr("env.added", names=", ".join(sorted(added))))
    irrelevant = LATER_SETTINGS | not_needed_settings(ctx.answers)
    open_values = sorted(
        k for k, v in parse_env(text).items() if v == PLACEHOLDER and k not in irrelevant
    )
    if open_values:
        ctx.say(ctx.tr("env.placeholder", names=", ".join(open_values)))
    if ctx.dry_run:
        ctx.say("+ write .env (mode 600; the old file is backed up first)")
        return
    try:
        backup = write_env(ctx.env_path, text, ctx.now())
    except OSError as exc:
        raise StepError("step.config.hint", str(exc)) from exc
    if backup is not None:
        ctx.say(ctx.tr("backup.made", name=backup.name))
        ctx.hand_back(backup)
    ctx.hand_back(ctx.env_path)


def step_folders(ctx: Context) -> None:
    need(
        ctx.runner.run([ctx.script("host", "create-layout.sh"), "--env-file", ".env"], stream=True),
        "step.folders.hint",
    )
    need(
        ctx.runner.run(
            [ctx.script("host", "check-host.sh"), "--only", "layout", "--env-file", ".env"],
            stream=True,
        ),
        "step.folders.hint",
    )


def step_vpn(ctx: Context) -> None:
    a = ctx.answers
    if a.vpn_kind != "ovpn":
        ctx.say(ctx.tr("step.vpn.not_needed"))
        return
    target = ctx.root / "config" / "expressvpn.ovpn"
    if a.ovpn_source:
        source = Path(a.ovpn_source)
        if ctx.dry_run:
            ctx.say(f"+ copy the .ovpn file to {target} (mode 600)")
        elif not target.exists() or source.resolve() != target.resolve():
            try:
                write_private(target, source.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                raise StepError("step.vpn.hint", str(exc)) from exc
            ctx.hand_back(target)
    elif not target.exists() and not ctx.dry_run:
        raise StepError("step.vpn.hint", "no .ovpn file")
    need(ctx.runner.run([ctx.script("vpn", "render_ovpn.py")]), "step.vpn.hint")


def qbit_conf(root: Path) -> Path:
    return root / "config" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"


def qbit_has_login(root: Path) -> bool:
    try:
        return "WebUI\\Password_PBKDF2" in qbit_conf(root).read_text(encoding="utf-8")
    except OSError:
        return False


def admin_hash_set(env: dict[str, str]) -> bool:
    """Has the first admin been made (a scrypt hash in .env, not the template's placeholder)?"""
    return env.get("APP_ADMIN_PASSWORD_HASH", PLACEHOLDER).startswith("scrypt:")


def set_admin(ctx: Context, user: str, password: str) -> None:
    """The app's first admin: `scripts/app/set_admin.py` writes the scrypt hash into .env."""
    need(
        ctx.runner.run(
            [ctx.script("app", "set_admin.py"), "--user", user, "--env-file", ".env",
             "--password-stdin"],
            input_text=password + "\n",
        ),
        "step.logins.hint",
    )  # fmt: skip


def step_logins(ctx: Context) -> None:
    a = ctx.answers
    env = parse_env(ctx.env_path.read_text(encoding="utf-8")) if ctx.env_path.exists() else {}
    hash_set = env.get("PROXY_PASSWORD_HASH", PLACEHOLDER) != PLACEHOLDER
    if a.web_password:
        # One login, two doors: the proxy's own for qBittorrent's port, and the app's first admin
        # account (#44) for the web page. The same name and password, set by two scripts.
        need(
            ctx.runner.run(
                [ctx.script("proxy", "set_password.py"), "--user", a.web_user,
                 "--env-file", ".env", "--password-stdin"],
                input_text=a.web_password + "\n",
            ),
            "step.logins.hint",
        )  # fmt: skip
        set_admin(ctx, a.web_user, a.web_password)
    elif not (hash_set and admin_hash_set(env)) and not ctx.dry_run:
        raise StepError("step.logins.hint", "no web password given")

    changed = ctx.previous is not None and ctx.previous.qbit_password != a.qbit_password
    if changed or not qbit_has_login(ctx.root):
        if changed:  # qBittorrent rewrites its config on exit: it must be stopped first
            ctx.runner.run(["docker", "compose", "stop", "qbittorrent"])
        need(
            ctx.runner.run(
                [ctx.script("qbittorrent", "seed_config.py"), "--user", a.qbit_user,
                 "--password-stdin"],
                input_text=a.qbit_password + "\n",
            ),
            "step.logins.hint",
        )  # fmt: skip
    need(
        ctx.runner.run([ctx.script("watchdog", "setup_key.py"), "--env-file", ".env"]),
        "step.logins.hint",
    )
    ctx.hand_back(ctx.env_path)


def parse_compose_ps(text: str) -> dict[str, tuple[str, str]]:
    """`docker compose ps --format json` -> {service: (state, health)}. Compose prints either one
    JSON object per line or one array, depending on its version; both are read."""
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
    except ValueError:
        items = []
        for line in text.splitlines():
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
    return {
        str(item.get("Service", "")): (str(item.get("State", "")), str(item.get("Health", "")))
        for item in items
        if isinstance(item, dict) and item.get("Service")
    }


def unhealthy_services(states: dict[str, tuple[str, str]]) -> list[str]:
    """Services that are missing, not running, or running with a health check that is not yet
    `healthy`. A service without a health check counts once it is running."""
    bad = []
    for service in SERVICES:
        state, health = states.get(service, ("", ""))
        if state != "running" or (health and health != "healthy"):
            bad.append(service)
    return bad


def wait_healthy(ctx: Context) -> None:
    if ctx.dry_run:
        ctx.say("+ wait until " + ", ".join(SERVICES) + " are healthy")
        return
    ctx.say(ctx.tr("wait.waiting", seconds=ctx.wait_seconds))
    waited = 0
    bad: list[str] = list(SERVICES)
    while waited <= ctx.wait_seconds:
        result = ctx.runner.run(
            ["docker", "compose", "ps", "--all", "--format", "json"], readonly=True
        )
        bad = unhealthy_services(parse_compose_ps(result.stdout)) if result.ok else list(SERVICES)
        if not bad:
            ctx.say(ctx.tr("wait.ok"))
            return
        ctx.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    raise StepError(
        "step.start.hint",
        ctx.tr("wait.timeout", seconds=ctx.wait_seconds, services=", ".join(bad)),
    )


def step_start(ctx: Context) -> None:
    need(
        ctx.runner.run(["docker", "compose", "up", "-d", "--build"], stream=True),
        "step.start.hint",
    )
    wait_healthy(ctx)


def step_qbit(ctx: Context) -> None:
    a = ctx.answers
    env = parse_env(ctx.env_path.read_text(encoding="utf-8")) if ctx.env_path.exists() else {}
    port = env.get("QBIT_WEBUI_PORT", "8080")
    need(
        ctx.runner.run(
            [ctx.script("qbittorrent", "setup_categories.py"), "--user", a.qbit_user,
             "--url", f"http://127.0.0.1:{port}", "--password-stdin"],
            input_text=a.qbit_password + "\n",
        ),
        "step.qbit.hint",
    )  # fmt: skip


def http_status(url: str, timeout: float) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def step_checks(ctx: Context) -> None:
    a = ctx.answers
    failed = False
    host = ctx.runner.run([ctx.script("host", "check-host.sh"), "--env-file", ".env"], stream=True)
    failed = failed or not host.ok
    password = a.web_password
    if not password and ctx.ask_secret is not None and not ctx.dry_run:
        password = ctx.ask_secret(ctx.tr("checks.proxy_password"))
    if password:
        ctx.runner.register_secrets([password])
        proxy = ctx.runner.run(
            [ctx.script("proxy", "verify_proxy.py"), "--env-file", ".env",
             "--host", a.lan_address, "--user", a.web_user, "--password-stdin"],
            input_text=password + "\n",
            stream=True,
        )  # fmt: skip
        failed = failed or not proxy.ok
    if a.jellyfin_url and not ctx.dry_run:
        getter = ctx.http_get or http_status
        status = getter(a.jellyfin_url.rstrip("/") + "/System/Info/Public", 5.0)
        ctx.say(
            ctx.tr("checks.jellyfin_ok")
            if status == 200
            else ctx.tr("checks.jellyfin_unreachable", url=a.jellyfin_url)
        )
    if ctx.confirm is not None and ctx.confirm("checks.killswitch"):
        killswitch = ctx.runner.run([ctx.script("vpn", "killswitch-test.sh")], stream=True)
        failed = failed or not killswitch.ok
    if failed:
        raise StepError("step.checks.hint")


STEPS = [
    Step("prepare", step_prepare),
    Step("config", step_config),
    Step("folders", step_folders),
    Step("vpn", step_vpn),
    Step("logins", step_logins),
    Step("start", step_start),
    Step("qbit", step_qbit),
    Step("checks", step_checks),
]
STEP_IDS = [s.id for s in STEPS]


def run_steps(ctx: Context, state: State, only_open: bool = True) -> None:
    """Run the steps in order, skipping the ones already done (unless `only_open` is False).
    Raises StepError at the first failure; the finished steps stay marked."""
    total = len(STEPS)
    for number, step in enumerate(STEPS, start=1):
        title = ctx.tr(f"step.{step.id}.title")
        if only_open and step.id in state.completed:
            ctx.say(ctx.tr("step.skipped", n=number, total=total, title=title))
            continue
        ctx.say("")
        ctx.say(ctx.tr("step.running", n=number, total=total, title=title))
        try:
            step.run(ctx)
        except StepError as error:
            error.step_id = step.id
            raise
        state.mark(step.id, ctx.now().isoformat(timespec="seconds"), ctx.dry_run)
