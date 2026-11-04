#!/usr/bin/env python3
"""wake.py: wake the transcription PC over the network when videos wait for Whisper.

A systemd timer on the media VM runs `wake.py run` every 2 minutes (docs/transcription.md). It
reads the app's database (read only) and sends a Wake-on-LAN "magic packet" to the PC when ALL of
this is true:

* at least one video is `queued` for transcription;
* the PC's worker has not asked the app for work in the last `WORKER_FRESH_SECONDS` (it asks
  every 30 s while it runs, so a PC that is up is never woken again);
* the last packet was sent more than `WAKE_RETRY_SECONDS` ago (a PC needs a few minutes to boot
  from the network and start its worker; a PC running Windows ignores the packet, and the next
  try comes 10 minutes later, which costs nothing);
* now is inside `TRANSCRIBER_WAKE_HOURS`, if that is set.

The PC then boots from the network (the netboot server answers it with the Linux image that runs
the worker, see docs/transcription.md), transcribes, and switches itself off when nothing is left.
Shutting down is the PC's decision, never this script's.

Commands:
    wake.py run [--dry-run]       what the timer does
    wake.py status [--json]       the queue, the last contact and packet, what `run` would do
    wake.py wake                  send a packet now (to test Wake-on-LAN)
    wake.py install-units --user NAME [--dry-run]   (root) install and start the timer
    wake.py remove-units [--dry-run]                (root) stop and remove it

Settings (in `.env`, process environment wins): TRANSCRIBER_PC_MAC (required to wake anything),
TRANSCRIBER_WOL_ADDRESS (the broadcast address, default 255.255.255.255), TRANSCRIBER_WAKE_HOURS
(like `1-7`; empty = any time). The database is `config/app/app.db`; the time of the last packet
is kept in `config/transcriber/last-wake`. Standard library only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "install"))

from ytp_install.envfile import parse_env  # noqa: E402

UNIT_NAME = "youtube-premium-transcriber-wake"
UNIT_DIR = Path("/etc/systemd/system")
WORKER_STATE_KEY = "transcriber.worker"  # the same key as ytp.transcriptions
WORKER_FRESH_SECONDS = 180
WAKE_RETRY_SECONDS = 600
WOL_PORT = 9
SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
_MAC = re.compile(r"[0-9a-f]{2}([:-]?)[0-9a-f]{2}(?:\1[0-9a-f]{2}){4}", re.I)
_HOURS = re.compile(r"(\d{1,2})-(\d{1,2})")


class WakeError(Exception):
    """Something a person has to fix (a setting, a missing database)."""


@dataclass(frozen=True)
class Config:
    root: Path
    mac: str  # normalised aa:bb:cc:dd:ee:ff, or "" (not set)
    address: str
    hours: tuple[int, int] | None

    @property
    def database(self) -> Path:
        return self.root / "config" / "app" / "app.db"

    @property
    def stamp(self) -> Path:
        return self.root / "config" / "transcriber" / "last-wake"


def parse_mac(text: str) -> str:
    """`AA-BB-CC-DD-EE-FF`, `aa:bb:...` or `aabbccddeeff` -> `aa:bb:cc:dd:ee:ff`."""
    value = text.strip()
    if not _MAC.fullmatch(value):
        raise WakeError(
            f"TRANSCRIBER_PC_MAC is not a MAC address like aa:bb:cc:dd:ee:ff: {value!r}"
        )
    digits = re.sub(r"[^0-9a-f]", "", value.lower())
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def parse_hours(text: str) -> tuple[int, int] | None:
    """`1-7` -> (1, 7): from 01:00 up to (not including) 07:00. `22-6` wraps past midnight."""
    value = text.strip()
    if not value:
        return None
    match = _HOURS.fullmatch(value)
    if not match or not all(0 <= int(g) <= 24 for g in match.groups()) or match[1] == match[2]:
        raise WakeError(f"TRANSCRIBER_WAKE_HOURS must look like 1-7 or 22-6, got {value!r}")
    return int(match[1]), int(match[2])


def within(hours: tuple[int, int] | None, hour: int) -> bool:
    if hours is None:
        return True
    start, end = hours
    return start <= hour < end if start < end else hour >= start or hour < end


def load_config(root: Path, environ: dict[str, str] | None = None) -> Config:
    environ = dict(os.environ) if environ is None else environ
    env_file = root / ".env"
    values = parse_env(env_file.read_text(encoding="utf-8")) if env_file.is_file() else {}

    def setting(name: str, default: str = "") -> str:
        return (environ.get(name) or values.get(name) or default).strip()

    raw_mac = setting("TRANSCRIBER_PC_MAC")
    address = setting("TRANSCRIBER_WOL_ADDRESS", "255.255.255.255")
    try:
        ipaddress.IPv4Address(address)
    except ValueError as exc:
        raise WakeError(
            f"TRANSCRIBER_WOL_ADDRESS must be an IPv4 address, got {address!r}"
        ) from exc
    return Config(
        root=root,
        mac=parse_mac(raw_mac) if raw_mac else "",
        address=address,
        hours=parse_hours(setting("TRANSCRIBER_WAKE_HOURS")),
    )


# ---------------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class QueueState:
    queued: int
    running: int
    worker_last_seen: datetime | None


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_queue(database: Path) -> QueueState:
    """The queue as the app's database has it. Read only: this script never writes there."""
    if not database.is_file():
        raise WakeError(f"the app's database is not there: {database}")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        try:
            counts = dict(
                conn.execute(
                    "SELECT status, COUNT(*) FROM transcriptions GROUP BY status"
                ).fetchall()
            )
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise WakeError(
                    "the app has no transcription queue yet (update and start it first)"
                ) from exc
            raise
        seen = conn.execute(
            "SELECT updated_at FROM app_state WHERE key = ?", (WORKER_STATE_KEY,)
        ).fetchone()
    finally:
        conn.close()
    return QueueState(
        queued=counts.get("queued", 0),
        running=counts.get("running", 0),
        worker_last_seen=parse_time(seen[0]) if seen else None,
    )


