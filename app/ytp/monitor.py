"""Noticing things worth an alert (issue #38): a scheduled job that writes the alert log.

`AlertMonitor.run_once` (every 15 seconds) looks at four places and records what changed. It
only ever READS the system and WRITES the app's own `alerts` / `vpn_events` tables; it controls
nothing (pausing torrents stays the watchdog's job, retrying a job stays the user's).

* **The VPN**, through the watchdog's status (the watchdog has no database; the app records its
  transitions). The condition is `ok`, `down`, `blind`, `mismatch` (the egress check), `starting`
  or `unknown` (the watchdog does not answer). Every change is a row in `vpn_events`. Alerts:
  `vpn_down`, `vpn_tunnel` (#81: the process runs, the tunnel does not), `vpn_blind`,
  `egress_mismatch`, `watchdog_silent` (unknown for several polls in a row; one missed poll is not
  news) and `vpn_recovered` (back to `ok` after one of those).
  `starting` alone never alerts. The last condition is kept in `app_state`, so a restart of the
  app does not repeat an alert.
* **Failed jobs**: every job that failed since the last look (a cursor in `app_state`; the first
  look starts at "now", so an old failure from before this feature is not announced).
* **Feed trouble**: a channel or podcast whose `last_error` is new or different. The error text
  is the feed's own message. An alert is not repeated while the same error stays (every retry
  would otherwise write one), and a recovered subscription can alert again later.
* **Disk space** on the media drive: `disk_low` as a warning below 10 % free and as an error
  below 5 %; one alert per crossing, not per run.

No address and no secret goes into an alert: the texts are built here from names, reasons and
numbers, and `alertlog.add` redacts anything that looks like an address.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import alertlog, jobs
from .db import Database
from .feeds import is_service_wide
from .jobs_api import job_label
from .scheduler import get_state, set_state

logger = logging.getLogger(__name__)

MONITOR_JOB_ID = "alert-monitor"
MONITOR_SECONDS = 15
SILENT_POLLS = 4  # "unknown" this many runs in a row = the watchdog is not answering

KEY_VPN = "alerts.vpn"
KEY_JOBS = "alerts.jobs_cursor"
KEY_FEEDS = "alerts.feed_errors"
KEY_DISK = "alerts.disk"

DISK_WARN_PERCENT = 10.0
DISK_ERROR_PERCENT = 5.0
BAD = ("down", "blind", "mismatch", "unknown")


def vpn_condition(status: dict[str, Any]) -> str:
    """The condition shown by the VPN bar (same rules as `Logic.vpnView` in the page)."""
    state = status.get("state")
    if status.get("egress") == "mismatch" and state in ("ok", "unknown"):
        return "mismatch"
    return state if state in ("ok", "down", "blind", "starting") else "unknown"


class AlertMonitor:
    def __init__(
        self,
        db: Database,
        watchdog: Any | None,
        data_root: Path,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ):
        self.db = db
        self.watchdog = watchdog
        self.data_root = data_root
        self._now = now
        self._disk_usage = disk_usage
        self._unknown_streak = 0

    def run_once(self) -> None:
        """Never raises: one failing check must not hide the others."""
        for name, check in (
            ("vpn", self.check_vpn),
            ("jobs", self.check_jobs),
            ("feeds", self.check_feeds),
            ("disk", self.check_disk),
        ):
            try:
                check()
            except sqlite3.ProgrammingError:  # the app is stopping and closed the database
                return
            except Exception:
                logger.exception("alert check failed", extra={"check": name})

    # ------------------------------------------------------------------ the VPN

    def check_vpn(self) -> None:
        if self.watchdog is None:
            return
        status = self.watchdog.fetch_status()
        condition = vpn_condition(status)
        paused = status.get("paused") is True
        reason = status.get("reason") if isinstance(status.get("reason"), str) else ""

        # One missed poll is not news: "unknown" only counts after several in a row.
        if condition == "unknown":
            self._unknown_streak += 1
            if self._unknown_streak < SILENT_POLLS:
                return
        else:
            self._unknown_streak = 0

        previous = _load(self.db, KEY_VPN, {})
        before = previous.get("condition")
        if before == condition and previous.get("paused") == paused:
            return
        _store(self.db, KEY_VPN, {"condition": condition, "paused": paused})
        alertlog.record_vpn_event(self.db, condition, paused, reason)
        if before == condition:
            return  # only the "paused" flag changed: shown in the history, not worth an alert
        logger.info("vpn condition changed", extra={"condition": condition, "was": before})
        if condition == "down":
            # The VPN process can run while the tunnel is dead (#81): a different thing to look
            # at (the VPN's logs) than a stopped container, so it has its own alert kind and text.
            tunnel = status.get("cause") == "tunnel"
            alertlog.add(
                self.db,
                "vpn_tunnel" if tunnel else "vpn_down",
                "error",
                "The VPN tunnel is not connected" if tunnel else "The VPN is down",
                (
                    "The VPN process is running, but the tunnel is not connected: nothing gets "
                    "through it. "
                    if tunnel
                    else "The VPN connection is down. "
                )
                + "The watchdog pauses all torrents so that nothing leaves through the home "
                "connection. Downloads wait until the VPN is back.",
                {"paused": paused},
            )
        elif condition == "blind":
            alertlog.add(
                self.db,
                "vpn_blind",
                "error",
                "The watchdog cannot see the VPN",
                "The watchdog cannot read the VPN status, so nobody can say whether the "
                "downloads are protected. Check the VPN container.",
            )
        elif condition == "mismatch":
            alertlog.add(
                self.db,
                "egress_mismatch",
                "error",
                "Traffic may not be leaving through the VPN",
                "The check of the public address found that the traffic does not come out of "
                "the VPN. All torrents were paused and stay paused until you look at it.",
            )
        elif condition == "unknown":
            alertlog.add(
                self.db,
                "watchdog_silent",
                "warning",
                "The VPN watchdog does not answer",
                "The app cannot reach the VPN watchdog, so the VPN state is not known.",
            )
        elif condition == "ok" and before in BAD:
            alertlog.add(
                self.db,
                "vpn_recovered",
                "info",
                "The VPN is working again",
                "The VPN is back. Torrents the watchdog paused stay paused unless "
                "WATCHDOG_RESUME_AFTER is set; resume them in qBittorrent if needed.",
                {"was": before},
            )

    # ------------------------------------------------------------------ failed jobs

    def check_jobs(self) -> None:
        stamp = self._now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        cursor = get_state(self.db, KEY_JOBS)
        if cursor is None:  # first look: announce only what fails from now on
            set_state(self.db, KEY_JOBS, stamp)
            return
        rows = self.db.query(
            "SELECT * FROM jobs WHERE status = 'failed' AND updated_at > ? ORDER BY updated_at, id",
            (cursor,),
        )
        newest = cursor
        for row in rows:
            job = jobs.Job.from_row(row)
            label = job_label(job)
            reason = job.error or ""
            alertlog.add(
                self.db,
                "job_failed",
                "error",
                f"A download failed: {label}",
                f"The download of {label} failed: {reason or 'no reason was recorded'}.",
                {"job_id": job.id, "label": label, "reason": reason, "type": job.type},
                key=f"job:{job.id}:{job.updated_at}",
            )
            newest = max(newest, job.updated_at)
        if newest != cursor:
            set_state(self.db, KEY_JOBS, newest)

    # ------------------------------------------------------------------ feeds

    def check_feeds(self) -> None:
        seen: dict[str, str] = _load(self.db, KEY_FEEDS, {})
        current: dict[str, str] = {}
        service_wide: set[str] = set()  # the same message for any number of channels: one alert
        for kind, table, name_column in (
            ("channel", "subscriptions", "channel_name"),
            ("podcast", "podcast_subscriptions", "title"),
        ):
            for row in self.db.query(
                f"SELECT id, {name_column} AS name, last_error FROM {table} "
                "WHERE last_error IS NOT NULL AND last_error <> ''"
            ):
                key = f"{kind}:{row['id']}"
                digest = hashlib.sha256(row["last_error"].encode()).hexdigest()[:16]
                if kind == "channel" and is_service_wide(row["last_error"]):
                    # YouTube is the problem, for every channel alike (feeds.SERVICE_WIDE): one
                    # alert for all of them, not one per channel.
                    key = f"service:{digest}"
                    if key not in service_wide and seen.get(key) != digest:
                        alertlog.add(
                            self.db,
                            "youtube_unavailable",
                            "warning",
                            "YouTube is not listing videos right now",
                            row["last_error"],
                            {"reason": row["last_error"]},
                        )
                    service_wide.add(key)
                    current[key] = digest
                    continue
                current[key] = digest
                if seen.get(key) != digest:
                    alertlog.add(
                        self.db,
                        "feed_error",
                        "warning",
                        f"Feed problem: {row['name']}",
                        f"{row['name']} could not be checked: {row['last_error']}",
                        {"source": kind, "name": row["name"], "reason": row["last_error"]},
                    )
        if current != seen:
            _store(self.db, KEY_FEEDS, current)

    # ------------------------------------------------------------------ disk space

    def check_disk(self) -> None:
        try:
            usage = self._disk_usage(self.data_root)
        except OSError:
            return  # the media drive is not mounted: the health check says so, not an alert
        if not usage.total:
            return
        free_percent = usage.free * 100 / usage.total
        level = (
            "error"
            if free_percent < DISK_ERROR_PERCENT
            else "warning"
            if free_percent < DISK_WARN_PERCENT
            else "ok"
        )
        before = get_state(self.db, KEY_DISK)
        if before == level:
            return
        set_state(self.db, KEY_DISK, level)
        if level == "ok" or (before == "error" and level == "warning"):
            return  # getting better is not an alert
        alertlog.add(
            self.db,
            "disk_low",
            level,
            "The media drive is almost full",
            f"Only {free_percent:.0f} % of the media drive is free. Downloads will fail when it "
            "is full.",
            {"free_percent": round(free_percent), "free_gb": round(usage.free / 1e9)},
        )


def disk_summary(data_root: Path, disk_usage: Callable[[Path], Any] = shutil.disk_usage):
    """`{"free_percent", "free_gb", "total_gb"}` for the dashboard, or None if unreadable."""
    try:
        usage = disk_usage(data_root)
    except OSError:
        return None
    if not usage.total:
        return None
    return {
        "free_percent": round(usage.free * 100 / usage.total),
        "free_gb": round(usage.free / 1e9),
        "total_gb": round(usage.total / 1e9),
    }


def _load(db: Database, key: str, default: Any) -> Any:
    raw = get_state(db, key)
    if raw is None:
        return default
    try:
        value = json.loads(raw)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _store(db: Database, key: str, value: Any) -> None:
    set_state(db, key, json.dumps(value, sort_keys=True))
