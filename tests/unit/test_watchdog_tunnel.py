# ruff: noqa: E501
"""The watchdog and a tunnel that is down while gluetun says "running" (issue #81).

gluetun's `/v1/vpn/status` answers `running` whenever its OpenVPN process loop runs, also while the
handshake fails again and again: on 2026-11-10 the tunnel was dead for three hours and the watchdog
showed the VPN as up. These tests use a fake gluetun that behaves like that: status running, no
public address (or a probe that gets nothing through).
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from itertools import pairwise

import pytest

from tests.helpers.fake_gluetun import API_KEY, FakeGluetun
from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from tests.unit.test_watchdog import A, B, C, Clock, free_port, pauses
from ytp import watchdog as wd
from ytp.clients.gluetun import (
    GluetunAuthError,
    GluetunControl,
    GluetunNoAddress,
    GluetunUnreachable,
)
from ytp.clients.qbittorrent import QbitClient
from ytp.tunnelprobe import TunnelProbe, parse_proxy, parse_targets
from ytp.watchdog import ConfigError, Watchdog, WatchdogConfig


class Alerts:
    """Stands in for the Alerter: keeps what would have been sent."""

    def __init__(self) -> None:
        self.sent: list = []

    def notify(self, alert) -> None:
        self.sent.append(alert)

    def kinds(self) -> list[str]:
        return [a.kind for a in self.sent]


class FakeProbe:
    """A tunnel probe whose answer the test chooses."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.last_error = "no target reachable through the tunnel (TimeoutError)"
        self.calls = 0

    def check(self) -> bool:
        self.calls += 1
        return self.ok


@pytest.fixture
def gluetun() -> Iterator[FakeGluetun]:
    server = FakeGluetun().start()
    yield server
    server.stop()


@pytest.fixture
def qbit_server() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    server.torrents = [
        {"hash": A, "name": "a", "state": "downloading", "progress": 0.5},
        {"hash": B, "name": "b", "state": "stoppedDL", "progress": 0.5},
        {"hash": C, "name": "c", "state": "uploading", "progress": 1.0},
    ]
    yield server
    server.stop()


def make(gluetun, qbit_server, *, probe=None, grace=180.0, restart_after=0.0, **config):
    clock = Clock()
    alerts = Alerts()
    cfg = WatchdogConfig(
        control_url=gluetun.url,
        api_key=API_KEY,
        qbit_url=qbit_server.url,
        qbit_user=USER,
        qbit_password=PASSWORD,
        tunnel_grace=grace,
        restart_after=restart_after,
        **config,
    )
    dog = Watchdog(
        GluetunControl(cfg.control_url, cfg.api_key, timeout=1.5),
        QbitClient(cfg.qbit_url, USER, PASSWORD, timeout=2),
        cfg,
        clock=clock,
        alerter=alerts,
        probe=probe,
        sleep=lambda seconds: None,
    )
    return dog, clock, alerts


def steps(dog, clock, count, seconds=1.0):
    for _ in range(count):
        dog.step()
        clock.now += seconds


def no_tunnel(gluetun):
    """gluetun's process runs, the tunnel does not: the state of the incident."""
    gluetun.status = "running"
    gluetun.public = {"public_ip": ""}


def wait_for_restart(dog, timeout=5.0):
    deadline = time.monotonic() + timeout
    while dog._restarting and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not dog._restarting, "the restart thread did not finish"


# --- the gluetun client ----------------------------------------------------------------------------


def test_an_empty_public_address_is_its_own_error(gluetun):
    no_tunnel(gluetun)
    with pytest.raises(GluetunNoAddress, match="no public address"):
        GluetunControl(gluetun.url, API_KEY).public_ip()
    gluetun.public = {}
    with pytest.raises(GluetunNoAddress):
        GluetunControl(gluetun.url, API_KEY).public_ip()


