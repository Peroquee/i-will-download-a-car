"""The conversation: language, the menu for an existing installation, the questions, the summary.

`main()` is what `scripts/install/install.py` runs. Everything it needs from the outside (the
terminal, the command runner, the clock, the system checks) is passed in, so a test can play a
whole installation with a scripted terminal and a runner that only records.

Flows:

* **new installation**: ask everything, show the summary, run all steps;
* **interrupted installation** (the state file says `config` is done but not all steps): the answers
  are read back from `.env`, only the web password (never stored) is asked again if its step is
  still open, and the open steps run;
* **existing installation** (`.env` exists, nothing open): a menu: change settings / update /
  repair / state / quit; `--update`, `--repair`, `--status` jump straight there.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import answers as ans
from .answers import Answers
from .envfile import missing_settings, parse_env
from .runner import Runner, file_logger
from .steps import (
    SERVICES,
    STEP_IDS,
    STEPS,
    Context,
    State,
    StepError,
    parse_compose_ps,
    run_steps,
    step_config,
    unhealthy_services,
    wait_healthy,
)
from .system import (
    LoginUser,
    detect_lan,
    detect_login_user,
    detect_timezone,
    is_debian_12,
    timezone_known,
)
from .texts import t


class Terminal:
    """The real keyboard and screen. Tests pass a scripted one with the same four methods."""

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str, default: str = "") -> str:
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        return input(shown).strip() or default

    def secret(self, prompt: str) -> str:
        return getpass.getpass(f"{prompt}: ")

    def pause(self) -> None:  # pragma: no cover - only for the real terminal
        return None


class Interrupted(Exception):
    """The user closed the input (Ctrl+D) or pressed Ctrl+C."""


@dataclass
class Deps:
    """Everything `main` takes from outside, with real defaults."""

    term: Terminal = field(default_factory=Terminal)
    runner: Runner | None = None
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] | None = None
    login: LoginUser | None = None
    http_get: Callable[[str, float], int] | None = None
    token: Callable[[], str] = lambda: secrets.token_urlsafe(18)
    os_release: Path = Path("/etc/os-release")
    require_root: bool = True
    timezone: str | None = None
    detect: Callable[[Runner], object] = detect_lan
    wait_seconds: int | None = None


class Conversation:
    """Asking questions in one language, checking answers, asking again until they are fine."""

    def __init__(self, term: Terminal, lang: str):
        self.term = term
        self.lang = lang

    def tr(self, key: str, **values: object) -> str:
        return t(self.lang, key, **values)

    def say(self, text: str = "") -> None:
        self.term.say(text)

    def why(self, key: str) -> None:
        self.say("")
        self.say(self.tr(key))

    def text(
        self,
        key: str,
        default: str = "",
        check: Callable[[str], str | None] | None = None,
        optional: bool = False,
        clear_with_dash: bool = False,
        **values: object,
    ) -> str:
        """Ask for a visible value. An empty answer takes the default; with `optional`, an empty
        default is allowed. `-` clears an existing optional value."""
        while True:
            answer = self.term.ask(self.tr(key, **values), default)
            if clear_with_dash and answer == "-":
                return ""
            if not answer and not optional:
                self.say(self.tr("err.empty"))
                continue
            problem = check(answer) if (check and answer) else None
            if problem is None:
                return answer
            self.say(self.tr(problem))

    def secret(
        self,
        key: str,
        check: Callable[[str], str | None] | None = None,
        current: str = "",
        optional: bool = False,
        twice: bool = False,
        **values: object,
    ) -> str:
        """Ask for a hidden value. With a `current` value, Enter keeps it."""
        suffix = f"\n  ({self.tr('keep_secret')})" if current else ""
        while True:
            answer = self.term.secret(self.tr(key, **values) + suffix)
            if not answer:
                if current:
                    return current
                if optional:
                    return ""
                self.say(self.tr("err.empty"))
                continue
            problem = check(answer) if check else None
            if problem is not None:
                self.say(self.tr(problem))
                continue
            if twice and self.term.secret(self.tr("web_password2.ask")) != answer:
                self.say(self.tr("err.differ"))
                continue
            return answer

    def yes(self, key: str, default: bool = True, **values: object) -> bool:
        marks = self.tr("yes_chars")
        hint = "Y/n" if default else "y/N"
        if self.lang == "de":
            hint = "J/n" if default else "j/N"
        answer = self.term.ask(f"{self.tr(key, **values)} ({hint})", "").strip()
        if not answer:
            return default
        return answer[0] in marks

    def choice(self, key: str, options: list[str], default: str) -> str:
        while True:
            answer = self.term.ask(self.tr(key), default)
            if answer in options:
                return answer
            self.say(self.tr("err.choice", options=" / ".join(options)))


# ------------------------------------------------------------------------------ questions


def looks_like_ovpn(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "err.ovpn_missing"
    if len(content) > 200_000 or not any(
        line.strip().startswith("remote ") for line in content.splitlines()
    ):
        return "err.ovpn_content"
    return None


def collect(
    conv: Conversation,
    deps: Deps,
    runner: Runner,
    root: Path,
    login: LoginUser,
    existing: Answers | None,
) -> Answers:
    """Ask every question. With `existing` (change settings) the current values are the defaults
    and Enter keeps a stored secret."""
    a = Answers(**{**vars(existing)}) if existing else Answers()
    a.language = conv.lang
    if existing is None:
        a.timezone = deps.timezone or detect_timezone()
        a.puid = a.pgid = 1000
    else:
        conv.say("")
        conv.say(conv.tr("menu.change_note"))

    # --- the VPN
    conv.why("vpn_kind.why")
    kind = conv.choice("vpn_kind.ask", ["1", "2"], "1" if a.vpn_kind == "ovpn" else "2")
    a.vpn_kind = "ovpn" if kind == "1" else "builtin"
    if a.vpn_kind == "ovpn":
        conv.why("ovpn_path.why")
        have_file = (root / "config" / "expressvpn.ovpn").exists()
        while True:
            path_text = conv.text(
                "ovpn_path.ask",
                "",
                optional=have_file,
                user=login.name,
            )
            if not path_text:
                a.ovpn_source = ""
                break
            problem = looks_like_ovpn(Path(path_text).expanduser())
            if problem is None:
                a.ovpn_source = str(Path(path_text).expanduser())
                break
            conv.say(conv.tr(problem))
        conv.why("vpn_user.why")
        a.vpn_user = conv.text("vpn_user.ask", a.vpn_user, ans.check_vpn_user)
        a.vpn_password = conv.secret("vpn_password.ask", ans.check_vpn_secret, a.vpn_password)
    else:
        conv.why("vpn_provider.why")
        a.vpn_provider = conv.text("vpn_provider.ask", a.vpn_provider, ans.check_provider)
        a.vpn_type = (
            "wireguard"
            if conv.choice("vpn_type.ask", ["1", "2"], "2" if a.vpn_type == "wireguard" else "1")
            == "2"
            else "openvpn"
        )
        if a.vpn_type == "wireguard":
            a.wireguard_key = conv.secret("wg_key.ask", ans.check_vpn_secret, a.wireguard_key)
            a.wireguard_addresses = conv.text(
                "wg_addresses.ask", a.wireguard_addresses, ans.check_wireguard_addresses
            )
        else:
            a.vpn_user = conv.text("vpn_user.ask", a.vpn_user, ans.check_vpn_user)
            a.vpn_password = conv.secret("vpn_password.ask", ans.check_vpn_secret, a.vpn_password)
        a.vpn_countries = conv.text(
            "vpn_countries.ask", a.vpn_countries, ans.check_countries, optional=True,
            clear_with_dash=True,
        )  # fmt: skip

    # --- the media folder
    conv.why("data_root.why")
    a.data_root = conv.text("data_root.ask", a.data_root, ans.check_data_root).rstrip("/") or "/"
    names = ", ".join(a.dirs.values())
    if not conv.yes("folders_default.ask", True, names=names):
        for key in ans.LIBRARY_DEFAULTS:
            while True:
                a.dirs[key] = conv.text("folder.ask", a.dirs[key], kind=conv.tr(f"kind.{key}"))
                problem = ans.check_folder_names(a.dirs)
                if problem is None or not _complete(a.dirs):
                    break
                conv.say(conv.tr(problem))
    problem = ans.check_folder_names(a.dirs)
    if problem is not None:  # a changed default that clashes: ask again from the defaults
        conv.say(conv.tr(problem))
        a.dirs = dict(ans.LIBRARY_DEFAULTS)

    # --- the web login
    conv.why("web_user.why")
    a.web_user = conv.text("web_user.ask", a.web_user, ans.check_user)
    if existing is None or conv.yes("web_password_change.ask", False):
        a.web_password = conv.secret(
            "web_password.ask", ans.check_web_password, twice=True, n=ans.MIN_WEB_PASSWORD
        )

    # --- qBittorrent
    if not a.qbit_password:
        conv.why("qbit_auto.why")
        if conv.yes("qbit_auto.ask", True):
            a.qbit_password = deps.token()
        else:
            a.qbit_password = conv.secret(
                "qbit_password.ask", ans.check_qbit_password, twice=True, n=ans.MIN_QBIT_PASSWORD
            )

    # --- the home network
    conv.why("lan_confirm.why")
    lan = deps.detect(runner)
    if lan is not None and a.lan_address in {"", lan.address}:  # type: ignore[attr-defined]
        a.lan_address, a.lan_subnet = lan.address, lan.subnet  # type: ignore[attr-defined]
    if not (a.lan_address and a.lan_subnet and conv.yes(
        "lan_confirm.ask", True, address=a.lan_address, subnet=a.lan_subnet
    )):  # fmt: skip
        a.lan_address = conv.text("lan_address.ask", a.lan_address, ans.check_ipv4)
        while True:
            a.lan_subnet = conv.text("lan_subnet.ask", a.lan_subnet, ans.check_subnet)
            problem = ans.check_address_in_subnet(a.lan_address, a.lan_subnet)
            if problem is None:
                break
            conv.say(conv.tr(problem))

    # --- Jellyfin, alerts, the home prefix (all optional)
    conv.why("jellyfin_url.why")
    a.jellyfin_url = conv.text(
        "jellyfin_url.ask", a.jellyfin_url, ans.check_http_url, optional=True, clear_with_dash=True
    ).rstrip("/")
    if a.jellyfin_url:
        conv.why("jellyfin_key.why")
        a.jellyfin_key = conv.secret(
            "jellyfin_key.ask", ans.check_jellyfin_key, a.jellyfin_key, optional=True
        )
    else:
        a.jellyfin_key = ""
    conv.why("alert_url.why")
    a.alert_url = conv.secret("alert_url.ask", ans.check_http_url, a.alert_url, optional=True)
    conv.why("home_prefix.why")
    a.home_prefix = ans.normalise_prefixes(
        conv.text(
            "home_prefix.ask", a.home_prefix, ans.check_prefixes, optional=True,
            clear_with_dash=True,
        )
    )  # fmt: skip

    # --- the time zone
    if not (timezone_known(a.timezone) and conv.yes("timezone.ask", True, zone=a.timezone)):
        a.timezone = conv.text(
            "timezone_name.ask", a.timezone, lambda v: ans.check_timezone(v, timezone_known(v))
        )
    return a


def _complete(dirs: dict[str, str]) -> bool:
    return all(dirs.values())


def show_summary(conv: Conversation, a: Answers) -> None:
    conv.say("")
    conv.say(conv.tr("summary.title"))
    secret = conv.tr("summary.secret")
    for key, value in a.masked().items():
        if value == "(set)":
            value = secret
        if value or key in ans.LIBRARY_DEFAULTS:
            conv.say(f"  {key} = {value}")
    conv.say("  " + conv.tr("summary.web", user=a.web_user))


# ------------------------------------------------------------------------------ answers file


def read_answers_file(path: Path, defaults: Answers) -> tuple[Answers, list[str]]:
    """Answers for a run without questions (for tests and automation). Lines `key=value`; the
    keys are the field names of `Answers` plus the `MEDIA_DIR_*` folder names. The file holds
    passwords, so it must be private (mode 600) and is never printed.
    Returns (answers, problems)."""
    problems: list[str] = []
    mode = path.stat().st_mode & 0o077
    if mode:
        problems.append("the answers file must not be readable by others (chmod 600)")
    a = Answers(**{**vars(defaults)})
    values = parse_env(path.read_text(encoding="utf-8"))
    for key, value in values.items():
        if key in ans.LIBRARY_DEFAULTS:
            a.dirs[key] = value
        elif key in ans.ANSWER_FIELDS and key != "dirs":
            current = getattr(a, key)
            setattr(a, key, int(value) if isinstance(current, int) else value)
        else:
            problems.append(f"unknown answer: {key}")
    problems += check_answers(a)
    return a, problems


def check_answers(a: Answers) -> list[str]:
    """Problems with a complete set of answers (the same rules the questions use)."""
    problems: list[str] = []

    def need(name: str, problem: str | None) -> None:
        if problem is not None:
            problems.append(f"{name}: {problem}")

    need("data_root", ans.check_data_root(a.data_root))
    need("dirs", ans.check_folder_names(a.dirs))
    need("web_user", ans.check_user(a.web_user))
    need("web_password", ans.check_web_password(a.web_password) if a.web_password else "err.empty")
    need("lan_address", ans.check_ipv4(a.lan_address))
    need("lan_subnet", ans.check_subnet(a.lan_subnet))
    if not problems:
        need("lan_address", ans.check_address_in_subnet(a.lan_address, a.lan_subnet))
    if a.vpn_kind == "ovpn":
        need("vpn_user", ans.check_vpn_user(a.vpn_user))
        need("vpn_password", ans.check_vpn_secret(a.vpn_password))
        if a.ovpn_source:
            need("ovpn_source", looks_like_ovpn(Path(a.ovpn_source)))
    elif a.vpn_kind == "builtin":
        need("vpn_provider", ans.check_provider(a.vpn_provider))
        if a.vpn_type == "wireguard":
            need("wireguard_key", ans.check_vpn_secret(a.wireguard_key))
            need("wireguard_addresses", ans.check_wireguard_addresses(a.wireguard_addresses))
        else:
            need("vpn_user", ans.check_vpn_user(a.vpn_user))
            need("vpn_password", ans.check_vpn_secret(a.vpn_password))
    else:
        problems.append("vpn_kind: must be ovpn or builtin")
    if a.qbit_password:
        need("qbit_password", ans.check_qbit_password(a.qbit_password))
    if a.jellyfin_url:
        need("jellyfin_url", ans.check_http_url(a.jellyfin_url))
        if a.jellyfin_key:
            need("jellyfin_key", ans.check_jellyfin_key(a.jellyfin_key))
    if a.alert_url:
        need("alert_url", ans.check_http_url(a.alert_url))
    if a.home_prefix:
        need("home_prefix", ans.check_prefixes(a.home_prefix))
    need("timezone", ans.check_timezone(a.timezone, timezone_known(a.timezone)))
    return problems


# ------------------------------------------------------------------------------ the main flow


def proxy_url(a: Answers) -> str:
    return "http://" + a.lan_address


def finish(conv: Conversation, a: Answers, env: dict[str, str], restart_hint: bool) -> None:
    conv.say("")
    conv.say(conv.tr("done.title"))
    port = env.get("PROXY_PORT", "80")
    base = proxy_url(a) + ("" if port == "80" else f":{port}")
    conv.say(conv.tr("done.open", url=base + "/", user=a.web_user))
    qbt_port = env.get("PROXY_QBT_PORT", "8081")
    conv.say(conv.tr("done.qbit", url=f"{proxy_url(a)}:{qbt_port}/"))
    conv.say(conv.tr("done.backups"))
    if a.jellyfin_url:
        conv.say(conv.tr("done.jellyfin", example=f"/media/jellyfin/{a.dirs['MEDIA_DIR_MOVIES']}"))
    conv.say(conv.tr("done.help"))
    if restart_hint:
        conv.say(conv.tr("done.relogin"))


def show_state(conv: Conversation, runner: Runner, state: State) -> None:
    conv.say("")
    conv.say(conv.tr("state.title"))
    for step in STEPS:
        mark = "✓" if step.id in state.completed else "·"
        conv.say(f"  {mark} {conv.tr(f'step.{step.id}.title')}")
    result = runner.run(["docker", "compose", "ps", "--all", "--format", "json"], readonly=True)
    states = parse_compose_ps(result.stdout) if result.ok else {}
    bad = unhealthy_services(states)
    for service in SERVICES:
        shown = states.get(service, ("missing", ""))
        conv.say(
            f"  {service}: {shown[0]} {shown[1]}".rstrip() + ("" if service not in bad else "  <-")
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.sh",
        description="Install or maintain youtube-premium (asks questions in plain language).",
    )
    parser.add_argument("--lang", choices=["de", "en"], help="language (otherwise asked)")
    parser.add_argument(
        "--answers", type=Path, help="read the answers from a private file; no questions"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be done, change nothing"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--update", action="store_true", help="back up, fetch the new version, rebuild, check"
    )
    mode.add_argument("--repair", action="store_true", help="run every step again and check")
    mode.add_argument("--status", action="store_true", help="show what is installed and running")
    return parser.parse_args(argv)


def main(argv: list[str], root: Path, deps: Deps | None = None) -> int:
    from .update import run_update

    deps = deps or Deps()
    args = parse_args(argv)
    term = deps.term
    log_path = root / "config" / "installer.log"
    clock = deps.now
    stamp = lambda: clock().isoformat(timespec="seconds")  # noqa: E731
    runner = deps.runner or Runner(
        root,
        dry_run=args.dry_run,
        say=term.say,
        log=(lambda line: None) if args.dry_run else file_logger(log_path, stamp),
    )
    state = State.load(root / "config" / "installer-state.json")
    lang = args.lang or state.language or ""

    try:
        # --- the system
        try:
            os_text = deps.os_release.read_text(encoding="utf-8")
        except OSError:
            os_text = ""
        if not is_debian_12(os_text):
            found = (
                os_text
                and next(
                    (
                        ln.split("=", 1)[1].strip('"')
                        for ln in os_text.splitlines()
                        if ln.startswith("PRETTY_NAME=")
                    ),
                    "?",
                )
            ) or "?"
            term.say(t("de", "not_debian", found=found))
            term.say(t("en", "not_debian", found=found))
            return 1
        if deps.require_root and not args.dry_run and os.geteuid() != 0:
            term.say("Bitte mit sudo starten / please run with sudo:  sudo ./install.sh")
            return 1

        # --- language
        if lang not in ("de", "en"):
            while True:
                answer = term.ask(t("de", "lang.ask")).lower()
                if answer[:1] in ("d", "g"):
                    lang = "de"
                    break
                if answer[:1] == "e":
                    lang = "en"
                    break
        conv = Conversation(term, lang)
        state.language = lang
        term.say("")
        term.say(conv.tr("title"))
        if args.dry_run:
            term.say(conv.tr("dry_run"))

        login = deps.login or detect_login_user(root)
        env_exists = (root / ".env").exists()
        env = parse_env((root / ".env").read_text(encoding="utf-8")) if env_exists else {}
        open_step = state.first_open() if state.exists else None
        interrupted = (
            env_exists and state.exists and open_step is not None and "config" in state.completed
        )

        def context(answers: Answers, previous: Answers | None = None) -> Context:
            runner.register_secrets(
                [answers.vpn_password, answers.wireguard_key, answers.qbit_password,
                 answers.jellyfin_key, answers.alert_url, answers.web_password]
            )  # fmt: skip
            ctx = Context(
                root=root, answers=answers, runner=runner, say=term.say, lang=lang, login=login,
                now=clock, previous=previous, ask_secret=term.secret,
                confirm=None if args.answers else (lambda q: conv.yes(q, False)),
                http_get=deps.http_get, dry_run=args.dry_run,
            )  # fmt: skip
            if deps.sleep is not None:
                ctx.sleep = deps.sleep
            if deps.wait_seconds is not None:
                ctx.wait_seconds = deps.wait_seconds
            return ctx

        if args.status:
            show_state(conv, runner, state)
            return 0
        if args.update:
            if not env_exists:
                term.say(conv.tr("update.no_install"))
                return 1
            return run_update(context(Answers.from_env(env)), state, conv)

        # --- which flow
        mode = "new"
        if args.repair:
            mode = "repair"
        elif interrupted and not args.answers:
            mode = "resume"
        elif env_exists and not args.answers:
            conv.say("")
            conv.say(conv.tr("menu.found"))
            choice = conv.choice("menu.options", ["1", "2", "3", "4", "5"], "5")
            mode = {"1": "change", "2": "update", "3": "repair", "4": "status", "5": "quit"}[choice]
        if mode == "quit":
            return 0
        if mode == "status":
            show_state(conv, runner, state)
            return 0
        if mode == "update":
            return run_update(context(Answers.from_env(env)), state, conv)

        previous: Answers | None = None
        if mode == "new":
            if args.answers:
                defaults = Answers(language=lang, timezone=deps.timezone or detect_timezone())
                lan = deps.detect(runner)
                if lan is not None:
                    defaults.lan_address, defaults.lan_subnet = lan.address, lan.subnet  # type: ignore[attr-defined]
                answers_, problems = read_answers_file(args.answers, defaults)
                if not answers_.qbit_password:
                    answers_.qbit_password = deps.token()
                if problems:
                    for problem in problems:
                        term.say(f"  {problem}")
                    return 2
            else:
                term.say("")
                term.say(conv.tr("intro"))
                answers_ = collect(conv, deps, runner, root, login, None)
                show_summary(conv, answers_)
                if not conv.yes("summary.confirm", True):
                    term.say(conv.tr("interrupted"))
                    return 0
            state.completed.clear()
        elif mode == "change":
            previous = Answers.from_env(env)
            answers_ = collect(conv, deps, runner, root, login, previous)
            show_summary(conv, answers_)
            if not conv.yes("summary.confirm", True):
                term.say(conv.tr("interrupted"))
                return 0
            state.forget([s for s in STEP_IDS if s != "prepare"], args.dry_run)
        else:  # resume and repair work from what .env says
            answers_ = Answers.from_env(env)
            answers_.language = lang
            if mode == "resume":
                term.say(conv.tr("menu.resume", step=conv.tr(f"step.{open_step}.title")))
            else:
                state.forget(STEP_IDS, args.dry_run)
            hash_open = env.get("PROXY_PASSWORD_HASH", "changeme") == "changeme"
            if hash_open or (mode == "resume" and "logins" not in state.completed):
                answers_.web_password = conv.secret(
                    "web_password.ask", ans.check_web_password, twice=True, n=ans.MIN_WEB_PASSWORD
                )
            if not answers_.qbit_password:
                answers_.qbit_password = deps.token()

        ctx = context(answers_, previous)
        state.save(args.dry_run)
        try:
            run_steps(ctx, state)
        except StepError as error:
            step_id = getattr(error, "step_id", "")
            term.say("")
            term.say(
                conv.tr("step.failed", title=conv.tr(f"step.{step_id}.title"))
                if step_id
                else conv.tr(error.hint)
            )
            term.say(conv.tr(error.hint))
            if error.detail and error.detail != error.hint:
                term.say("  " + error.detail.splitlines()[-1][:300])
            term.say(conv.tr("step.failed_more", log=str(log_path)))
            return 1
        if args.dry_run:
            term.say("")
            term.say(conv.tr("done.dry"))
            return 0
        finish(
            conv,
            answers_,
            parse_env((root / ".env").read_text(encoding="utf-8"))
            if (root / ".env").exists()
            else {},
            True,
        )
        return 0
    except (KeyboardInterrupt, EOFError):
        term.say("")
        term.say(t(lang if lang in ("de", "en") else "en", "interrupted"))
        return 130


def run_cli() -> int:  # pragma: no cover - the real entry point
    root = Path(__file__).resolve().parents[3]
    return main(sys.argv[1:], root)


__all__ = [
    "Conversation",
    "Deps",
    "Terminal",
    "main",
    "missing_settings",
    "run_cli",
    "step_config",
    "wait_healthy",
]
