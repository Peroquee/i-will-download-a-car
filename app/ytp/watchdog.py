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

**"Running" is not "connected" (#81).** gluetun's `/v1/vpn/status` says `running` while its
OpenVPN process loops through failed handshakes, so on its own it said "up" for three hours of a
dead tunnel. The VPN counts as up only with evidence that the tunnel is connected, all of:

1. the status is `running`;
2. gluetun has a public address (`GET /v1/publicip/ip` answers one: empty without a tunnel);
3. a reachability probe through gluetun's own HTTP proxy gets out (`tunnelprobe.py`; catches a
   tunnel that dies after it was up, when the address is still the old one).

A missing status (stopped, crashed, unreachable) counts as before: `threshold` polls. A *tunnel*
failure (running, but 2 or 3 fail) first has to last `WATCHDOG_TUNNEL_GRACE` seconds (default 180):
gluetun reconnects on its own, and a normal reconnect (two to three minutes were seen) must not
alert or pause. After the grace it is the same outage as a stopped VPN: `threshold` more polls,
the same alert, pause and dashboard state, with the cause "tunnel" and its own reason text.

**Restart of a stuck VPN (opt-out).** If the tunnel stays down for `WATCHDOG_RESTART_AFTER`
seconds (default 900) while gluetun says `running`, the watchdog asks gluetun to stop and start the
VPN (`PUT /v1/vpn/status`), which makes it pick a new server; that is what cured the outage of
2026-11-10. At most `WATCHDOG_RESTART_MAX` times per outage with doubling spacing, one alert per
restart, never when the VPN was stopped by someone else. It needs the PUT route in the watchdog's
role (`make watchdog-key`); without it the watchdog says so once and goes on without restarting.