def test_the_vpn_is_stopped_and_started_with_a_put_and_the_key_in_the_header(gluetun):
    control = GluetunControl(gluetun.url, API_KEY)
    control.set_vpn_status("stopped")
    control.set_vpn_status("running")
    assert gluetun.puts == ["stopped", "running"]
    puts = [h for h in gluetun.hits if h.get("method") == "PUT"]
    assert all(h["headers"]["x-api-key"] == API_KEY for h in puts)
    assert all(API_KEY not in h["path"] for h in puts)


@pytest.mark.parametrize("status", [403, 404, 401])
def test_a_refused_put_is_an_auth_error_that_says_what_to_do(gluetun, status):
    gluetun.put_http_status = status
    with pytest.raises(GluetunAuthError, match="make watchdog-key"):
        GluetunControl(gluetun.url, API_KEY).set_vpn_status("stopped")


@pytest.mark.parametrize("word", ["restarting", "", "RUNNING ", "crashed"])
def test_only_running_and_stopped_can_be_put(gluetun, word):
    with pytest.raises(ValueError, match="running"):
        GluetunControl(gluetun.url, API_KEY).set_vpn_status(word)
    assert gluetun.puts == []


# --- running is not connected ------------------------------------------------------------------------


def test_with_a_public_address_a_running_vpn_is_up(gluetun, qbit_server):
    dog, clock, alerts = make(gluetun, qbit_server, probe=FakeProbe(True))
    steps(dog, clock, 10)
    snap = dog.snapshot()
    assert (snap["state"], snap["tunnel"], snap["cause"]) == ("ok", "connected", "")
    assert pauses(qbit_server) == 0
    assert alerts.sent == []


def test_a_tunnel_that_stays_dead_becomes_an_outage_after_the_grace_and_the_threshold(
    gluetun, qbit_server
):
    dog, clock, alerts = make(gluetun, qbit_server, grace=180.0, threshold=3)
    steps(dog, clock, 5)
    no_tunnel(gluetun)
    steps(dog, clock, 170)  # still within the 180 s grace: nothing happens
    snap = dog.snapshot()
    assert (snap["state"], snap["tunnel"]) == ("ok", "reconnecting")
    assert snap["tunnel_since"] is not None
    assert pauses(qbit_server) == 0
    assert alerts.sent == []
    steps(dog, clock, 20)  # the grace is over, then the threshold of 3 polls
    snap = dog.snapshot()
    assert snap["state"] == "down"
    assert (snap["cause"], snap["tunnel"]) == ("tunnel", "down")
    assert snap["reason"] == wd.REASON_NO_ADDRESS
    assert snap["reason"].startswith("Tunnel not connected")
    assert snap["paused"] is True
    assert pauses(qbit_server) == 1, "the torrents were paused, like for a stopped VPN"
    assert alerts.kinds() == ["down"]
    assert alerts.sent[0].urgent is True
    assert "Tunnel not connected" in alerts.sent[0].message


def test_a_normal_reconnect_under_the_grace_does_not_alert_or_pause(gluetun, qbit_server):
    """The 2026-11-09 reconnect took about two minutes: running, no tunnel, then connected."""
    dog, clock, alerts = make(gluetun, qbit_server, grace=180.0)
    steps(dog, clock, 5)
    no_tunnel(gluetun)
    steps(dog, clock, 120)
    assert dog.snapshot()["tunnel"] == "reconnecting"
    gluetun.public = {"public_ip": "185.107.12.34"}
    steps(dog, clock, 5)
    snap = dog.snapshot()
    assert (snap["state"], snap["tunnel"], snap["tunnel_since"]) == ("ok", "connected", None)
    assert pauses(qbit_server) == 0
    assert alerts.sent == []


def test_a_stopped_vpn_still_counts_after_the_threshold_without_any_grace(gluetun, qbit_server):
    dog, clock, alerts = make(gluetun, qbit_server, grace=180.0, threshold=3)
    steps(dog, clock, 3)
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    snap = dog.snapshot()
    assert (snap["state"], snap["cause"]) == ("down", "stopped")
    assert snap["reason"] == "the VPN is not running"
    assert alerts.kinds() == ["down"]