def last_wake(cfg: Config) -> datetime | None:
    try:
        return parse_time(cfg.stamp.read_text(encoding="utf-8").strip())
    except OSError:
        return None


def remember_wake(cfg: Config, when: datetime) -> None:
    cfg.stamp.parent.mkdir(parents=True, exist_ok=True)
    cfg.stamp.write_text(when.isoformat(timespec="seconds") + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------- deciding


def decide(
    cfg: Config, queue: QueueState, woken: datetime | None, now: datetime
) -> tuple[bool, str]:
    """(wake?, why). Pure: everything it needs is passed in, so the tests can walk every case."""
    if queue.queued == 0:
        return False, "nothing is waiting for Whisper"
    if not cfg.mac:
        return False, f"{queue.queued} waiting, but TRANSCRIBER_PC_MAC is not set"
    if queue.worker_last_seen is not None:
        age = (now - queue.worker_last_seen).total_seconds()
        if age < WORKER_FRESH_SECONDS:
            return False, f"the PC is up (asked {int(age)} s ago)"
    if woken is not None:
        age = (now - woken).total_seconds()
        if 0 <= age < WAKE_RETRY_SECONDS:
            return False, f"woken {int(age)} s ago, waiting for it to boot"
    local_hour = now.astimezone().hour
    if not within(cfg.hours, local_hour):
        return (
            False,
            f"{queue.queued} waiting, but {local_hour}:00 is outside TRANSCRIBER_WAKE_HOURS",
        )
    return True, f"{queue.queued} waiting and the PC is not up"


# ---------------------------------------------------------------------------------- waking


def magic_packet(mac: str) -> bytes:
    """6 x 0xFF, then the MAC 16 times: the Wake-on-LAN frame every network card understands."""
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise WakeError("a MAC address has 6 bytes")
    return b"\xff" * 6 + raw * 16


def send_packet(mac: str, address: str, port: int = WOL_PORT) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Twice: a single UDP packet on a busy switch can get lost, and a second one is harmless.
        for _ in range(2):
            sock.sendto(packet, (address, port))
            time.sleep(0.2)


def run(
    cfg: Config, *, dry_run: bool = False, now: datetime | None = None, send=send_packet
) -> str:
    now = now or datetime.now(UTC)
    queue = read_queue(cfg.database)
    wake, why = decide(cfg, queue, last_wake(cfg), now)
    if not wake:
        return f"not waking: {why}"
    if dry_run:
        return f"would wake {cfg.mac} via {cfg.address}: {why}"
    send(cfg.mac, cfg.address)
    remember_wake(cfg, now)
    return f"woke {cfg.mac} via {cfg.address}: {why}"


# ---------------------------------------------------------------------------------- units


def render_unit(text: str, user: str, root: Path) -> str:
    return text.replace("@USER@", user).replace("@ROOT@", str(root))


def _systemctl(argv: list[str], dry_run: bool, *, check: bool = True) -> None:
    if dry_run:
        print("+ " + " ".join(argv))
        return
    result = subprocess.run(argv, check=False)
    if check and result.returncode != 0:
        raise WakeError(f"`{' '.join(argv)}` failed")


def install_units(
    root: Path, user: str, *, dry_run: bool = False, unit_dir: Path = UNIT_DIR
) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise WakeError("that is not a plain user name")
    if not SAFE_PATH.fullmatch(str(root)):
        raise WakeError("the install folder has characters a unit file cannot hold safely")
    for name in (f"{UNIT_NAME}.service", f"{UNIT_NAME}.timer"):
        source = root / "scripts" / "transcriber" / "systemd" / name
        text = render_unit(source.read_text(encoding="utf-8"), user, root)
        target = unit_dir / name
        if dry_run:
            print(f"+ write {target}")
            continue
        target.write_text(text, encoding="utf-8")
        target.chmod(0o644)
    _systemctl(["systemctl", "daemon-reload"], dry_run)
    _systemctl(["systemctl", "enable", "--now", f"{UNIT_NAME}.timer"], dry_run)


def remove_units(*, dry_run: bool = False, unit_dir: Path = UNIT_DIR) -> None:
    _systemctl(["systemctl", "disable", "--now", f"{UNIT_NAME}.timer"], dry_run, check=False)
    for name in (f"{UNIT_NAME}.service", f"{UNIT_NAME}.timer"):
        if dry_run:
            print(f"+ remove {unit_dir / name}")
        else:
            (unit_dir / name).unlink(missing_ok=True)
    _systemctl(["systemctl", "daemon-reload"], dry_run, check=False)


# ---------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wake.py", description=__doc__.split("\n")[0])
    parser.add_argument("--repo-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    running = sub.add_parser("run")
    running.add_argument("--dry-run", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    sub.add_parser("wake")
    units = sub.add_parser("install-units")
    units.add_argument("--user", required=True)
    units.add_argument("--dry-run", action="store_true")
    removing = sub.add_parser("remove-units")
    removing.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root: Path = args.repo_root.resolve()
    try:
        if args.command == "install-units":
            install_units(root, args.user, dry_run=args.dry_run)
            return 0
        if args.command == "remove-units":
            remove_units(dry_run=args.dry_run)
            return 0
        cfg = load_config(root)
        if args.command == "run":
            print(run(cfg, dry_run=args.dry_run))
        elif args.command == "wake":
            if not cfg.mac:
                raise WakeError("TRANSCRIBER_PC_MAC is not set in .env")
            send_packet(cfg.mac, cfg.address)
            remember_wake(cfg, datetime.now(UTC))
            print(f"sent a Wake-on-LAN packet to {cfg.mac} via {cfg.address}")
        else:
            queue = read_queue(cfg.database)
            woken = last_wake(cfg)
            wake, why = decide(cfg, queue, woken, datetime.now(UTC))
            report = {
                "queued": queue.queued,
                "running": queue.running,
                "worker_last_seen": queue.worker_last_seen.isoformat()
                if queue.worker_last_seen
                else None,
                "last_wake": woken.isoformat() if woken else None,
                "mac": cfg.mac or None,
                "would_wake": wake,
                "why": why,
            }
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                for key, value in report.items():
                    print(f"{key:17} {value}")
    except WakeError as exc:
        print(f"wake.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
