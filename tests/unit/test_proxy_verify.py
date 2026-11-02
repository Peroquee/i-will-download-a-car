"""scripts/proxy/verify_proxy.py against a fake proxy that behaves like the real Caddy setup."""

from __future__ import annotations

import base64
import http.server
import importlib.util
import io
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "proxy" / "verify_proxy.py"
spec = importlib.util.spec_from_file_location("verify_proxy", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

USER = "tester"
PASSWORD = "a long test password"  # a test value, not a secret
GOOD = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


def start_fake_app(*, basic_challenge: bool = False, login_required: bool = True):
    """The app behind a LAN-only proxy: public /health, the app's own 401 elsewhere."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if login_required and self.path != "/health":
                self.send_response(401)
                if basic_challenge:
                    self.send_header("WWW-Authenticate", 'Basic realm="restricted"')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_fake_proxy(*, require_login: bool = True, bad_status: int | None = None):
    """The qBittorrent port: the proxy's own basic auth."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if require_login and self.headers.get("Authorization") != GOOD:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="restricted"')
                self.end_headers()
                return
            self.send_response(bad_status or 200)
            self.end_headers()
            self.wfile.write(b"ok")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def proxy() -> Iterator[http.server.HTTPServer]:
    server = start_fake_proxy()
    yield server
    server.shutdown()


@pytest.fixture
def app_port() -> Iterator[http.server.HTTPServer]:
    server = start_fake_app()
    yield server
    server.shutdown()


def run(server, app, password: str = PASSWORD):
    return mod.verify(
        "127.0.0.1", app.server_address[1], server.server_address[1], free_port(), USER, password
    )


def test_a_proxy_that_behaves_passes_every_check(proxy, app_port, monkeypatch):
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    assert run(proxy, app_port).failures == 0


def test_a_wrong_password_fails_only_the_qbittorrent_login_check(
    proxy, app_port, monkeypatch, capsys
):
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    report = run(proxy, app_port, password="nope")
    assert report.failures == 1
    assert "make proxy-password" in capsys.readouterr().out


def test_qbittorrent_without_login_fails(app_port, monkeypatch):
    server = start_fake_proxy(require_login=False)
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    try:
        report = run(server, app_port)
    finally:
        server.shutdown()
    assert report.failures == 1  # no 401 on the qBittorrent port


def test_an_app_port_that_still_asks_for_basic_auth_fails(proxy, monkeypatch, capsys):
    app = start_fake_app(basic_challenge=True)
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    try:
        report = run(proxy, app)
    finally:
        app.shutdown()
    assert report.failures == 1
    assert "no longer asks for Basic" in capsys.readouterr().out


def test_an_app_that_serves_without_a_login_fails(proxy, monkeypatch):
    app = start_fake_app(login_required=False)
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    try:
        report = run(proxy, app)
    finally:
        app.shutdown()
    assert report.failures == 1  # /api/jobs must answer 401


def test_an_app_that_is_published_directly_fails(proxy, app_port, monkeypatch, capsys):
    other = start_fake_proxy()
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", other.server_address[1])
    try:
        report = run(proxy, app_port)
    finally:
        other.shutdown()
    assert report.failures == 1
    assert "not reachable" in capsys.readouterr().out


def test_qbittorrent_published_directly_fails(proxy, app_port, monkeypatch):
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    direct = start_fake_proxy()
    try:
        report = mod.verify(
            "127.0.0.1", app_port.server_address[1], proxy.server_address[1],
            direct.server_address[1], USER, PASSWORD,
        )  # fmt: skip
    finally:
        direct.shutdown()
    assert report.failures == 1


def test_nothing_listening_is_reported_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    report = mod.verify("127.0.0.1", free_port(), free_port(), free_port(), USER, PASSWORD)
    assert report.failures >= 4
    assert "Traceback" not in capsys.readouterr().out


def test_the_password_is_never_printed(proxy, app_port, monkeypatch, capsys):
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    run(proxy, app_port, password="nope-secret-value")
    out = capsys.readouterr()
    assert "nope-secret-value" not in out.out + out.err
    assert GOOD not in out.out + out.err


def write_env(tmp_path: Path, app_server, qbt_server, bind: str = "127.0.0.1") -> Path:
    env = tmp_path / ".env"
    env.write_text(
        f"PROXY_BIND={bind}\nPROXY_PORT={app_server.server_address[1]}\n"
        f"PROXY_QBT_PORT={qbt_server.server_address[1]}\n"
        f"PROXY_USER={USER}\nQBIT_WEBUI_PORT={free_port()}\n"
    )
    return env


def test_main_reads_address_ports_and_user_from_env(proxy, app_port, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    env = write_env(tmp_path, app_port, proxy)
    assert mod.main(["--env-file", str(env), "--password-stdin"]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_exits_1_when_a_check_fails(proxy, app_port, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "APP_BACKEND_PORT", free_port())
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong\n"))
    env = write_env(tmp_path, app_port, proxy)
    assert mod.main(["--env-file", str(env), "--password-stdin"]) == 1


def test_main_needs_a_single_address(proxy, app_port, tmp_path):
    env = write_env(tmp_path, app_port, proxy, bind="0.0.0.0")
    with pytest.raises(SystemExit) as error:
        mod.main(["--env-file", str(env), "--password-stdin"])
    assert "--host" in str(error.value)


def test_main_explains_a_missing_env_file(tmp_path):
    with pytest.raises(SystemExit) as error:
        mod.main(["--env-file", str(tmp_path / "nope")])
    assert "not found" in str(error.value)