def test_a_dead_control_server_still_counts_as_before(gluetun, qbit_server):
    dog, clock, _ = make(gluetun, qbit_server, threshold=3)
    steps(dog, clock, 3)
    gluetun.stop()
    steps(dog, clock, 3)
    assert dog.snapshot()["state"] == "down"
    assert dog.snapshot()["cause"] == "unreachable"


def test_the_tunnel_coming_back_ends_the_outage_with_the_back_alert(gluetun, qbit_server):
    dog, clock, alerts = make(gluetun, qbit_server, grace=10.0, threshold=3)
    steps(dog, clock, 3)
    no_tunnel(gluetun)
    steps(dog, clock, 20)
    assert dog.snapshot()["state"] == "down"
    gluetun.public = {"public_ip": "185.107.12.34"}
    steps(dog, clock, 3)
    snap = dog.snapshot()
    assert (snap["state"], snap["cause"], snap["tunnel"], snap["tunnel_since"]) == (
        "ok",
        "",
        "connected",
        None,
    )
    assert alerts.kinds() == ["down", "recovered"]
    assert "tunnel is connected again" in alerts.sent[1].message
    assert alerts.sent[1].urgent is False


def test_a_probe_that_gets_nothing_through_counts_although_the_address_is_there(
    gluetun, qbit_server
):
    """The tunnel died after it was up: gluetun still shows the old address."""
    probe = FakeProbe(True)
    dog, clock, alerts = make(gluetun, qbit_server, probe=probe, grace=10.0, threshold=3)
    steps(dog, clock, 3)
    probe.ok = False
    dog.run_probe()
    steps(dog, clock, 20)
    assert dog.snapshot()["state"] == "ok", "one failed probe is not enough (confirm = 2)"
    dog.run_probe()
    steps(dog, clock, 20)
    snap = dog.snapshot()
    assert (snap["state"], snap["cause"], snap["probe"]) == ("down", "tunnel", "failing")
    assert snap["reason"] == wd.REASON_NO_TRAFFIC
    assert pauses(qbit_server) == 1
    probe.ok = True
    dog.run_probe()
    steps(dog, clock, 3)
    assert dog.snapshot()["state"] == "ok"
    assert alerts.kinds() == ["down", "recovered"]


def test_one_failed_probe_between_good_ones_never_counts(gluetun, qbit_server):
    probe = FakeProbe(True)
    dog, clock, alerts = make(gluetun, qbit_server, probe=probe, grace=0.0, threshold=1)
    for ok in (True, False, True, False, True):
        probe.ok = ok
        dog.run_probe()
        steps(dog, clock, 3)
    assert dog.snapshot()["state"] == "ok"
    assert dog.snapshot()["probe"] == "ok"
    assert alerts.sent == []


def test_without_a_probe_only_the_address_counts(gluetun, qbit_server):
    dog, clock, _ = make(gluetun, qbit_server, probe=None)
    steps(dog, clock, 5)
    snap = dog.snapshot()
    assert (snap["state"], snap["probe"]) == ("ok", "off")


def test_a_key_that_may_not_read_the_address_weakens_the_check_but_does_not_blind_the_watchdog(
    gluetun, qbit_server, caplog
):
    gluetun.public_http_status = 404  # an old key: the route is not in its role
    dog, clock, _ = make(gluetun, qbit_server, threshold=3)
    gluetun.hits.clear()
    steps(dog, clock, 10)
    assert dog.misconfigured is None
    assert dog.snapshot()["state"] == "ok"
    assert dog.ip_unavailable is not None
    assert [h["path"] for h in gluetun.hits].count("/v1/publicip/ip") == 1, (
        "asked once, not per poll"
    )
    gluetun.status = "stopped"  # the status route still protects
    steps(dog, clock, 4)
    assert dog.snapshot()["state"] == "down"
    assert "weaker" in caplog.text


