#!/usr/bin/env python3
"""recovery_test.py: does the whole stack come back correctly after a reboot? (issue #32)

A reboot cannot be done BY a script that lives on the machine being rebooted, so the test has
two halves around the reboot, which you do yourself:

    1. scripts/ops/recovery_test.py prepare      # on the media VM, stack up and healthy
    2. reboot: the whole Proxmox host (the real test), or just the VM:  sudo reboot
    3. scripts/ops/recovery_test.py verify       # on the VM, once you can log in again

`prepare` records what is running (container start times, the boot id, the jobs that are in
progress) in `reports/recovery-state.json` (mode 600, nothing secret in it). `verify` waits for the
stack and then checks, printing one PASS/FAIL/WARN line each and NO address:

  * the machine really rebooted (new boot id) and Docker starts at boot;
  * every container is running again, was started AFTER the reboot, is healthy, and has a restart
    policy that brings it back;
  * the ORDER: gluetun before qbittorrent, app before proxy, and qBittorrent was started only after
    gluetun's log said the tunnel was up and its DNS ready (when that can be read);
  * the VPN comes before qBittorrent's traffic: the watchdog says `ok`, qBittorrent's public
    address is a VPN one (never the home one) and no torrent runs while the VPN is not ok;
  * the jobs that were in progress: each one is done, failed WITH a reason, or on its way again
    (queued/downloading); one stuck in `organizing` or one that vanished is a failure.

Add a small legal torrent or a YouTube video and start `prepare` while it is downloading, or the job
check has nothing to judge (a WARN, or a failure with --require-jobs).

Usage:
    scripts/ops/recovery_test.py prepare [--state FILE] [--dry-run]
    scripts/ops/recovery_test.py verify  [--state FILE] [--wait SECONDS] [--require-jobs]
                                         [--no-reboot-check] [--dry-run]

Exit: 0 everything passed (WARNs allowed), 1 a check failed, 2 not ready / cannot run.
Run as the user who can use Docker and read config/app/app.db. Only the standard library is used.
Environment (optional): RT_COMPOSE (default "docker compose"), RT_IP_URL (default
https://api.ipify.org: asked from the host for the home address, never printed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = REPO_ROOT / "reports" / "recovery-state.json"
SERVICES = ("gluetun", "qbittorrent", "app", "proxy", "watchdog")
ORDER = (("gluetun", "qbittorrent"), ("app", "proxy"))  # (must start first, must start later)
IN_PROGRESS = ("queued", "downloading", "organizing")
OK_POLICIES = ("unless-stopped", "always")
STUCK_ORGANIZING_SECONDS = 600


class NotReady(Exception):
    """The test cannot run (exit 2); the message says why."""


# ------------------------------------------------------------------------------------ docker


class Docker:
    """The few Docker commands the test needs; tests replace this class."""

    def __init__(self, compose: list[str]):
        self.compose = compose

    def _run(self, args: list[str], timeout: float = 60.0) -> tuple[int, str]:
        try:
            done = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return done.returncode, done.stdout

    def container(self, service: str) -> str | None:
        code, out = self._run([*self.compose, "ps", "-a", "-q", service])
        ident = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
        return ident or None

    def inspect(self, ident: str) -> dict | None:
        code, out = self._run(["docker", "inspect", ident])
        if code != 0:
            return None
        try:
            return json.loads(out)[0]
        except (ValueError, IndexError):
            return None

    def exec(self, ident: str, command: list[str], timeout: float = 20.0) -> tuple[int, str]:
        return self._run(["docker", "exec", ident, *command], timeout)

    def logs(self, ident: str) -> str:
        return self._run(["docker", "logs", "--timestamps", ident], timeout=30.0)[1]

    def docker_enabled(self) -> bool:
        code, out = self._run(["systemctl", "is-enabled", "docker"])
        return code == 0 and out.strip() == "enabled"


# ------------------------------------------------------------------------------------ facts


@dataclass
class Report:
    failures: int = 0
    warnings: int = 0
    lines: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)
        print(line)

    def ok(self, text: str) -> None:
        self.say(f"PASS  {text}")

    def bad(self, text: str) -> None:
        self.failures += 1
        self.say(f"FAIL  {text}")

    def warn(self, text: str) -> None:
        self.warnings += 1
        self.say(f"WARN  {text}")


def parse_time(text: str) -> datetime | None:
    """Docker's RFC 3339 time with nanoseconds, as aware UTC; None for the zero time."""
    if not text or text.startswith("0001-"):
        return None
    cleaned = re.sub(r"(\.\d{6})\d*", r"\1", text.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(cleaned).astimezone(UTC)
    except ValueError:
        return None


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def read_jobs(db: Path) -> list[dict]:
    """The jobs that are in progress: id, type, status, error, updated_at (no names, no links)."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise NotReady(
            f"cannot open {db} ({exc}); run as the owner of config/ or with sudo"
        ) from exc
    try:
        marks = ",".join("?" for _ in IN_PROGRESS)
        rows = conn.execute(
            f"SELECT id, type, status, COALESCE(error, ''), updated_at FROM jobs "
            f"WHERE status IN ({marks}) ORDER BY id",
            IN_PROGRESS,
        ).fetchall()
    except sqlite3.Error as exc:
        raise NotReady(f"cannot read the jobs from {db}: {exc}") from exc
    finally:
        conn.close()
    return [
        {"id": r[0], "type": r[1], "status": r[2], "error": r[3], "updated_at": r[4]} for r in rows
    ]


def read_job(db: Path, job_id: int) -> dict | None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        row = conn.execute(
            "SELECT id, type, status, COALESCE(error, ''), updated_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"id": row[0], "type": row[1], "status": row[2], "error": row[3], "updated_at": row[4]}


def judge_job(before: dict, after: dict | None, now: datetime) -> tuple[str, str]:
    """('ok' | 'bad' | 'wait', note) for one job that was in progress before the reboot."""
    label = f"job #{before['id']} ({before['type']}, was {before['status']})"
    if after is None:
        return "bad", f"{label} has vanished"
    status = after["status"]
    if status == "done":
        return "ok", f"{label} finished: done"
    if status == "failed":
        if after["error"].strip():
            return "ok", f"{label} failed cleanly, with a reason"
        return "bad", f"{label} failed WITHOUT a reason"
    if status in ("queued", "downloading"):
        return "ok", f"{label} is on its way again: {status}"
    updated = parse_time(after["updated_at"])
    if updated is not None and (now - updated).total_seconds() > STUCK_ORGANIZING_SECONDS:
        return "bad", f"{label} is stuck in {status} since {after['updated_at']}"
    return "wait", f"{label} is in {status}"


# ------------------------------------------------------------------------------------ prepare


def snapshot(docker: Docker, db: Path) -> dict:
    containers: dict[str, dict] = {}
    for service in SERVICES:
        ident = docker.container(service)
        info = docker.inspect(ident) if ident else None
        if info is None:
            raise NotReady(f"service {service} has no container; is the stack up (`make up`)?")
        state = info.get("State", {})
        health = state.get("Health", {}).get("Status", "none")
        if not state.get("Running") or health not in ("healthy", "none"):
            raise NotReady(
                f"service {service} is not running and healthy ({health}); fix that first"
            )
        containers[service] = {"started_at": state.get("StartedAt", "")}
    return {
        "version": 1,
        "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "boot_id": boot_id(),
        "containers": containers,
        "jobs": read_jobs(db),
    }


def prepare(args: argparse.Namespace, docker: Docker) -> int:
    snap = snapshot(docker, args.db)
    print(f"stack is up; {len(snap['jobs'])} job(s) in progress")
    for job in snap["jobs"]:
        print(f"  job #{job['id']} ({job['type']}): {job['status']}")
    if not snap["jobs"]:
        print("note: nothing is in progress; add a small download first for a meaningful test")
    if args.dry_run:
        print(f"dry run: would write {args.state}")
        return 0
    args.state.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(snap, handle, indent=2)
    args.state.chmod(0o600)
    print(f"state written to {args.state}")
    print("Now reboot: the Proxmox host (the real test) or the VM (`sudo reboot`).")
    print("When you can log in again: scripts/ops/recovery_test.py verify")
    return 0


# ------------------------------------------------------------------------------------ verify


def health_of(info: dict) -> str:
    return info.get("State", {}).get("Health", {}).get("Status", "none")


def wait_for_stack(docker: Docker, wait: float, poll: float, clock, sleep) -> dict[str, dict]:
    """Wait until every service is running and healthy; returns what was seen (inspect data)."""
    deadline = clock() + wait
    seen: dict[str, dict] = {}
    while True:
        seen = {}
        for service in SERVICES:
            ident = docker.container(service)
            info = docker.inspect(ident) if ident else None
            if info is not None:
                seen[service] = info
        if len(seen) == len(SERVICES) and all(
            i.get("State", {}).get("Running") and health_of(i) in ("healthy", "none")
            for i in seen.values()
        ):
            return seen
        if clock() >= deadline:
            return seen
        sleep(poll)


# What gluetun 3.41's log says when it is ready (it never prints "healthy"): the OpenVPN tunnel is
# up, and its DNS is ready. [healthcheck] listening only means its health server started.
READY_LINES = (
    re.compile(r"\[openvpn\] Initialization Sequence Completed"),
    re.compile(r"\[dns\] ready"),
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def gluetun_ready_time(logs: str, since: datetime | None) -> datetime | None:
    """When gluetun's timestamped log says it is ready, after `since` (its latest start).

    The tunnel-up line and the DNS-ready line are each looked for (first one after `since`); the
    LATER of the two found is the answer, so a qBittorrent started before either is caught.
    None if neither line is there (then only the start order can be judged)."""
    found: list[datetime] = []
    for pattern in READY_LINES:
        for line in logs.splitlines():
            clean = _ANSI.sub("", line)
            if not pattern.search(clean):
                continue
            stamp = parse_time(clean.split(" ", 1)[0])
            if stamp is not None and (since is None or stamp >= since):
                found.append(stamp)
                break
    return max(found) if found else None


def home_address(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read(100).decode().strip()
    except (OSError, ValueError):
        return ""


def verdict(address: str, home: str) -> str:
    if not address:
        return "no connection"
    return "HOME IP LEAK" if address == home else "VPN IP"


def verify(
    args: argparse.Namespace,
    docker: Docker,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    current_boot: Callable[[], str] = boot_id,
    ip_of_home: Callable[[], str] | None = None,
) -> int:
    try:
        before = json.loads(args.state.read_text())
    except (OSError, ValueError):
        raise NotReady(
            f"no readable state at {args.state}; run `prepare` before the reboot"
        ) from None
    report = Report()
    print(f"verifying against {args.state} (written {before.get('created', '?')})")

    # 1. the reboot itself
    if args.no_reboot_check:
        report.warn("reboot check skipped (--no-reboot-check)")
    elif before.get("boot_id") and current_boot() == before["boot_id"]:
        report.bad("the machine has NOT rebooted since `prepare` (same boot id)")
    else:
        report.ok("the machine rebooted since `prepare`")
    if docker.docker_enabled():
        report.ok("Docker is enabled at boot")
    else:
        report.warn("could not confirm that Docker starts at boot (systemctl is-enabled docker)")

    # 2. everything running and healthy again
    seen = wait_for_stack(docker, args.wait, args.poll, clock, sleep)
    for service in SERVICES:
        info = seen.get(service)
        if info is None:
            report.bad(f"{service}: no container")
            continue
        state = info.get("State", {})
        health = health_of(info)
        if not state.get("Running"):
            report.bad(f"{service}: not running ({state.get('Status', '?')})")
        elif health not in ("healthy", "none"):
            report.bad(f"{service}: running but {health}")
        else:
            report.ok(f"{service}: running and {'healthy' if health == 'healthy' else 'up'}")
        policy = info.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "")
        if policy not in OK_POLICIES:
            report.bad(f"{service}: restart policy is {policy or 'none'}, it would not come back")
        old = parse_time(before.get("containers", {}).get(service, {}).get("started_at", ""))
        new = parse_time(state.get("StartedAt", ""))
        if old is not None and new is not None and new <= old and not args.no_reboot_check:
            report.bad(f"{service}: was not started again after `prepare`")

    # 3. the order
    started = {s: parse_time(i.get("State", {}).get("StartedAt", "")) for s, i in seen.items()}
    for first, later in ORDER:
        a, b = started.get(first), started.get(later)
        if a is None or b is None:
            report.bad(f"order {first} -> {later}: a start time is missing")
        elif a < b:
            report.ok(f"order: {first} started before {later}")
        else:
            report.bad(f"order: {later} started BEFORE {first}")
    gluetun_id = docker.container("gluetun")
    qbit_started = started.get("qbittorrent")
    gluetun_started = started.get("gluetun")
    ready_at = gluetun_ready_time(docker.logs(gluetun_id), gluetun_started) if gluetun_id else None
    if ready_at is None or qbit_started is None:
        report.warn("could not read from gluetun's log when it was ready; start order only")
    elif qbit_started >= ready_at:
        report.ok("qbittorrent was started only after gluetun's tunnel and DNS were ready")
    else:
        report.bad("qbittorrent was started BEFORE gluetun's tunnel and DNS were ready")

    # 4. the VPN before qBittorrent's traffic
    watchdog_id = docker.container("watchdog")
    qbit_id = docker.container("qbittorrent")
    state_code, state_text = docker.exec(
        watchdog_id or "", ["python", "-m", "ytp.watchdog", "--probe", "state"]
    )
    wd_state = state_text.strip() if state_code == 0 else "unreachable"
    if wd_state == "ok":
        report.ok("the watchdog says the VPN is ok")
    else:
        report.bad(f"the watchdog state is {wd_state}, not ok")
    counts_code, counts_text = docker.exec(
        watchdog_id or "", ["python", "-m", "ytp.watchdog", "--probe", "counts"]
    )
    running = None
    match = re.search(r"running=(\d+)", counts_text)
    if counts_code == 0 and match:
        running = int(match.group(1))
    if running is not None and wd_state != "ok" and running > 0:
        report.bad(f"{running} torrent(s) are RUNNING while the VPN is not ok")
    elif running is not None:
        report.ok(f"no torrent runs without the VPN ({running} running, state {wd_state})")
    else:
        report.warn("could not ask qBittorrent how many torrents run")
    home = (
        ip_of_home or (lambda: home_address(os.environ.get("RT_IP_URL", "https://api.ipify.org")))
    )()
    _, address = docker.exec(
        qbit_id or "", ["curl", "-s", "--max-time", "8", "https://1.1.1.1/cdn-cgi/trace"]
    )
    found = ""
    for line in address.splitlines():
        if line.startswith("ip="):
            found = line[3:].strip()
    kind = verdict(found, home) if home else ("no connection" if not found else "unknown")
    if kind == "VPN IP":
        report.ok("qbittorrent leaves through a VPN address (VPN IP)")
    elif kind == "HOME IP LEAK":
        report.bad("HOME IP LEAK: qbittorrent shows the home address")
    elif kind == "no connection":
        report.bad("qbittorrent has no connection although the VPN is reported up")
    else:
        report.warn("could not learn the home address, so the exit address cannot be judged")

    # 5. the jobs
    judge_jobs(args, before, report, clock, sleep, now)

    print()
    if report.failures:
        print(f"FAILED: {report.failures} check(s) failed, {report.warnings} warning(s)")
        return 1
    print(f"OK: the stack came back correctly ({report.warnings} warning(s))")
    return 0


def judge_jobs(args, before, report, clock, sleep, now) -> None:
    jobs = before.get("jobs", [])
    if not jobs:
        message = "no job was in progress at `prepare`: the job check had nothing to judge"
        (report.bad if args.require_jobs else report.warn)(message)
        return
    deadline = clock() + args.wait
    pending = {job["id"]: job for job in jobs}
    verdicts: dict[int, tuple[str, str]] = {}
    while True:
        for job_id, job in list(pending.items()):
            after = read_job(args.db, job_id)
            kind, note = judge_job(job, after, now())
            verdicts[job_id] = (kind, note)
            if kind != "wait":
                del pending[job_id]
        if not pending or clock() >= deadline:
            break
        sleep(args.poll)
    for job in jobs:
        kind, note = verdicts[job["id"]]
        if kind == "ok":
            report.ok(note)
        elif kind == "bad":
            report.bad(note)
        else:
            report.bad(f"{note} and did not settle in {int(args.wait)} s")


# ------------------------------------------------------------------------------------ main


def main(argv: list[str] | None = None, docker: Docker | None = None) -> int:
    parser = argparse.ArgumentParser(description="Before/after-reboot check of the whole stack.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--state", type=Path, default=DEFAULT_STATE)
        p.add_argument("--db", type=Path, default=REPO_ROOT / "config" / "app" / "app.db")
        p.add_argument("--dry-run", action="store_true")
        if name == "verify":
            p.add_argument(
                "--wait", type=float, default=600.0, help="seconds to wait for the stack"
            )
            p.add_argument("--poll", type=float, default=10.0)
            p.add_argument("--require-jobs", action="store_true")
            p.add_argument("--no-reboot-check", action="store_true")
    args = parser.parse_args(argv)
    docker = docker or Docker(shlex.split(os.environ.get("RT_COMPOSE", "docker compose")))
    try:
        if args.command == "prepare":
            return prepare(args, docker)
        if args.dry_run:
            print(
                f"dry run: would wait up to {args.wait:.0f} s for {', '.join(SERVICES)} and check"
            )
            print("  reboot, health, restart policy, start order, VPN before traffic, jobs")
            return 0
        return verify(args, docker)
    except NotReady as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
