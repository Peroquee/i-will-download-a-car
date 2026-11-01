"""The VPN watchdog (issue #21): a second layer that pauses the torrents when the VPN drops.

The first layer is gluetun's firewall, which blocks everything outside the tunnel. This is a
small separate process (its own container, `python -m ytp.watchdog`, same image as the app) so it
keeps working when the app is wedged. Every second it asks gluetun's control server whether the
VPN is `running`. After `threshold` failures in a row it tells qBittorrent to pause the torrents
at once; it does not wait for the app and does not need the database.

    healthy ── threshold failures ──► unhealthy (pause_all)
       ▲                                  │
       └── VPN ok again ──────────────────┘   optional: resume after `resume_after` s stable

* **A blip does not thrash.** One or two failed polls change nothing.
* **Only what was running is resumed.** While healthy it remembers (every 30 s) which torrents
  are not paused; auto-resume starts exactly those, so a torrent you paused by hand stays
  paused. Auto-resume is off by default (`WATCHDOG_RESUME_AFTER=0`): after an outage the
  torrents wait for you.
* **If pausing cannot be delivered** (qBittorrent lives in gluetun's network namespace and is
  unreachable whenever gluetun is down, which also means it cannot send anything) the pause is
  retried until it works or the VPN is back.
* **A watchdog that cannot see is not evidence of an outage.** A 401/403/404 from the control
  server means the watchdog is set up wrong; it says so loudly, reports itself unhealthy to
  Docker, and does NOT pause (the firewall is still the real protection).
* Only transitions are logged, not every poll.

Environment: GLUETUN_CONTROL_URL (default http://gluetun:8000), GLUETUN_CONTROL_APIKEY,
QBIT_URL / QBIT_USER / QBIT_PASSWORD, WATCHDOG_INTERVAL (s, default 1), WATCHDOG_THRESHOLD
(default 3), WATCHDOG_RESUME_AFTER (s, default 0 = never). `--check` is the Docker health check.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .clients.gluetun import GluetunAuthError, GluetunControl, GluetunUnreachable
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError
from .logging_setup import configure_logging

logger = logging.getLogger("ytp.watchdog")

HEARTBEAT_FILE = Path(os.environ.get("WATCHDOG_HEARTBEAT_FILE", "/tmp/watchdog.alive"))
RUNNING_REFRESH_SECONDS = 30.0
PAUSE_RETRY_SECONDS = 5.0


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class WatchdogConfig:
    control_url: str = "http://gluetun:8000"
    api_key: str = ""
    qbit_url: str = "http://gluetun:8080"
    qbit_user: str = "admin"
    qbit_password: str = ""
    interval: float = 1.0
    threshold: int = 3
    resume_after: float = 0.0

    def __repr__(self) -> str:  # never show the secrets
        return f"WatchdogConfig(control_url={self.control_url!r}, interval={self.interval})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WatchdogConfig:
        values = os.environ if env is None else env
        problems: list[str] = []

        def text(name: str, default: str = "") -> str:
            return values.get(name, "").strip() or default

        def number(name: str, default: float, low: float, high: float) -> float:
            raw = text(name, str(default))
            try:
                value = float(raw)
            except ValueError:
                problems.append(f"{name} must be a number, got {raw!r}")
                return default
            if not low <= value <= high:
                problems.append(f"{name} must be between {low} and {high}, got {raw}")
            return value

        api_key = text("GLUETUN_CONTROL_APIKEY")
        password = text("QBIT_PASSWORD")
        if not api_key:
            problems.append("GLUETUN_CONTROL_APIKEY must be set (make watchdog-key)")
        if not password:
            problems.append(
                "QBIT_PASSWORD must be set: without it the watchdog cannot pause anything"
            )
        config = cls(
            control_url=text("GLUETUN_CONTROL_URL", "http://gluetun:8000").rstrip("/"),
            api_key=api_key,
            qbit_url=text("QBIT_URL", "http://gluetun:8080").rstrip("/"),
            qbit_user=text("QBIT_USER", "admin"),
            qbit_password=password,
            interval=number("WATCHDOG_INTERVAL", 1.0, 0.2, 60.0),
            threshold=int(number("WATCHDOG_THRESHOLD", 3, 1, 60)),
            resume_after=number("WATCHDOG_RESUME_AFTER", 0.0, 0.0, 86400.0),
        )
        if problems:
            raise ConfigError("invalid configuration:\n  " + "\n  ".join(problems))
        return config


class Watchdog:
    def __init__(
        self,
        control: GluetunControl,
        qbit: QbitClient,
        config: WatchdogConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.control = control
        self.qbit = qbit
        self.config = config
        self._clock = clock
        self.state = "healthy"  # or "unhealthy"
        self.misconfigured: str | None = None
        self.failures = 0
        self.paused_by_us = False  # a pause was delivered during the current outage
        self._remembered: list[str] = []  # hashes that were running (not paused) while healthy
        self._remembered_at: float | None = None
        self._next_pause_attempt = 0.0
        self._pause_failure_logged = False
        self._healthy_since: float | None = None
        self._next_resume_attempt = 0.0
        self.pauses = 0
        self.resumes = 0

    # ------------------------------------------------------------------ one poll

    def step(self) -> None:
        """One poll of gluetun, and whatever follows from it."""
        now = self._clock()
        try:
            ok = self.control.vpn_status() == "running"
            reason = "" if ok else "the VPN is not running"
        except GluetunAuthError as exc:
            self._set_misconfigured(str(exc))
            return
        except GluetunUnreachable as exc:
            ok, reason = False, str(exc)
        if self.misconfigured:
            logger.info("watchdog: the gluetun control server accepts the key now")
            self.misconfigured = None

        if ok:
            self._on_ok(now)
        else:
            self._on_failure(now, reason)

    def _set_misconfigured(self, message: str) -> None:
        if self.misconfigured != message:
            logger.error(
                "watchdog cannot read the VPN status and is NOT protecting anything",
                extra={"error": message},
            )
        self.misconfigured = message
        self.failures = 0

    def _on_failure(self, now: float, reason: str) -> None:
        self.failures += 1
        self._healthy_since = None
        if self.state == "healthy" and self.failures >= self.config.threshold:
            self.state = "unhealthy"
            logger.error(
                "VPN unhealthy: pausing all torrents",
                extra={"failures": self.failures, "reason": reason},
            )
            self.paused_by_us = False
            self._pause_failure_logged = False
            self._next_pause_attempt = 0.0
        if self.state == "unhealthy" and not self.paused_by_us and now >= self._next_pause_attempt:
            self._pause(now)

    def _pause(self, now: float) -> None:
        try:
            self.qbit.pause_all()
        except QbitAuthError as exc:
            self._pause_failed(now, f"qBittorrent login refused: {exc}", level=logging.ERROR)
        except QbitError as exc:
            # Normal while gluetun is down: qBittorrent shares its network namespace.
            self._pause_failed(now, str(exc), level=logging.WARNING)
        else:
            self.paused_by_us = True
            self.pauses += 1
            logger.warning("all torrents paused", extra={"will_resume": sorted(self._remembered)})

    def _pause_failed(self, now: float, message: str, *, level: int) -> None:
        self._next_pause_attempt = now + PAUSE_RETRY_SECONDS
        if not self._pause_failure_logged:  # once per outage; a second per second would drown it
            logger.log(
                level, "could not pause the torrents yet, retrying", extra={"error": message}
            )
            self._pause_failure_logged = True

    def _on_ok(self, now: float) -> None:
        self.failures = 0
        if self.state == "healthy":
            self._remember_running(now)
            return
        # unhealthy, VPN is back
        if self._healthy_since is None:
            self._healthy_since = now
            logger.info("VPN is running again", extra={"paused": self.paused_by_us})
        if not self.paused_by_us:
            # The pause never got delivered, but the VPN is fine again: nothing to undo.
            self._back_to_healthy()
            return
        if self.config.resume_after <= 0:
            self._back_to_healthy(note="auto-resume is off: the torrents stay paused")
            return
        if (
            now - self._healthy_since >= self.config.resume_after
            and now >= self._next_resume_attempt
        ):
            self._resume(now)

    def _resume(self, now: float) -> None:
        hashes = list(self._remembered)
        try:
            if hashes:
                self.qbit.resume_torrents(hashes)
        except QbitError as exc:
            self._next_resume_attempt = now + PAUSE_RETRY_SECONDS
            logger.warning("could not resume the torrents yet, retrying", extra={"error": str(exc)})
            return
        self.resumes += 1
        logger.warning(
            "VPN stable: torrents resumed",
            extra={"count": len(hashes), "stable_for": self.config.resume_after},
        )
        self._back_to_healthy()

    def _back_to_healthy(self, note: str = "") -> None:
        self.state = "healthy"
        self.paused_by_us = False
        self._healthy_since = None
        self._remembered_at = None  # take a fresh look at what is running
        if note:
            logger.info("watchdog healthy again: %s", note)

    def _remember_running(self, now: float) -> None:
        """While all is well, note which torrents are running (cheap, every 30 s)."""
        if self._remembered_at is not None and now - self._remembered_at < RUNNING_REFRESH_SECONDS:
            return
        self._remembered_at = now
        # On a QbitError keep the previous list: qBittorrent may be mid-reconnect.
        with contextlib.suppress(QbitError):
            self._remembered = [t.hash for t in self.qbit.list_torrents() if not t.paused]

    # ------------------------------------------------------------------ the loop

    def run(self, stop: threading.Event, heartbeat: Path | None = HEARTBEAT_FILE) -> None:
        while not stop.is_set():
            started = self._clock()
            try:
                self.step()
            except Exception:  # the watchdog must outlive any bug in one poll
                logger.exception("watchdog poll failed")
            if heartbeat is not None:
                write_heartbeat(heartbeat, self.misconfigured)
            stop.wait(max(0.0, self.config.interval - (self._clock() - started)))


def write_heartbeat(path: Path, misconfigured: str | None) -> None:
    # If this fails the health check notices the stale file.
    with contextlib.suppress(OSError):
        path.write_text("ok" if not misconfigured else f"misconfigured: {misconfigured}")


def check(path: Path | None = None, max_age: float = 10.0) -> tuple[bool, str]:
    """Docker's health check: alive recently, and able to see the VPN status."""
    path = path or HEARTBEAT_FILE
    try:
        age = time.time() - path.stat().st_mtime
        content = path.read_text()
    except OSError:
        return False, "no heartbeat yet"
    if age > max_age:
        return False, f"last poll {age:.0f} s ago"
    if content != "ok":
        return False, content
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        ok, message = check()
        print(message)
        return 0 if ok else 1
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        config = WatchdogConfig.from_env()
    except ConfigError as exc:
        logger.error("invalid configuration", extra={"problems": str(exc).splitlines()[1:]})
        return 2
    watchdog = Watchdog(
        GluetunControl(config.control_url, config.api_key),
        QbitClient(config.qbit_url, config.qbit_user, config.qbit_password, timeout=5.0),
        config,
    )
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    logger.info(
        "watchdog started",
        extra={
            "control_url": config.control_url,
            "interval": config.interval,
            "threshold": config.threshold,
            "resume_after": config.resume_after,
        },
    )
    watchdog.run(stop)
    logger.info("watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