def test_a_failing_address_route_that_is_not_an_auth_problem_counts_as_unreachable(
    gluetun, qbit_server
):
    gluetun.public_http_status = 500
    dog, clock, _ = make(gluetun, qbit_server, threshold=3)
    steps(dog, clock, 4)
    snap = dog.snapshot()
    assert (snap["state"], snap["cause"]) == ("down", "unreachable")


def test_the_snapshot_holds_no_address_and_no_secret(gluetun, qbit_server):
    dog, clock, _ = make(gluetun, qbit_server, probe=FakeProbe(True), grace=0.0)
    no_tunnel(gluetun)
    steps(dog, clock, 5)
    text = str(dog.snapshot())
    assert "185.107" not in text
    assert API_KEY not in text


# --- the probe loop ----------------------------------------------------------------------------------


def test_the_probe_loop_runs_in_its_own_thread_while_the_watchdog_runs(gluetun, qbit_server):
    probe = FakeProbe(True)
    dog, _, _ = make(gluetun, qbit_server, probe=probe, probe_interval=0.05)
    stop = threading.Event()
    thread = threading.Thread(target=dog.run, args=(stop, None), daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while probe.calls < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert probe.calls >= 2
    assert dog.probe_state == "ok"


# --- the restart of a stuck VPN ------------------------------------------------------------------------


def drive_until_down(dog, clock, gluetun, seconds=30):
    steps(dog, clock, 3)
    no_tunnel(gluetun)
    steps(dog, clock, seconds)
    assert dog.snapshot()["state"] == "down"


def test_after_the_configured_time_the_watchdog_restarts_the_vpn_and_says_so(gluetun, qbit_server):
    dog, clock, alerts = make(gluetun, qbit_server, grace=10.0, restart_after=300.0)
    drive_until_down(dog, clock, gluetun)
    assert gluetun.puts == [], "not yet: the tunnel has been dead for 30 s"
    steps(dog, clock, 260)
    assert gluetun.puts == [], "290 s of dead tunnel are less than 300"
    steps(dog, clock, 20)
    wait_for_restart(dog)
    assert gluetun.puts == ["stopped", "running"]
    assert "restart" in alerts.kinds()
    restart = next(a for a in alerts.sent if a.kind == "restart")
    assert restart.title == "VPN restarted by the watchdog"
    assert restart.urgent is False
    assert API_KEY not in restart.message
    steps(dog, clock, 5)  # the fake gave the tunnel back: recovered
    assert dog.snapshot()["state"] == "ok"
    assert alerts.kinds() == ["down", "restart", "recovered"]
    assert "restarted the VPN 1 time" in alerts.sent[-1].message
    assert dog.restarts == 0, "the counters belong to one outage"
    assert dog.cause == ""


def test_a_vpn_that_somebody_stopped_is_never_started_by_the_watchdog(gluetun, qbit_server):
    dog, clock, _ = make(gluetun, qbit_server, grace=10.0, restart_after=120.0)
    steps(dog, clock, 3)
    gluetun.status = "stopped"
    steps(dog, clock, 1000)
    wait_for_restart(dog)
    assert gluetun.puts == []
    assert dog.snapshot()["cause"] == "stopped"


def test_the_restart_is_off_with_zero(gluetun, qbit_server):
    dog, clock, _ = make(gluetun, qbit_server, grace=10.0, restart_after=0.0)
    drive_until_down(dog, clock, gluetun)
    steps(dog, clock, 5000)
    assert gluetun.puts == []
    assert dog.snapshot()["restart"] == "off"


def test_the_restarts_are_spaced_out_doubling_and_stop_at_the_maximum(gluetun, qbit_server):
    gluetun.heal_on_start = False  # the new server does not help either
    dog, clock, alerts = make(gluetun, qbit_server, grace=10.0, restart_after=300.0, restart_max=3)
    drive_until_down(dog, clock, gluetun)
    times = []
    for _ in range(4000):  # one second per step
        before = len(gluetun.puts)
        dog.step()
        clock.now += 1.0
        wait_for_restart(dog)
        if len(gluetun.puts) > before:
            times.append(clock.now)
            no_tunnel(gluetun)  # still dead
    assert gluetun.puts == ["stopped", "running"] * 3
    gaps = [b - a for a, b in pairwise(times)]
    assert gaps[0] == pytest.approx(600, abs=3), "the second restart waits twice as long"
    assert gaps[1] == pytest.approx(1200, abs=3), "the third four times as long"
    assert alerts.kinds().count("restart") == 3
    assert alerts.kinds().count("restart_gave_up") == 1
    gave_up = next(a for a in alerts.sent if a.kind == "restart_gave_up")
    assert gave_up.urgent is True
    assert dog.snapshot()["state"] == "down", "still protected: paused, alerted, shown down"


def test_a_watchdog_that_may_not_restart_says_so_once_and_carries_on(gluetun, qbit_server, caplog):
    gluetun.put_http_status = 403
    dog, clock, alerts = make(gluetun, qbit_server, grace=10.0, restart_after=120.0)
    drive_until_down(dog, clock, gluetun)
    steps(dog, clock, 200)
    wait_for_restart(dog)
    steps(dog, clock, 3000)
    wait_for_restart(dog)
    puts = [h for h in gluetun.hits if h.get("method") == "PUT"]
    assert len(puts) == 1, "asked once; a refusal is not retried every few minutes"
    assert alerts.kinds().count("restart_denied") == 1
    assert dog.snapshot()["restart"] == "denied"
    assert dog.snapshot()["state"] == "down", "the outage is still handled"
    assert pauses(qbit_server) == 1
    assert "make watchdog-key" in caplog.text


def test_a_vpn_that_was_stopped_and_cannot_be_started_is_retried_and_alerted(gluetun, qbit_server):
    gluetun.fail_start = 100  # every start fails with a 500 until the test lets it work
    dog, clock, alerts = make(gluetun, qbit_server, grace=10.0, restart_after=120.0)
    drive_until_down(dog, clock, gluetun)
    steps(dog, clock, 130)
    wait_for_restart(dog)
    assert gluetun.puts == ["stopped"], "stopped, and starting it failed"
    assert "restart_failed" in alerts.kinds()
    assert next(a for a in alerts.sent if a.kind == "restart_failed").urgent is True
    gluetun.fail_start = 0
    steps(dog, clock, 40)  # the retry comes after 30 s, also while the status says stopped
    wait_for_restart(dog)
    assert gluetun.puts[-1] == "running"


def test_a_restart_never_blocks_the_polls(gluetun, qbit_server):
    """It runs on its own thread: a slow gluetun cannot delay the pause."""
    dog, clock, _ = make(gluetun, qbit_server, grace=10.0, restart_after=120.0)
    release = threading.Event()
    original = dog.control.set_vpn_status

    def slow(status):
        release.wait(5)
        original(status)

    dog.control.set_vpn_status = slow  # type: ignore[method-assign]
    drive_until_down(dog, clock, gluetun)
    started = time.monotonic()
    steps(dog, clock, 130)
    assert time.monotonic() - started < 3, "the polls went on while the restart waited"
    assert dog._restarting is True
    release.set()
    wait_for_restart(dog)


# --- the tunnel probe itself -----------------------------------------------------------------------------


class FakeProxy:
    """A tiny HTTP proxy that answers CONNECT: 200 for the targets it allows, 502 for the rest."""

    def __init__(self, allow: set[str]) -> None:
        self.allow = allow
        self.requests: list[str] = []
        self.leftover: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                line = data.split(b"\r\n", 1)[0].decode(errors="replace")
                self.requests.append(line)
                target = line.split(" ")[1] if line.startswith("CONNECT ") else ""
                if target in self.allow:
                    conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    with contextlib.suppress(OSError):
                        self.leftover.append(conn.recv(100))  # nothing must come after CONNECT
                else:
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def proxy():
    made: list[FakeProxy] = []

    def build(allow):
        fake = FakeProxy(set(allow))
        made.append(fake)
        return fake

    yield build
    for fake in made:
        fake.close()


def probe_for(fake: FakeProxy, targets="1.1.1.1:443,8.8.8.8:443", **kw) -> TunnelProbe:
    return TunnelProbe(f"http://127.0.0.1:{fake.port}", parse_targets(targets), **kw)


def test_one_reachable_target_is_enough(proxy):
    fake = proxy({"8.8.8.8:443"})
    probe = probe_for(fake, timeout=2)
    assert probe.check() is True
    assert [r.rsplit(" ", 1)[0] for r in fake.requests] == [
        "CONNECT 1.1.1.1:443",
        "CONNECT 8.8.8.8:443",
    ]
    assert probe.last_error == ""


def test_the_first_target_that_works_ends_the_probe(proxy):
    fake = proxy({"1.1.1.1:443", "8.8.8.8:443"})
    assert probe_for(fake, timeout=2).check() is True
    assert [r.rsplit(" ", 1)[0] for r in fake.requests] == ["CONNECT 1.1.1.1:443"], (
        "the second is not even asked"
    )


def test_nothing_reachable_is_a_failure_that_names_no_address(proxy):
    fake = proxy(set())
    probe = probe_for(fake, timeout=2)
    assert probe.check() is False
    assert "no target reachable" in probe.last_error
    assert "1.1.1.1" not in probe.last_error
    assert "8.8.8.8" not in probe.last_error


def test_an_unreachable_proxy_is_a_failure_not_an_exception():
    probe = TunnelProbe(f"http://127.0.0.1:{free_port()}", parse_targets("1.1.1.1:443"), timeout=1)
    assert probe.check() is False


def test_a_proxy_that_never_answers_times_out(proxy):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        slow = TunnelProbe(
            f"http://127.0.0.1:{sock.getsockname()[1]}", parse_targets("1.1.1.1:443"), timeout=0.3
        )
        started = time.monotonic()
        assert slow.check() is False
    assert time.monotonic() - started < 3


def test_the_probe_sends_nothing_but_the_connect_request(proxy):
    fake = proxy({"1.1.1.1:443"})
    assert probe_for(fake, targets="1.1.1.1:443", timeout=2).check() is True
    time.sleep(0.2)
    assert fake.leftover in ([], [b""]), "no byte follows the CONNECT: the probe closes at once"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.1.1.1:443", (("1.1.1.1", 443),)),
        ("1.1.1.1:443, 8.8.8.8:53", (("1.1.1.1", 443), ("8.8.8.8", 53))),
        ("cloudflare.com:443 github.com:443", (("cloudflare.com", 443), ("github.com", 443))),
        ("[2606:4700::1111]:443", (("2606:4700::1111", 443),)),
    ],
)
def test_targets_are_host_port_lists(text, expected):
    assert parse_targets(text) == expected


