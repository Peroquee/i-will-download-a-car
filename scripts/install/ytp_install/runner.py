"""Running commands for the installer.

One place runs every command, so three promises can be kept (and tested) in one place:

* **A secret is never on a command line.** Passwords go in through `input_text` (stdin) only.
  Secrets the installer knows are registered, and a command that contains one anywhere in its
  arguments is refused before it runs (a bug, not a user error).
* **`--dry-run` changes nothing.** Commands that only look (`readonly=True`) still run; all others
  are printed with a `+` and not executed.
* **Everything is logged** (the command, never its input, and the result) to `config/installer.log`
  with mode 600, so that "what happened?" has an answer when something fails.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

MIN_SECRET_LENGTH = 6


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SecretOnCommandLine(AssertionError):
    pass


class Runner:
    def __init__(
        self,
        root: Path,
        *,
        dry_run: bool = False,
        say: Callable[[str], None] = print,
        log: Callable[[str], None] | None = None,
    ):
        self.root = root
        self.dry_run = dry_run
        self._say = say
        self._log = log or (lambda line: None)
        self._secrets: set[str] = set()

    def register_secrets(self, values: Iterable[str]) -> None:
        self._secrets |= {v for v in values if len(v) >= MIN_SECRET_LENGTH}

    def _guard(self, argv: list[str]) -> None:
        for part in argv:
            for secret in self._secrets:
                if secret in part:
                    raise SecretOnCommandLine("a secret would have been put on a command line")

    def run(
        self,
        argv: list[str],
        *,
        input_text: str | None = None,
        readonly: bool = False,
        stream: bool = False,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> Result:
        """Run a command. `stream=True` lets its output appear on the terminal as it happens
        (a long build); otherwise the output is captured (and only its tail is logged)."""
        self._guard(argv)
        shown = shlex.join(argv)
        if self.dry_run and not readonly:
            self._say(f"+ {shown}" + ("   (reads the password from stdin)" if input_text else ""))
            self._log(f"dry-run: {shown}")
            return Result(0)
        result = self._execute(argv, input_text, stream, timeout, cwd or self.root)
        self._log(f"$ {shown} -> {result.returncode}")
        if result.returncode != 0 and (result.stdout or result.stderr):
            tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
            self._log("  " + "\n  ".join(tail))
        return result

    def _execute(
        self,
        argv: list[str],
        input_text: str | None,
        stream: bool,
        timeout: float | None,
        cwd: Path,
    ) -> Result:
        try:
            completed = subprocess.run(
                argv,
                input=input_text,
                text=True,
                capture_output=not stream,
                cwd=cwd,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return Result(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", "timed out")
        return Result(completed.returncode, completed.stdout or "", completed.stderr or "")


def file_logger(path: Path, now: Callable[[], str]) -> Callable[[str], None]:
    """Append lines to the installer log, creating it with mode 600."""

    def write(line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{now()} {line}\n")

    return write