Environment: GLUETUN_CONTROL_URL (default http://gluetun:8000), GLUETUN_CONTROL_APIKEY,
QBIT_URL / QBIT_USER / QBIT_PASSWORD, WATCHDOG_INTERVAL (s, default 1), WATCHDOG_THRESHOLD
(default 3), WATCHDOG_RESUME_AFTER (s, default 0 = never), WATCHDOG_TUNNEL_GRACE (s, default
180), TUNNEL_PROBE_PROXY / TUNNEL_PROBE_TARGETS / TUNNEL_PROBE_INTERVAL / TUNNEL_PROBE_CONFIRM,
WATCHDOG_RESTART_AFTER (s, default 900, 0 = never) and WATCHDOG_RESTART_MAX (default 5).
`--check` is the Docker health check.
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
from .clients.gluetun import (
    GluetunAuthError,
    GluetunControl,
    GluetunError,
    GluetunNoAddress,
    GluetunUnreachable,
)
from .clients.qbittorrent import QbitAuthError, QbitClient, QbitError
from .clients.watchdog import WatchdogStatusClient
from .egress import EgressChecker, parse_networks
from .logging_setup import configure_logging
from .tunnelprobe import DEFAULT_TARGETS, TunnelProbe, parse_proxy, parse_targets

logger = logging.getLogger("ytp.watchdog")

HEARTBEAT_FILE = Path(os.environ.get("WATCHDOG_HEARTBEAT_FILE", "/tmp/watchdog.alive"))
RUNNING_REFRESH_SECONDS = 30.0
PAUSE_RETRY_SECONDS = 5.0
RESTART_BACKOFF_MAX = 6 * 3600.0  # the spacing between two restarts doubles up to this
RESTART_START_RETRY_SECONDS = 30.0  # the VPN stopped by us and not started again: try again
REASON_NO_ADDRESS = (
    "Tunnel not connected: gluetun reports the VPN as running but has no public address"
)
REASON_NO_TRAFFIC = (
    "Tunnel not connected: gluetun reports the VPN as running but nothing gets through the tunnel"
)


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
    tunnel_grace: float = (
        180.0  # seconds a "running but not connected" tunnel may take to come back
    )
    probe_proxy: str = "http://gluetun:8888"  # empty = no reachability probe
    probe_targets: str = DEFAULT_TARGETS
    probe_interval: float = 10.0  # seconds between probes; 0 = off
    probe_confirm: int = 2  # consecutive failed probes before the probe counts as bad
    restart_after: float = (
        900.0  # seconds of dead tunnel before asking gluetun to restart; 0 = never
    )
    restart_max: int = 5  # restarts per outage

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
        raw_proxy = values.get("TUNNEL_PROBE_PROXY")  # unset = default, set but empty = no probe
        probe_proxy = "http://gluetun:8888" if raw_proxy is None else raw_proxy.strip()
        if probe_proxy:
            try:
                parse_proxy(probe_proxy)
            except ValueError as exc:
                problems.append(f"TUNNEL_PROBE_PROXY {exc}")
        probe_targets = text("TUNNEL_PROBE_TARGETS", DEFAULT_TARGETS)
        try:
            parse_targets(probe_targets)
        except ValueError as exc:
            problems.append(f"TUNNEL_PROBE_TARGETS: {exc}")
        probe_interval = number("TUNNEL_PROBE_INTERVAL", 10.0, 0.0, 3600.0)
        if 0 < probe_interval < 5:
            problems.append("TUNNEL_PROBE_INTERVAL must be 0 (off) or at least 5 seconds")
        restart_after = number("WATCHDOG_RESTART_AFTER", 900.0, 0.0, 86400.0)
        if 0 < restart_after < 120:
            problems.append("WATCHDOG_RESTART_AFTER must be 0 (never) or at least 120 seconds")
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
            tunnel_grace=number("WATCHDOG_TUNNEL_GRACE", 180.0, 0.0, 3600.0),
            probe_proxy=probe_proxy,
            probe_targets=probe_targets,
            probe_interval=probe_interval,
            probe_confirm=int(number("TUNNEL_PROBE_CONFIRM", 2, 1, 10)),
            restart_after=restart_after,
            restart_max=int(number("WATCHDOG_RESTART_MAX", 5, 1, 50)),
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
        probe: TunnelProbe | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.alerter = alerter
        self.egress = egress
        self.probe = probe
        self._sleep = sleep
        # The tunnel evidence (#81). `probe_state`: off | unknown | ok | failing.
        self.probe_state = "off" if probe is None else "unknown"
        self._probe_failures = 0
        self.cause = ""  # why the VPN is down: stopped | unreachable | tunnel ("" while up)
        self.tunnel = "unknown"  # connected | reconnecting | down | unknown
        self._tunnel_bad_since: float | None = None  # monotonic, first poll with a bad tunnel
        self._tunnel_bad_wall: datetime | None = None
        self.ip_unavailable: str | None = None  # the key may not read the public address
        # Restart of a stuck VPN (#81).
        self.restarts = 0  # during the current outage
        self.restart_state = "off" if config.restart_after <= 0 else "armed"  # off|armed|denied
        self._restart_due: float | None = None
        self._restarting = False
        self._we_stopped = False  # we stopped the VPN to restart it and have not started it again
        self._next_start_attempt = 0.0
        self._gave_up = False
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
                # #81: why it is down, and whether the tunnel itself is connected
                "cause": self.cause,
                "tunnel": self.tunnel,
                "tunnel_since": (
                    self._tunnel_bad_wall.isoformat(timespec="seconds")
                    if self._tunnel_bad_wall
                    else None
                ),
                "probe": self.probe_state,
                "restart": self.restart_state,
                "restarts": self.restarts,
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
        soft = False  # a tunnel failure: the process runs, the tunnel does not work
        try:
            if self.control.vpn_status() != "running":
                ok, reason, cause = False, "the VPN is not running", "stopped"
            else:
                ok, reason, cause = self._tunnel_evidence()
                soft = not ok
        except GluetunAuthError as exc:
            self._set_misconfigured(str(exc))
            return
        except GluetunUnreachable as exc:
            ok, reason, cause = False, str(exc), "unreachable"
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
            if self._tunnel_bad_since is not None:
                logger.info(
                    "tunnel connected again",
                    extra={"after_s": round(now - self._tunnel_bad_since)},
                )
            self._tunnel_bad_since = None
            self._tunnel_bad_wall = None
            self.tunnel = "connected"
            self._on_ok(now)
            return
        if soft:
            if self._tunnel_bad_since is None:
                self._tunnel_bad_since = now
                self._tunnel_bad_wall = self._wall()
                logger.warning(
                    "tunnel not connected: waiting for gluetun to reconnect before counting it "
                    "as an outage",
                    extra={"reason": reason, "grace_s": self.config.tunnel_grace},
                )
            if self.state == "healthy" and now - self._tunnel_bad_since < self.config.tunnel_grace:
                self.failures = 0  # a reconnect is not an outage (yet)
                self.tunnel = "reconnecting"
                return
            self.tunnel = "down"
        else:
            # Stopped or unreachable: counted as always. During an outage the time the tunnel has
            # been dead is kept (one lost answer of the control server must not restart the
            # restart timer); before one, a hard failure starts the grace again.
            if self.state == "healthy":
                self._tunnel_bad_since = None
                self._tunnel_bad_wall = None
            self.tunnel = "unknown"
        self._on_failure(now, reason, cause)

    def _tunnel_evidence(self) -> tuple[bool, str, str]:
        """With the status `running`: is the tunnel connected? (ok, reason, cause)."""
        if self.ip_unavailable is None:
            try:
                self.control.public_ip()
            except GluetunNoAddress:
                return False, REASON_NO_ADDRESS, "tunnel"
            except GluetunAuthError as exc:
                # An older key without this route: the check is weaker, but the watchdog is not
                # blind (the status and the probe still work). Said once, loudly.
                self.ip_unavailable = str(exc)
                logger.error(
                    "the watchdog may not read gluetun's public address: the tunnel check is "
                    "weaker until you run make watchdog-key",
                    extra={"error": str(exc)},
                )
        if self.probe_state == "failing":
            return False, REASON_NO_TRAFFIC, "tunnel"
        return True, "", ""

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

    def _on_failure(self, now: float, reason: str, cause: str = "stopped") -> None:
        self.failures += 1
        self._healthy_since = None
        if self.state == "unhealthy" and (reason != self._reason or cause != self.cause):
            with self._lock:  # the reason changed during the outage (stopped, then reconnecting)
                self._reason = reason
            self.cause = cause
        if self.state == "healthy" and self.failures >= self.config.threshold:
            self.state = "unhealthy"
            self.cause = cause
            logger.error(
                "VPN unhealthy: pausing all torrents",
                extra={"failures": self.failures, "reason": reason, "cause": cause},
            )
            self.paused_by_us = False
            self._pause_failure_logged = False
            self._next_pause_attempt = 0.0
            self._announce_down = True
            self._outage_started = self._wall()
            self._mark(reason)
        if self.state == "unhealthy" and not self.paused_by_us and now >= self._next_pause_attempt:
            self._pause(now)
        self._maybe_restart(now)
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

    # ------------------------------------------------------------ restart of a stuck VPN (#81)

    def _maybe_restart(self, now: float) -> None:
        """Ask gluetun to restart the VPN once the tunnel has been dead for a long time.

        Only for a *tunnel* outage (gluetun says `running`): a VPN that is stopped was stopped by
        someone, and is not started by us, except the one we stopped ourselves a moment ago. Runs
        on its own thread, so a slow gluetun cannot hold up the polls (and the pause)."""
        config = self.config
        if self._restarting or (self.restart_state != "armed" and not self._we_stopped):
            return
        if self._we_stopped:  # a restart that stopped the VPN and could not start it again
            if now >= self._next_start_attempt:
                self._start_thread(now, finish_only=True)
            return
        if self.state != "unhealthy" or self.cause != "tunnel" or self._tunnel_bad_since is None:
            return
        if self.restarts >= config.restart_max:
            if not self._gave_up:
                self._gave_up = True
                logger.error("the watchdog restarted the VPN, the tunnel is still down: giving up")
                self._alert(
                    "restart_gave_up",
                    "VPN still down after restarts",
                    [
                        f"The watchdog restarted the VPN {self.restarts} times; the tunnel is "
                        "still not connected. It will not try again during this outage.",
                        "Look at the VPN: docker compose logs gluetun | grep -v 'http server'",
                    ],
                    urgent=True,
                )
            return
        if self._restart_due is None:
            self._restart_due = self._tunnel_bad_since + config.restart_after
        if now >= self._restart_due:
            self._start_thread(now, finish_only=False)

    def _start_thread(self, now: float, *, finish_only: bool) -> None:
        self._restarting = True
        threading.Thread(
            target=self._restart_vpn, args=(now, finish_only), name="vpn-restart", daemon=True
        ).start()

    def _restart_vpn(self, started: float, finish_only: bool) -> None:
        try:
            if not finish_only:
                # Set first: if the PUT is lost on the way gluetun may have stopped anyway, and
                # then the retry below starts it again (PUT "running" on a running VPN is harmless).
                self._we_stopped = True
                self.control.set_vpn_status("stopped")
                for _ in range(20):  # wait until gluetun says it stopped
                    self._sleep(1.0)
                    if self.control.vpn_status() == "stopped":
                        break
            for attempt in range(3):
                try:
                    self.control.set_vpn_status("running")
                    break
                except GluetunAuthError:
                    raise
                except GluetunError:
                    if attempt == 2:
                        raise
                    self._sleep(2.0)
            self._we_stopped = False
            self.restarts += 1
            spacing = min(self.config.restart_after * 2**self.restarts, RESTART_BACKOFF_MAX)
            self._restart_due = self._clock() + spacing
            logger.warning(
                "the tunnel stayed down: the watchdog restarted the VPN",
                extra={"restarts": self.restarts, "next_in_s": round(spacing)},
            )
            self._alert(
                "restart",
                "VPN restarted by the watchdog",
                [
                    "The VPN process was running but the tunnel was not connected for "
                    f"{round(self.config.restart_after / 60)} minutes or more.",
                    f"The watchdog asked gluetun to restart the VPN (restart {self.restarts} of "
                    f"at most {self.config.restart_max} during this outage).",
                ],
                urgent=False,
            )
        except GluetunAuthError as exc:
            self._we_stopped = False  # it may not stop (or start) anything: nothing to put back
            self.restart_state = "denied"
            logger.error(
                "the watchdog may not restart the VPN: run make watchdog-key and recreate "
                "gluetun, or set WATCHDOG_RESTART_AFTER=0",
                extra={"error": str(exc)},
            )
            self._alert(
                "restart_denied",
                "Watchdog may not restart the VPN",
                [
                    "The tunnel has been down for a long time, but gluetun's control server "
                    "refuses the restart. Run: make watchdog-key, then recreate gluetun and the "
                    "watchdog. Meanwhile restart the VPN by hand: docker compose restart gluetun",
                ],
                urgent=False,
            )
        except GluetunError as exc:
            logger.error("restarting the VPN did not work", extra={"error": str(exc)})
            self._next_start_attempt = self._clock() + RESTART_START_RETRY_SECONDS
            self._restart_due = self._clock() + self.config.restart_after
            if self._we_stopped:
                self._alert(
                    "restart_failed",
                    "VPN stopped and not started again",
                    [
                        "The watchdog stopped the VPN to restart it and could not start it "
                        "again. It keeps trying; if this stays: docker compose restart gluetun",
                    ],
                    urgent=True,
                )
        finally:
            self._restarting = False

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
        was_tunnel = self.cause == "tunnel"
        restarts = self.restarts
        self.cause = ""
        self.restarts = 0
        self._restart_due = None
        self._gave_up = False
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
            [
                (
                    f"The tunnel is connected again{lasted}."
                    if was_tunnel
                    else f"The VPN is running again{lasted}."
                ),
                *([f"The watchdog restarted the VPN {restarts} time(s)."] if restarts else []),
                f"Torrents: {outcome}.",
            ],
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
            # The pause belonged to the mismatch that just ended (the owner may have resumed by
            # now): a LATER confirmed mismatch is a new leak and must pause again (#67).
            self._egress_paused = False
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

    def _probe_loop(self, stop: threading.Event) -> None:
        interval = self.config.probe_interval
        assert self.probe is not None
        stop.wait(min(5.0, interval))
        while not stop.is_set():
            try:
                self.run_probe()
            except Exception:
                logger.exception("tunnel probe failed")
            stop.wait(interval)

    def run_probe(self) -> None:
        """One reachability probe through the tunnel (every `probe_interval` seconds, own thread).

        `probe_confirm` failures in a row are needed before the probe counts as bad: one lost
        packet is not a dead tunnel. A success counts at once."""
        if self.probe is None:
            return
        if self.probe.check():
            self._probe_failures = 0
            self.probe_state = "ok"
            return
        self._probe_failures += 1
        if self._probe_failures >= self.config.probe_confirm:
            if self.probe_state != "failing":
                logger.warning("tunnel probe fails", extra={"why": self.probe.last_error})
            self.probe_state = "failing"

    def run(self, stop: threading.Event, heartbeat: Path | None = HEARTBEAT_FILE) -> None:
        if self.probe is not None and self.config.probe_interval > 0:
            threading.Thread(
                target=self._probe_loop, args=(stop,), name="tunnel-probe", daemon=True
            ).start()
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
    * `cause`, `tunnel`, `reason`, `probe`, `restart`, `restarts` (#81): why it is down, from the
      same page (`cause` is `tunnel` when gluetun runs but the tunnel does not);
    * `gluetun`: gluetun's own status word (`running`, `stopped`, ...) and `address`: `present` or
      `none` (never the address itself): what the VM scenario "process running, tunnel down"
      needs to show that it reproduced the incident;
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
    if what in {"cause", "tunnel", "reason", "restart", "restarts", "probe"}:
        # What the watchdog says about WHY (#81): from its status page, no address in it.
        port = config.status_port or 8090
        status = WatchdogStatusClient(f"http://127.0.0.1:{port}").fetch_status()
        if status["state"] == "unknown":
            return 1, "unreachable"
        return 0, str(status.get(what, ""))
    if what in {"gluetun", "address"}:
        # What gluetun itself says: its status word, and only whether it HAS a public address.
        control = GluetunControl(config.control_url, config.api_key, timeout=3.0)
        try:
            if what == "gluetun":
                return 0, control.vpn_status()
            try:
                control.public_ip()
            except GluetunNoAddress:
                return 0, "none"
            return 0, "present"
        except GluetunError:
            return 1, "unreachable"
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
    tunnel_probe = (
        TunnelProbe(config.probe_proxy, parse_targets(config.probe_targets))
        if config.probe_proxy and config.probe_interval > 0
        else None
    )
    watchdog = Watchdog(control, qbit, config, alerter=alerter, egress=egress, probe=tunnel_probe)
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
            "tunnel_grace": config.tunnel_grace,
            "tunnel_probe_every": config.probe_interval if tunnel_probe else "off",
            "restart_after": config.restart_after or "off",
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