@pytest.mark.parametrize(
    "text", ["", " ", "1.1.1.1", "1.1.1.1:0", "1.1.1.1:70000", "a b:443", "x:y"]
)
def test_bad_targets_name_the_entry_not_the_value(text):
    with pytest.raises(ValueError, match=r"entry|at least") as error:
        parse_targets(text)
    assert "1.1.1.1:70000" not in str(error.value)


@pytest.mark.parametrize(
    "url", ["", "gluetun:8888", "https://gluetun:8888", "http://u:p@h:1", "http://h:1/x"]
)
def test_the_proxy_must_be_a_plain_http_address(url):
    with pytest.raises(ValueError, match="must be http"):
        parse_proxy(url)


def test_the_proxy_address_is_split():
    assert parse_proxy("http://gluetun:8888") == ("gluetun", 8888)
    assert parse_proxy("http://gluetun") == ("gluetun", 80)


# --- the configuration -----------------------------------------------------------------------------------

ENV = {"GLUETUN_CONTROL_APIKEY": "k" * 24, "QBIT_PASSWORD": "pw"}


def test_the_defaults_are_a_three_minute_grace_a_probe_and_a_fifteen_minute_restart():
    cfg = WatchdogConfig.from_env(ENV)
    assert cfg.tunnel_grace == 180.0
    assert (cfg.probe_proxy, cfg.probe_interval, cfg.probe_confirm) == (
        "http://gluetun:8888",
        10.0,
        2,
    )
    assert cfg.probe_targets == "1.1.1.1:443,8.8.8.8:443"
    assert (cfg.restart_after, cfg.restart_max) == (900.0, 5)


