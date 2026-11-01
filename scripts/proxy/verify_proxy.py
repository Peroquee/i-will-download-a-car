#!/usr/bin/env python3
"""verify_proxy.py: prove the reverse proxy does its job from a machine on the LAN (issue #29).

Checks, against the running stack:
  1. the app port answers 401 (with a Basic challenge) without credentials,
     and 401 with a wrong password,
  2. with the right password the app answers (GET /health -> 200),
  3. the qBittorrent port answers 401 without credentials and 200 with them,
  4. the ports behind the proxy are NOT reachable on the VM's LAN address:
     the app's 8000 and qBittorrent's Web UI port (it is bound to 127.0.0.1).

What it cannot show from one machine: that a client OUTSIDE the LAN is refused. That
needs a second source address; tests/system/test_c10_proxy.py proves it with a throwaway
Caddy that uses the same proxy/Caddyfile and a LAN_SUBNET that excludes the test client.

Usage:
    scripts/proxy/verify_proxy.py [--env-file PATH] [--host ADDRESS] [--user NAME]
                                  [--password-stdin]

Address, ports and user come from .env (PROXY_BIND, PROXY_PORT, PROXY_QBT_PORT, PROXY_USER).
--host overrides the address (needed when PROXY_BIND is 0.0.0.0). The password is asked for
on the terminal or read from stdin; it is never printed. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import http.client
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
APP_BACKEND_PORT = 8000  # the app's own port inside the bridge network; must not be published
TIMEOUT = 8.0


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def request(
    host: str, port: int, path: str, auth: tuple[str, str] | None
) -> tuple[int, dict[str, str]]:
    """GET without following redirects. Returns (status, lower-cased response headers)."""
    headers = {"User-Agent": "ytp-verify-proxy"}
    if auth is not None:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}
    finally:
        conn.close()


def port_is_closed(host: str, port: int) -> bool:
    """True if nothing accepts a TCP connection on host:port (refused or filtered)."""
    try:
        with socket.create_connection((host, port), timeout=3.0):
            return False
    except OSError:
        return True


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else "")
        )
        if not ok:
            self.failures += 1


def verify(host: str, app_port: int, qbt_port: int, direct_qbt_port: int, user: str, password: str):
    report = Report()
    login = (user, password)

    def status(port: int, auth, path: str = "/") -> tuple[int | None, dict[str, str]]:
        try:
            return request(host, port, path, auth)
        except OSError as error:
            return None, {"error": str(error)}

    code, headers = status(app_port, None)
    report.check(
        code == 401, "app without credentials -> 401", f"got {code} {headers.get('error', '')}"
    )
    report.check(
        headers.get("www-authenticate", "").lower().startswith("basic"),
        "app asks for Basic authentication",
        headers.get("www-authenticate", "no WWW-Authenticate header"),
    )
    code, _ = status(app_port, (user, password + "x"))
    report.check(code == 401, "app with a wrong password -> 401", f"got {code}")
    code, _ = status(app_port, login, "/health")
    report.check(
        code == 200,
        "app /health with the login -> 200",
        f"got {code}: if 401, run `make proxy-password` and `docker compose up -d`",
    )

    code, _ = status(qbt_port, None)
    report.check(code == 401, "qBittorrent without credentials -> 401", f"got {code}")
    code, _ = status(qbt_port, login)
    report.check(code == 200, "qBittorrent with the login -> 200", f"got {code}")

    report.check(
        port_is_closed(host, APP_BACKEND_PORT),
        f"the app's own port {APP_BACKEND_PORT} is not reachable on {host}",
    )
    report.check(
        port_is_closed(host, direct_qbt_port),
        f"qBittorrent's own port {direct_qbt_port} is not reachable on {host} (bound to localhost)",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the reverse proxy from the LAN.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--host", default=None, help="the VM's LAN address (default: PROXY_BIND)")
    parser.add_argument("--user", default=None, help="login name (default: PROXY_USER)")
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin"
    )
    args = parser.parse_args(argv)

    if not args.env_file.exists():
        raise SystemExit(f"error: {args.env_file} not found")
    env = read_env(args.env_file)
    host = args.host or env.get("PROXY_BIND", "")
    if not host or host == "0.0.0.0":
        raise SystemExit("error: PROXY_BIND is not a single address; pass --host <the VM's LAN IP>")
    try:
        app_port = int(env["PROXY_PORT"])
        qbt_port = int(env["PROXY_QBT_PORT"])
        direct_qbt_port = int(env.get("QBIT_WEBUI_PORT", "8080"))
    except (KeyError, ValueError):
        raise SystemExit(
            "error: PROXY_PORT / PROXY_QBT_PORT missing or not numbers in .env"
        ) from None
    user = args.user or env.get("PROXY_USER", "admin")

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"Password for {user}: ")
    if not password:
        raise SystemExit("error: no password given")

    print(f"proxy at {host}: app port {app_port}, qBittorrent port {qbt_port}")
    report = verify(host, app_port, qbt_port, direct_qbt_port, user, password)
    if report.failures:
        print(f"{report.failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
