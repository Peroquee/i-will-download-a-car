#!/usr/bin/env python3
"""render_ovpn.py: make ExpressVPN's .ovpn acceptable to gluetun's custom provider (issue #4).

Gluetun's custom OpenVPN provider refuses a `remote` that is a host name ("host is
not an IP address"), and ExpressVPN's file has a host name plus `remote-random`.
This script reads the original, resolves the first `remote` host name to an IPv4
address, drops `remote-random` (one fixed server), and writes the result for the
gluetun container to read. Everything else in the file is copied unchanged.

Usage:
    scripts/vpn/render_ovpn.py [--source PATH] [--output PATH] [--dry-run]

    --source   ExpressVPN's original file (default: config/expressvpn.ovpn)
    --output   the rendered file (default: config/gluetun/custom/expressvpn.ovpn,
               which the gluetun container sees as /gluetun/custom/expressvpn.ovpn)
    --dry-run  resolve and report, write nothing

Server IPs rotate, so run it again before `docker compose up` (`make up` does) and
whenever the tunnel stops connecting. Safe to re-run. The file holds ExpressVPN's
keys: the output is written with mode 600, and this script never prints file contents.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "config" / "expressvpn.ovpn"
DEFAULT_OUTPUT = REPO_ROOT / "config" / "gluetun" / "custom" / "expressvpn.ovpn"

Resolver = Callable[[str], str]


class RenderError(Exception):
    """A problem the user can fix; reported without a traceback."""


def resolve_ipv4(host: str) -> str:
    """The first IPv4 address DNS returns for host."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RenderError(f"cannot resolve {host}: {exc.strerror or exc}") from exc
    if not infos:
        raise RenderError(f"{host} has no IPv4 address")
    return str(infos[0][4][0])


def is_ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def render(text: str, resolve: Resolver = resolve_ipv4) -> tuple[str, str]:
    """Return (rendered text, report line). Raises RenderError on an unusable file."""
    out: list[str] = []
    report = ""
    for line in text.splitlines():
        words = line.split()
        directive = words[0] if words else ""
        if directive == "remote-random":
            continue
        if directive == "remote":
            if report:
                continue  # one fixed server: keep the first remote only
            if len(words) < 2:
                raise RenderError("a 'remote' line has no host")
            host, rest = words[1], words[2:]
            if is_ipv4(host):
                address, report = host, f"remote is already an IP address ({host}), kept"
            else:
                address = resolve(host)
                report = f"remote {host} -> {address}"
            out.append(" ".join(["remote", address, *rest]))
            continue
        out.append(line)
    if not report:
        raise RenderError("the file has no 'remote' line; is it really an OpenVPN config?")
    return "\n".join(out) + "\n", report


def write_private(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, mode 600, so a crash never leaves half a file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        tmp.replace(path)
    except PermissionError as exc:
        raise RenderError(
            f"cannot write {path}: {exc.strerror}. If Docker created the folder as root, run: "
            f'sudo chown -R "$USER" {path.parent.parent}'
        ) from exc


def main(argv: list[str] | None = None, resolve: Resolver = resolve_ipv4) -> int:
    parser = argparse.ArgumentParser(description="Render ExpressVPN's .ovpn for gluetun.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="resolve and report, write nothing")
    args = parser.parse_args(argv)

    try:
        if not args.source.is_file():
            raise RenderError(
                f"{args.source} not found. Download the OpenVPN config from the ExpressVPN "
                "website (Set up on more devices > Manual configuration) and save it there."
            )
        rendered, report = render(args.source.read_text(encoding="utf-8"), resolve)
        print(report)
        if args.dry_run:
            print(f"dry run: would write {args.output}")
            return 0
        write_private(args.output, rendered)
        print(f"wrote {args.output} (mode 600)")
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