def test_an_empty_probe_proxy_switches_the_probe_off_but_unset_is_the_default():
    assert WatchdogConfig.from_env({**ENV, "TUNNEL_PROBE_PROXY": ""}).probe_proxy == ""
    assert WatchdogConfig.from_env(ENV).probe_proxy == "http://gluetun:8888"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("TUNNEL_PROBE_PROXY", "gluetun:8888", "TUNNEL_PROBE_PROXY"),
        ("TUNNEL_PROBE_TARGETS", "1.1.1.1", "TUNNEL_PROBE_TARGETS"),
        ("TUNNEL_PROBE_INTERVAL", "2", "TUNNEL_PROBE_INTERVAL"),
        ("TUNNEL_PROBE_INTERVAL", "x", "TUNNEL_PROBE_INTERVAL"),
        ("TUNNEL_PROBE_CONFIRM", "0", "TUNNEL_PROBE_CONFIRM"),
        ("WATCHDOG_TUNNEL_GRACE", "-1", "WATCHDOG_TUNNEL_GRACE"),
        ("WATCHDOG_TUNNEL_GRACE", "99999", "WATCHDOG_TUNNEL_GRACE"),
        ("WATCHDOG_RESTART_AFTER", "60", "WATCHDOG_RESTART_AFTER"),
        ("WATCHDOG_RESTART_MAX", "0", "WATCHDOG_RESTART_MAX"),
    ],
)
def test_bad_settings_are_refused_with_the_name_of_the_setting(name, value, message):
    with pytest.raises(ConfigError, match=message):
        WatchdogConfig.from_env({**ENV, name: value})


