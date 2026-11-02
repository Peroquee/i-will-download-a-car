"""Test doubles for the installer (issue #37): a scripted terminal and a runner that records.

`sys.path` gets `scripts/install` so `ytp_install` imports like the installer does.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "install"))

from ytp_install import cli  # noqa: E402
from ytp_install.runner import Result, Runner  # noqa: E402
from ytp_install.system import Lan, LoginUser  # noqa: E402

NOW = datetime(2026, 11, 2, 9, 30, 0)
ADMIN_HASH = "scrypt:32768:8:1:c2FsdHNhbHRzYWx0:a2V5a2V5a2V5a2V5"  # the shape, not a real hash
DEBIAN = 'ID=debian\nVERSION_CODENAME=bookworm\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
SECRETS = {
    "vpn_password": "vpn-vpn-vpn-1",
    "web_password": "web-web-web-2x",
    "qbit": "qbit-qbit-qbit-3",
    "jellyfin_key": "abababababababababababababababab",
    "alert_url": "http://ntfy.lan/nnnn-4",
}


class FakeTerminal(cli.Terminal):
    """Answers prompts from `rules`: (text found in the prompt, answer or list of answers).

    An empty answer means "take the default", like pressing Enter. Everything shown and every
    prompt is kept in `transcript`; hidden answers are kept apart in `typed_secrets`.
    """

    def __init__(self, rules: list[tuple[str, str | list[str]]] | None = None):
        self.rules = [
            (needle, list(a) if isinstance(a, list) else [a]) for needle, a in (rules or [])
        ]
        self.transcript: list[str] = []
        self.typed_secrets: list[str] = []
        self.unanswered: list[str] = []

    def say(self, text: str = "") -> None:
        self.transcript.append(text)

    def _answer(self, prompt: str) -> str:
        self.transcript.append(f"? {prompt}")
        if len(self.transcript) > 2000:  # a question asked forever: the input has ended
            raise EOFError
        for needle, answers in self.rules:
            if needle in prompt:
                return answers.pop(0) if len(answers) > 1 else answers[0]
        self.unanswered.append(prompt)
        return ""

    def ask(self, prompt: str, default: str = "") -> str:
        return self._answer(prompt) or default

    def secret(self, prompt: str) -> str:
        answer = self._answer(prompt)
        self.typed_secrets.append(answer)
        return answer

    @property
    def text(self) -> str:
        return "\n".join(self.transcript)


class FakeRunner(Runner):
    """Records every command; `rules` decides the result: (text in the command, Result)."""

    def __init__(self, root: Path, *, dry_run: bool = False):
        self.lines: list[str] = []
        super().__init__(root, dry_run=dry_run, say=self.lines.append, log=self.lines.append)
        self.commands: list[list[str]] = []
        self.inputs: list[tuple[list[str], str]] = []
        self.rules: list[tuple[str, Result]] = []
        self.compose_ps: list[str] = []  # successive outputs of `docker compose ps`

    def fail(self, needle: str, code: int = 1, stderr: str = "boom") -> None:
        self.rules.append((needle, Result(code, "", stderr)))

    def _execute(self, argv, input_text, stream, timeout, cwd):
        self.commands.append(list(argv))
        if input_text is not None:
            self.inputs.append((list(argv), input_text))
        joined = " ".join(argv)
        for needle, result in self.rules:
            if needle in joined:
                return result
        if "seed_config.py" in joined:  # the real script writes the login into qBittorrent's config
            conf = self.root / "config" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text('[Preferences]\nWebUI\\Password_PBKDF2="@ByteArray(x:y)"\n')
        if "set_password.py" in joined:  # the real script stores a hash in .env
            env = self.root / ".env"
            env.write_text(
                env.read_text().replace(
                    "PROXY_PASSWORD_HASH=changeme", "PROXY_PASSWORD_HASH='$2a$14$hashed'"
                )
            )
        if "set_admin.py" in joined:  # the real script stores a scrypt hash and the name in .env
            env = self.root / ".env"
            user = argv[argv.index("--user") + 1]
            text = env.read_text()
            for key, value in (("APP_ADMIN_USER", user), ("APP_ADMIN_PASSWORD_HASH", ADMIN_HASH)):
                lines = [ln for ln in text.splitlines() if not ln.startswith(key + "=")]
                text = "\n".join([*lines, f"{key}={value}"]) + "\n"
            env.write_text(text)
        if argv[:3] == ["docker", "compose", "ps"]:
            out = (
                self.compose_ps.pop(0)
                if len(self.compose_ps) > 1
                else (self.compose_ps or [healthy_ps()])[0]
            )
            return Result(0, out)
        if argv[:2] == ["ip", "-4"]:
            return Result(1)
        return Result(0, "")

    def ran(self, needle: str) -> list[list[str]]:
        return [c for c in self.commands if needle in " ".join(c)]

    def index(self, needle: str) -> int:
        for i, command in enumerate(self.commands):
            if needle in " ".join(command):
                return i
        raise AssertionError(f"{needle!r} was never run: {[' '.join(c) for c in self.commands]}")


def healthy_ps() -> str:
    import json

    return "\n".join(
        json.dumps({"Service": s, "State": "running", "Health": "healthy"})
        for s in ("gluetun", "qbittorrent", "app", "proxy", "watchdog")
    )


def make_repo(tmp_path: Path) -> Path:
    """A project folder with what the installer reads: the template and (empty) script paths."""
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy(ROOT / ".env.example", root / ".env.example")
    return root


def deps(term: FakeTerminal, runner: FakeRunner, tmp_path: Path, **overrides):
    os_release = tmp_path / "os-release"
    os_release.write_text(DEBIAN)
    values = dict(
        term=term,
        runner=runner,
        now=lambda: NOW,
        sleep=lambda s: None,
        login=LoginUser("pat", 1000, 1000),
        http_get=lambda url, timeout: 200,
        token=lambda: SECRETS["qbit"],
        os_release=os_release,
        require_root=False,
        timezone="Europe/Berlin",
        detect=lambda runner: Lan("192.168.178.20", "192.168.178.0/24"),
        wait_seconds=30,
    )
    values.update(overrides)
    return cli.Deps(**values)


def new_install_rules(ovpn: Path) -> list[tuple[str, str | list[str]]]:
    """The answers of a typical beginner (English), taking every default."""
    return [
        ("Sprache / Language", "e"),
        ("How do you get your details", "1"),
        ("Path of the .ovpn file", str(ovpn)),
        ("VPN user name", "vpn-user"),
        ("VPN password", SECRETS["vpn_password"]),
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
        ("Address of your Jellyfin", "http://192.168.178.108:8096"),
        ("Jellyfin API key", SECRETS["jellyfin_key"]),
        ("Address for alerts", SECRETS["alert_url"]),
    ]
