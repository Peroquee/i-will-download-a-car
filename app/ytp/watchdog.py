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
import http.server
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .alerts import FORMATS, Alert, Alerter
from .clients.gluetun import GluetunAuthError, GluetunControl, GluetunUnreachable
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError
from .clients.watchdog import WatchdogStatusClient
from .egress import EgressChecker, parse_networks
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
    alert_url: str = ""
    alert_token: str = ""
    alert_format: str = "ntfy"
    status_port: int = 8090  # 0 = no status server
    egress_interval: float = 300.0  # seconds between egress checks; 0 = off
    egress_confirm: int = 2  # consecutive mismatches before acting
    home_public_ip: str = ""  # semi-secret: addresses/prefixes of the home line
    egress_expected_country: str = ""

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

        alert_url = text("ALERT_URL")
        alert_format = text("ALERT_FORMAT", "ntfy").lower()
        if alert_format not in FORMATS:
            problems.append(
                f"ALERT_FORMAT must be one of {', '.join(FORMATS)}, got {alert_format!r}"
            )
        if alert_url and urlsplit(alert_url).scheme not in {"http", "https"}:
            problems.append("ALERT_URL must be an http(s) address (its value is not shown)")
        egress_interval = number("EGRESS_CHECK_INTERVAL", 300.0, 0.0, 86400.0)
        if 0 < egress_interval < 30:
            problems.append("EGRESS_CHECK_INTERVAL must be 0 (off) or at least 30 seconds")
        home_ip = text("HOME_PUBLIC_IP")
        try:
            parse_networks(home_ip)
        except ValueError as exc:  # names the entry, never its value
            problems.append(f"HOME_PUBLIC_IP: {exc}")
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
            alert_url=alert_url,
            alert_token=text("ALERT_TOKEN"),
            alert_format=alert_format,
            status_port=int(number("WATCHDOG_STATUS_PORT", 8090, 0, 65535)),
            egress_interval=egress_interval,
            egress_confirm=int(number("EGRESS_CONFIRM", 2, 1, 10)),
            home_public_ip=home_ip,
            egress_expected_country=text("EGRESS_EXPECTED_COUNTRY"),
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
        alerter: Alerter | None = None,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        egress: EgressChecker | None = None,
    ):
        self.alerter = alerter
        self.egress = egress
        self.egress_state = "off" if egress is None else "unknown"  # off|unknown|ok|mismatch
        self.egress_country = ""
        self._egress_checked: datetime | None = None
        self._egress_bad = 0
        self._egress_paused = False
        self._wall = wall
        self._lock = threading.Lock()
        self._since = wall()
        self._reason = ""
        self._last_poll: datetime | None = None
        self._outage_started: datetime | None = None
        self._announce_down = False
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

    # ------------------------------------------------------------------ status and alerts

    def snapshot(self) -> dict[str, object]:
        """What the app shows as a badge: no secrets, no addresses."""
        with self._lock:
            if self._last_poll is None:
                state = "starting"
            elif self.misconfigured:
                state = "blind"
            else:
                state = "down" if self.state == "unhealthy" else "ok"
            return {
                "state": state,  # ok | down | blind (cannot read the VPN status) | starting
                "since": self._since.isoformat(timespec="seconds"),
                "reason": self._reason,
                "paused": self.paused_by_us,
                "last_poll": self._last_poll.isoformat(timespec="seconds")
                if self._last_poll
                else None,
                "auto_resume": self.config.resume_after > 0,
                "egress": self.egress_state,
                "egress_checked": (
                    self._egress_checked.isoformat(timespec="seconds")
                    if self._egress_checked
                    else None
                ),
                "egress_country": self.egress_country,
            }

    def _mark(self, reason: str = "") -> None:
        with self._lock:
            self._since = self._wall()
            self._reason = reason

    def _alert(self, kind: str, title: str, lines: list[str], *, urgent: bool) -> None:
        if self.alerter is None:
            return
        now = self._wall().isoformat(timespec="seconds")
        self.alerter.notify(
            Alert(kind, title, "\n".join([f"Time: {now}", *lines]), now, urgent=urgent)
        )

    # ------------------------------------------------------------------ one poll

    def step(self) -> None:
        """One poll of gluetun, and whatever follows from it."""
        now = self._clock()
        with self._lock:
            self._last_poll = self._wall()
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
            self._mark()
            self._alert(
                "sighted",
                "Watchdog can see the VPN again",
                ["The watchdog can read the VPN status again."],
                urgent=False,
            )

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
            first = self.misconfigured is None
            self._mark(message)
            if first:
                self._alert(
                    "blind",
                    "Watchdog cannot read the VPN status",
                    [
                        "The VPN watchdog is NOT protecting anything: gluetun's control server "
                        "refuses its key. Run: make watchdog-key",
                    ],
                    urgent=True,
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
            self._announce_down = True
            self._outage_started = self._wall()
            self._mark(reason)
        if self.state == "unhealthy" and not self.paused_by_us and now >= self._next_pause_attempt:
            self._pause(now)
        if self._announce_down:
            self._announce_down = False
            self._alert(
                "down",
                "VPN is down",
                [
                    f"Reason: {self._reason}",
                    "All torrents were paused."
                    if self.paused_by_us
                    else "Pausing the torrents is still being tried (qBittorrent is "
                    "unreachable while the VPN is down, so it cannot send anything either).",
                ],
                urgent=True,
            )

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
            self._back_to_healthy(outcome="nothing had been paused")
            return
        if self.config.resume_after <= 0:
            self._back_to_healthy(
                note="auto-resume is off: the torrents stay paused",
                outcome="the torrents stay paused: resume them in qBittorrent",
            )
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
        self._back_to_healthy(outcome=f"{len(hashes)} torrent(s) were resumed")

    def _back_to_healthy(self, note: str = "", outcome: str = "") -> None:
        started = self._outage_started
        self.state = "healthy"
        self.paused_by_us = False
        self._healthy_since = None
        self._remembered_at = None  # take a fresh look at what is running
        self._outage_started = None
        self._mark()
        if note:
            logger.info("watchdog healthy again: %s", note)
        lasted = f" after {int((self._wall() - started).total_seconds())} s" if started else ""
        self._alert(
            "recovered",
            "VPN is back",
            [f"The VPN is running again{lasted}.", f"Torrents: {outcome}."],
            urgent=False,
        )

    def _remember_running(self, now: float) -> None:
        """While all is well, note which torrents are running (cheap, every 30 s)."""
        if self._remembered_at is not None and now - self._remembered_at < RUNNING_REFRESH_SECONDS:
            return
        self._remembered_at = now
        # On a QbitError keep the previous list: qBittorrent may be mid-reconnect.
        with contextlib.suppress(QbitError):
            self._remembered = [t.hash for t in self.qbit.list_torrents() if not t.paused]

    # ------------------------------------------------------------------ the egress check (#23)

    def check_egress(self) -> None:
        """One slow egress sanity check and what follows. Runs on its own thread, every few minutes.

        Only while the VPN is up and the watchdog can see: a restarting tunnel is not a mismatch.
        `egress_confirm` mismatches in a row are needed (one odd answer is not a leak). A
        confirmed mismatch alerts loudly and pauses all torrents; they are never resumed
        automatically (that needs a person who has looked), also not when the next check is fine.
        """
        if self.egress is None or self.state != "healthy" or self.misconfigured:
            return
        result = self.egress.check()
        with self._lock:
            self._egress_checked = self._wall()
            if result.country:
                self.egress_country = result.country
        if result.status == "inconclusive":
            logger.warning("egress check inconclusive", extra={"why": "; ".join(result.problems)})
            return
        if result.status == "ok":
            self._egress_bad = 0
            if self.egress_state == "mismatch":
                logger.warning("egress check passes again (torrents stay paused)")
                self._alert(
                    "egress_ok",
                    "VPN egress check passes again",
                    [
                        "The egress address looks right again.",
                        "Torrents that were paused stay paused: check and resume them yourself.",
                    ],
                    urgent=False,
                )
            self.egress_state = "ok"
            return
        self._egress_bad += 1
        logger.error(
            "egress check found a problem",
            extra={
                "problems": result.problems,
                "egress": result.masked_ip,
                "organization": result.organization,
                "consecutive": self._egress_bad,
            },
        )
        if self._egress_bad < self.config.egress_confirm:
            return
        first = self.egress_state != "mismatch"
        self.egress_state = "mismatch"
        if not self._egress_paused:
            try:
                self.qbit.pause_all()
                self._egress_paused = True
            except QbitError as exc:
                logger.error(
                    "could not pause the torrents after an egress mismatch",
                    extra={"error": str(exc)},
                )
        if first:
            self._alert(
                "egress",
                "VPN egress check FAILED",
                [
                    *[f"- {problem}" for problem in result.problems],
                    "All torrents were paused. They stay paused until you resume them."
                    if self._egress_paused
                    else "Pausing the torrents did not work yet; it is retried at the next check.",
                ],
                urgent=True,
            )

    # ------------------------------------------------------------------ the loop

    def _egress_loop(self, stop: threading.Event, first_after: float) -> None:
        interval = self.config.egress_interval
        stop.wait(first_after)  # let the tunnel settle after a start
        while not stop.is_set():
            try:
                self.check_egress()
            except Exception:
                logger.exception("egress check failed")
            stop.wait(interval)

    def run(self, stop: threading.Event, heartbeat: Path | None = HEARTBEAT_FILE) -> None:
        if self.egress is not None and self.config.egress_interval > 0:
            threading.Thread(
                target=self._egress_loop,
                args=(stop, min(20.0, self.config.egress_interval)),
                name="egress-check",
                daemon=True,
            ).start()
        while not stop.is_set():
            started = self._clock()
            try:
                self.step()
            except Exception:  # the watchdog must outlive any bug in one poll
                logger.exception("watchdog poll failed")
            if heartbeat is not None:
                write_heartbeat(heartbeat, self.misconfigured)
            stop.wait(max(0.0, self.config.interval - (self._clock() - started)))


class StatusServer:
    """A tiny read-only status page for the app's badge: `GET /status` returns `snapshot()`.

    It listens inside the container only (no published port); the app reaches it as
    `http://watchdog:8090/status` over the stack's bridge network. The answer holds no secrets
    and no addresses, so it needs no login. Anything but GET /status is refused.
    """

    def __init__(self, watchdog: Watchdog, port: int):
        handler_watchdog = watchdog

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # quiet: the app polls it often
                pass

            def _reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path != "/status":
                    return self._reply(404, b'{"detail":"not found"}')
                payload = json.dumps(handler_watchdog.snapshot()).encode()
                self._reply(200, payload)

            def _refuse(self) -> None:
                self._reply(405, b'{"detail":"read-only"}')

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

        self._server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


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


def probe(what: str, argument: str = "", env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Helpers for `scripts/vpn/vpn-abuse-test.sh`, run inside the watchdog container.

    They print plain words (no JSON, no names, no addresses) so the host script can read them:

    * `state`: the watchdog's own state (`ok`, `down`, `blind`, `starting`) from its status page;
    * `counts`: `running=N paused=M` torrents, from qBittorrent;
    * `hashes`: the infohashes of the running torrents, comma separated;
    * `resume HASHES`: resume exactly those torrents (comma separated), prints `ok`.

    Returns (exit code, text); exit 1 and `unreachable` when the thing cannot be asked.
    """
    values = os.environ if env is None else env
    try:
        config = WatchdogConfig.from_env(values)
    except ConfigError:
        return 2, "invalid configuration"
    if what == "state":
        port = config.status_port or 8090
        status = WatchdogStatusClient(f"http://127.0.0.1:{port}").fetch_status()
        return (0, str(status["state"])) if status["state"] != "unknown" else (1, "unreachable")
    qbit = QbitClient(config.qbit_url, config.qbit_user, config.qbit_password, timeout=5.0)
    try:
        torrents = qbit.list_torrents()
        running = [t.hash for t in torrents if not t.paused]
        if what == "counts":
            return 0, f"running={len(running)} paused={len(torrents) - len(running)}"
        if what == "hashes":
            return 0, ",".join(running)
        if what == "resume":
            hashes = [h for h in argument.split(",") if h]
            if hashes:
                qbit.resume_torrents(hashes)
            return 0, "ok"
    except QbitError:
        return 1, "unreachable"
    except ValueError as exc:
        return 2, str(exc)
    return 2, f"unknown probe {what!r}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        ok, message = check()
        print(message)
        return 0 if ok else 1
    if args and args[0] == "--probe":
        code, text = probe(args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else "")
        print(text)
        return code
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        config = WatchdogConfig.from_env()
    except ConfigError as exc:
        logger.error("invalid configuration", extra={"problems": str(exc).splitlines()[1:]})
        return 2
    alerter = (
        Alerter(config.alert_url, config.alert_token, config.alert_format)
        if config.alert_url
        else None
    )
    control = GluetunControl(config.control_url, config.api_key)
    qbit = QbitClient(config.qbit_url, config.qbit_user, config.qbit_password, timeout=5.0)
    egress = (
        EgressChecker(
            control,
            qbit,
            home_networks=parse_networks(config.home_public_ip),
            expected_countries=tuple(
                c.strip() for c in config.egress_expected_country.split(",") if c.strip()
            ),
        )
        if config.egress_interval > 0
        else None
    )
    watchdog = Watchdog(control, qbit, config, alerter=alerter, egress=egress)
    status_server = StatusServer(watchdog, config.status_port) if config.status_port else None
    if status_server:
        status_server.start()
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
            "alerts": alerter.host if alerter else "off",
            "egress_check_every": config.egress_interval or "off",
            "home_networks_configured": bool(config.home_public_ip),
            "expected_country": config.egress_expected_country or None,
            "status_port": config.status_port or "off",
        },
    )
    watchdog.run(stop)
    if status_server:
        status_server.stop()
    if alerter:
        alerter.close()
    logger.info("watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