def test_zero_turns_the_probe_the_grace_and_the_restart_off():
    cfg = WatchdogConfig.from_env(
        {
            **ENV,
            "TUNNEL_PROBE_INTERVAL": "0",
            "WATCHDOG_TUNNEL_GRACE": "0",
            "WATCHDOG_RESTART_AFTER": "0",
        }
    )
    assert (cfg.probe_interval, cfg.tunnel_grace, cfg.restart_after) == (0.0, 0.0, 0.0)


# --- the probe helpers the VM scenario uses --------------------------------------------------------------


def probe_env(gluetun, qbit_server, port=0):
    return {
        "GLUETUN_CONTROL_URL": gluetun.url,
        "GLUETUN_CONTROL_APIKEY": API_KEY,
        "QBIT_PASSWORD": PASSWORD,
        "QBIT_USER": USER,
        "QBIT_URL": qbit_server.url,
        "WATCHDOG_STATUS_PORT": str(port),
    }


def test_the_gluetun_probes_report_the_status_word_and_only_whether_there_is_an_address(
    gluetun, qbit_server
):
    env = probe_env(gluetun, qbit_server)
    assert wd.probe("gluetun", env=env) == (0, "running")
    assert wd.probe("address", env=env) == (0, "present")
    no_tunnel(gluetun)
    assert wd.probe("gluetun", env=env) == (0, "running"), "the incident: running, no tunnel"
    assert wd.probe("address", env=env) == (0, "none")
    gluetun.status = "stopped"
    assert wd.probe("gluetun", env=env) == (0, "stopped")


