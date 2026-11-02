"""The real proxy/Caddyfile in a real Caddy: LAN check, login and header spoofing (issue #29).

Runs only on a host with a Docker daemon (the ytp-host runner, or by hand on the VM:
`make test-system`). It does not touch the running stack: it starts a throwaway Caddy
(same image tag, same Caddyfile, same hardening flags as docker-compose.yml) on its own
network, in front of a stand-in upstream that answers on 8000 and 8080 under the names
`app` and `gluetun`.

The test client is the host, which Caddy sees as the network's gateway address. So
"inside the LAN" means LAN_SUBNET = that network's subnet, and "outside" means a subnet
the client is not in. That is what lets one machine prove the refusal of outside clients,
including a spoofed X-Forwarded-For.
"""

from __future__ import annotations

import base64
import http.client
import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from tests.helpers import docker
from tests.helpers.compose import parse_env_file
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT

pytestmark = [pytest.mark.docker, pytest.mark.network, pytest.mark.slow]

CADDY_IMAGE = f"caddy:{parse_env_file(ENV_EXAMPLE)['CADDY_TAG']}"
PROXY_DIR = REPO_ROOT / "proxy"
USER = "tester"
PASSWORD = "a long test password"  # a test value, not a secret
OUTSIDE_SUBNET = "10.99.99.0/24"  # no test network is ever in here (checked below)
SPOOFED_LAN_HEADERS = {
    "X-Forwarded-For": "10.99.99.7",
    "X-Real-IP": "10.99.99.7",
    "Forwarded": "for=10.99.99.7",
    "X-Forwarded-Host": "localhost",
}


@dataclass
class Net:
    name: str
    subnet: str


@dataclass
class Proxy:
    name: str
    app_port: int
    qbt_port: int


