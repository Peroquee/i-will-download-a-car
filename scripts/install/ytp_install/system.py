"""What the installer looks at on this machine: the system, the network address, the time zone.

Parsing is separate from running (`parse_*` take text), so the rules are tested with sample
output of `ip`, and nothing here changes the machine.
"""

from __future__ import annotations

import ipaddress
import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path

from .runner import Runner

ZONEINFO = Path("/usr/share/zoneinfo")


def parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            values[name.strip()] = value.strip().strip("'\"")
    return values


def is_debian_12(os_release_text: str) -> bool:
    values = parse_os_release(os_release_text)
    return values.get("ID") == "debian" and values.get("VERSION_CODENAME") == "bookworm"


def parse_route_source(text: str) -> str | None:
    """The source address in `ip -4 -o route get <address>` output (`... src 192.168.1.2 ...`)."""
    match = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)\b", text)
    return match.group(1) if match else None


def parse_subnet(addr_text: str, address: str) -> str | None:
    """The network of `address` from `ip -4 -o addr show` output (`inet 192.168.178.123/24 ...`)."""
    for match in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+/\d+)\b", addr_text):
        interface = ipaddress.ip_interface(match.group(1))
        if str(interface.ip) == address:
            return str(interface.network)
    return None


@dataclass(frozen=True)
class Lan:
    address: str
    subnet: str


def detect_lan(runner: Runner) -> Lan | None:
    """The address this machine uses towards the internet and the subnet it sits in. Nothing is
    sent: `ip route get` only asks the routing table."""
    route = runner.run(["ip", "-4", "-o", "route", "get", "1.1.1.1"], readonly=True)
    address = parse_route_source(route.stdout) if route.ok else None
    if address is None:
        return None
    addrs = runner.run(["ip", "-4", "-o", "addr", "show"], readonly=True)
    subnet = parse_subnet(addrs.stdout, address) if addrs.ok else None
    return Lan(address, subnet) if subnet else None


def timezone_known(name: str, zoneinfo: Path = ZONEINFO) -> bool:
    return (
        bool(re.fullmatch(r"[A-Za-z0-9_+./-]{1,64}", name))
        and ".." not in name
        and (zoneinfo / name).is_file()
    )


def detect_timezone(
    etc_timezone: Path = Path("/etc/timezone"), default: str = "Europe/Berlin"
) -> str:
    try:
        value = etc_timezone.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return value if value and timezone_known(value) else default


@dataclass(frozen=True)
class LoginUser:
    name: str
    uid: int
    gid: int


def detect_login_user(root: Path, environ: dict[str, str] | None = None) -> LoginUser:
    """The person who started the installer: SUDO_USER, else the owner of the project folder."""
    env = os.environ if environ is None else environ
    name = env.get("SUDO_USER", "")
    try:
        entry = pwd.getpwnam(name) if name and name != "root" else pwd.getpwuid(root.stat().st_uid)
    except KeyError:
        entry = pwd.getpwuid(os.getuid())
    return LoginUser(entry.pw_name, entry.pw_uid, entry.pw_gid)