@pytest.mark.security
def test_the_gluetun_probe_output_holds_no_address_and_no_key(gluetun, qbit_server):
    env = probe_env(gluetun, qbit_server)
    for what in ("gluetun", "address"):
        _, text = wd.probe(what, env=env)
        assert "185.107" not in text
        assert API_KEY not in text


def test_the_gluetun_probes_say_unreachable_instead_of_raising(gluetun, qbit_server):
    env = {
        **probe_env(gluetun, qbit_server),
        "GLUETUN_CONTROL_URL": f"http://127.0.0.1:{free_port()}",
    }
    assert wd.probe("gluetun", env=env) == (1, "unreachable")
    assert wd.probe("address", env=env) == (1, "unreachable")


def test_the_why_probes_read_the_status_page(gluetun, qbit_server):
    dog, clock, _ = make(gluetun, qbit_server, grace=5.0, probe=FakeProbe(True))
    server = wd.StatusServer(dog, 0)
    server.start()
    try:
        env = probe_env(gluetun, qbit_server, server.port)
        steps(dog, clock, 2)
        assert wd.probe("cause", env=env) == (0, "")
        assert wd.probe("tunnel", env=env) == (0, "connected")
        no_tunnel(gluetun)
        steps(dog, clock, 30)
        assert wd.probe("state", env=env) == (0, "down")
        assert wd.probe("cause", env=env) == (0, "tunnel")
        assert wd.probe("tunnel", env=env) == (0, "down")
        code, reason = wd.probe("reason", env=env)
        assert code == 0
        assert reason.startswith("Tunnel not connected")
        assert wd.probe("restart", env=env) == (0, "off")
        assert wd.probe("restarts", env=env) == (0, "0")
    finally:
        server.stop()
    assert wd.probe("cause", env=probe_env(gluetun, qbit_server, free_port())) == (1, "unreachable")


@pytest.mark.parametrize(
    "target", ["192.168.1.1:443", "10.0.0.1:80", "127.0.0.1:443", "[::1]:443", "169.254.1.1:80"]
)
def test_a_private_probe_target_is_refused_because_it_would_pass_without_the_tunnel(target):
    with pytest.raises(ValueError, match="not a public address"):
        parse_targets(target)
    with pytest.raises(ConfigError, match="TUNNEL_PROBE_TARGETS"):
        WatchdogConfig.from_env({**ENV, "TUNNEL_PROBE_TARGETS": target})


def test_one_lost_answer_of_the_control_server_does_not_restart_the_restart_timer(
    gluetun, qbit_server
):
    dog, clock, _ = make(gluetun, qbit_server, grace=10.0, restart_after=300.0)
    drive_until_down(dog, clock, gluetun)
    bad_since = dog._tunnel_bad_since
    steps(dog, clock, 100)
    gluetun.http_status = 500  # the control server blips
    steps(dog, clock, 2)
    gluetun.http_status = 200
    steps(dog, clock, 5)
    assert dog._tunnel_bad_since == bad_since, "still the first poll of this tunnel outage"
    steps(dog, clock, 200)
    wait_for_restart(dog)
    assert gluetun.puts == ["stopped", "running"], "the restart came 300 s after the tunnel died"


def test_a_stop_whose_answer_is_lost_is_followed_by_a_start(gluetun, qbit_server):
    """The PUT "stopped" may reach gluetun although the answer never comes back."""
    dog, clock, _ = make(gluetun, qbit_server, grace=10.0, restart_after=120.0)
    drive_until_down(dog, clock, gluetun)
    real = dog.control.set_vpn_status

    def flaky(status):
        if status == "stopped":
            real(status)
            raise GluetunUnreachable("the answer got lost")
        real(status)

    dog.control.set_vpn_status = flaky  # type: ignore[method-assign]
    steps(dog, clock, 130)
    wait_for_restart(dog)
    steps(dog, clock, 40)
    wait_for_restart(dog)
    assert gluetun.puts[0] == "stopped"
    assert "running" in gluetun.puts, "the VPN that may have been stopped is started again"