def basic(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def get(port: int, path: str = "/", headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}
    finally:
        conn.close()


def get_headers_seen_by_upstream(port: int, headers: dict[str, str]) -> dict[str, str]:
    """The request headers the stand-in upstream received for a request through the proxy."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/", headers=headers)
        response = conn.getresponse()
        body = response.read()
        assert response.status == 200, body
        return json.loads(body)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def net() -> Iterator[Net]:
    name = docker.unique_name("net")
    created = docker.run(["network", "create", name])
    assert created.returncode == 0, created.stderr
    info = json.loads(docker.run(["network", "inspect", name], check=True).stdout)
    subnet = info[0]["IPAM"]["Config"][0]["Subnet"]
    assert not subnet.startswith("10.99.99."), "pick another OUTSIDE_SUBNET for this host"
    yield Net(name, subnet)
    docker.run(["network", "rm", name])


ECHO_UPSTREAM = """
import http.server, json, threading

class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

for port in (8000, 8080):
    server = http.server.ThreadingHTTPServer(("", port), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
threading.Event().wait()
"""


@pytest.fixture(scope="module")
def upstream(net) -> Iterator[str]:
    """Answers 200 on :8000 (the app) and :8080 (qBittorrent), sends back the request headers it
    received as JSON, and logs each request."""
    name = docker.unique_name("upstream")
    args = ["run", "-d", "--name", name, "--network", net.name]
    args += ["--network-alias", "app", "--network-alias", "gluetun"]
    args += [docker.PYTHON, "python", "-u", "-c", ECHO_UPSTREAM]
    started = docker.run(args, timeout=300)
    assert started.returncode == 0, started.stderr
    time.sleep(2)
    yield name
    docker.run(["rm", "-f", name])


@pytest.fixture(scope="module")
def password_hash() -> str:
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", CADDY_IMAGE, "caddy", "hash-password"],
        input=PASSWORD + "\n",
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("$2"), "expected a bcrypt hash"
    return result.stdout.strip()


def caddy_args(net: Net, lan_subnet: str, user: str, hashed: str, *, detach: bool) -> list[str]:
    """`docker run` arguments equivalent to the proxy service in docker-compose.yml."""
    args = ["run", "--network", net.name, "--user", "4321:4321", "--read-only"]
    for path in ("/data", "/config", "/tmp"):
        args += ["--tmpfs", path]
    args += ["--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE"]
    args += ["--security-opt", "no-new-privileges:true"]
    args += ["-e", f"LAN_SUBNET={lan_subnet}", "-e", f"PROXY_USER={user}"]
    args += ["-e", f"PROXY_PASSWORD_HASH={hashed}"]
    args += ["-v", f"{PROXY_DIR}:/etc/caddy:ro"]
    if detach:
        args += ["-d", "-p", "127.0.0.1::8080", "-p", "127.0.0.1::8081"]
        args += ["--health-cmd", "wget -q -O /dev/null http://127.0.0.1:8082/health"]
        args += ["--health-interval", "2s", "--health-retries", "10"]
    return args


def published_port(name: str, container_port: int) -> int:
    out = docker.run(["port", name, f"{container_port}/tcp"], check=True).stdout.split()[0]
    return int(out.rsplit(":", 1)[1])


def start_proxy(net: Net, lan_subnet: str, user: str, hashed: str) -> tuple[Proxy | None, str]:
    """Start a throwaway Caddy. Returns (Proxy, "") once healthy, or (None, logs) if it stopped."""
    name = docker.unique_name("caddy")
    started = docker.run(
        [*caddy_args(net, lan_subnet, user, hashed, detach=True), "--name", name, CADDY_IMAGE],
        timeout=300,
    )
    assert started.returncode == 0, started.stderr
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = docker.run(
            ["inspect", "--format", "{{.State.Status}} {{.State.Health.Status}}", name]
        ).stdout.split()
        if state and state[0] != "running":
            logs = docker.run(["logs", name])
            docker.run(["rm", "-f", name])
            return None, logs.stdout + logs.stderr
        if len(state) == 2 and state[1] == "healthy":
            return Proxy(name, published_port(name, 8080), published_port(name, 8081)), ""
        time.sleep(1)
    logs = docker.run(["logs", name])
    docker.run(["rm", "-f", name])
    pytest.fail(f"Caddy did not become healthy:\n{logs.stdout}{logs.stderr}")


@pytest.fixture(scope="module")
def inside(net, upstream, password_hash) -> Iterator[Proxy]:
    """The client counts as inside the LAN: LAN_SUBNET is the network the client comes from."""
    proxy, logs = start_proxy(net, net.subnet, USER, password_hash)
    assert proxy, logs
    yield proxy
    docker.run(["rm", "-f", proxy.name])


@pytest.fixture(scope="module")
def outside(net, upstream, password_hash) -> Iterator[Proxy]:
    """The client is outside: LAN_SUBNET is a network the client is not in."""
    proxy, logs = start_proxy(net, OUTSIDE_SUBNET, USER, password_hash)
    assert proxy, logs
    yield proxy
    docker.run(["rm", "-f", proxy.name])


def upstream_requests(upstream: str) -> int:
    logs = docker.run(["logs", upstream])
    return (logs.stdout + logs.stderr).count('"GET ')


def test_the_caddyfile_is_valid(password_hash):
    result = docker.run(
        [
            "run",
            "--rm",
            "-e",
            "LAN_SUBNET=192.168.178.0/24",
            "-e",
            f"PROXY_USER={USER}",
            "-e",
            f"PROXY_PASSWORD_HASH={password_hash}",
            "-v",
            f"{PROXY_DIR}:/etc/caddy:ro",
            CADDY_IMAGE,
            "caddy",
            "validate",
            "--config",
            "/etc/caddy/Caddyfile",
        ],
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("port_name", ["app_port", "qbt_port"])
class TestClientInsideTheLan:
    def test_no_credentials_means_401_with_a_basic_challenge(self, inside, port_name):
        status, headers = get(getattr(inside, port_name))
        assert status == 401
        assert headers["www-authenticate"].lower().startswith("basic")

    def test_a_wrong_password_means_401(self, inside, port_name):
        status, _ = get(getattr(inside, port_name), headers=basic(password="not the password"))
        assert status == 401

    def test_a_wrong_user_means_401(self, inside, port_name):
        status, _ = get(getattr(inside, port_name), headers=basic(user="someone-else"))
        assert status == 401

    def test_the_right_login_reaches_the_upstream(self, inside, port_name):
        status, headers = get(getattr(inside, port_name), headers=basic())
        assert status == 200
        assert "server" not in headers, "the Server header is removed"

    def test_forwarding_headers_from_the_client_change_nothing(self, inside, port_name):
        port = getattr(inside, port_name)
        assert get(port, headers=SPOOFED_LAN_HEADERS)[0] == 401
        assert get(port, headers={**SPOOFED_LAN_HEADERS, **basic()})[0] == 200


@pytest.mark.security
@pytest.mark.parametrize("port_name", ["app_port", "qbt_port"])
class TestClientOutsideTheLan:
    def test_everything_is_refused_even_with_the_right_login(self, outside, port_name):
        port = getattr(outside, port_name)
        assert get(port)[0] == 403
        assert get(port, headers=basic())[0] == 403
        assert get(port, "/health", headers=basic())[0] == 403

    def test_a_spoofed_lan_address_in_headers_is_not_believed(self, outside, port_name):
        # The client's real address is outside; headers claiming a LAN address must not matter.
        port = getattr(outside, port_name)
        assert get(port, headers=SPOOFED_LAN_HEADERS)[0] == 403
        assert get(port, headers={**SPOOFED_LAN_HEADERS, **basic()})[0] == 403


@pytest.mark.security
def test_refused_requests_never_reach_the_upstream(outside, upstream):
    before = upstream_requests(upstream)
    for port in (outside.app_port, outside.qbt_port):
        get(port, headers=basic())
        get(port, headers={**SPOOFED_LAN_HEADERS, **basic()})
    assert upstream_requests(upstream) == before


@pytest.mark.security
def test_unauthenticated_requests_never_reach_the_upstream(inside, upstream):
    before = upstream_requests(upstream)
    for port in (inside.app_port, inside.qbt_port):
        get(port)
        get(port, headers=basic(password="wrong"))
    assert upstream_requests(upstream) == before


@pytest.mark.security
@pytest.mark.parametrize("port_name", ["app_port", "qbt_port"])
def test_the_upstream_never_receives_the_proxy_login(inside, port_name):
    """The browser's basic-auth header is the proxy's login. qBittorrent 5.3 answers 401 to any
    request with an Authorization header, and no backend has a use for the proxy password."""
    seen = get_headers_seen_by_upstream(getattr(inside, port_name), basic())
    assert "authorization" not in seen, seen
    assert "proxy-authorization" not in seen, seen
    assert "password" not in json.dumps(seen).lower()


@pytest.mark.parametrize("port_name", ["app_port", "qbt_port"])
def test_the_host_header_still_reaches_the_upstream_unchanged(inside, port_name):
    """qBittorrent's CSRF check compares Origin with Host, so Host must not be rewritten."""
    port = getattr(inside, port_name)
    seen = get_headers_seen_by_upstream(port, {**basic(), "Host": f"192.168.178.123:{port}"})
    assert seen["host"] == f"192.168.178.123:{port}"


@pytest.mark.security
def test_the_health_check_listener_is_not_published(inside):
    ports = docker.run(["port", inside.name]).stdout
    assert "8082" not in ports
    assert set(ports.replace("->", " ").split()) >= {"8080/tcp", "8081/tcp"}


@pytest.mark.security
@pytest.mark.parametrize(
    ("lan_subnet", "user", "hashed", "denied"),
    [
        ("", USER, "$2a$14$x", {403}),
        (OUTSIDE_SUBNET, "", "", {403}),
        ("__NET__", "", "", {401, 403}),
        ("__NET__", USER, "", {401, 403}),
        ("__NET__", USER, "changeme", {401, 403}),
    ],
)
def test_a_missing_setting_refuses_everyone_or_does_not_start(
    net, upstream, lan_subnet, user, hashed, denied
):
    """A half-configured proxy must fail closed: no start, or nothing gets through."""
    subnet = net.subnet if lan_subnet == "__NET__" else lan_subnet
    proxy, _logs = start_proxy(net, subnet, user, hashed)
    if proxy is None:
        return  # refused to start: closed
    try:
        for port in (proxy.app_port, proxy.qbt_port):
            assert get(port, headers=basic())[0] in denied
    finally:
        docker.run(["rm", "-f", proxy.name])
