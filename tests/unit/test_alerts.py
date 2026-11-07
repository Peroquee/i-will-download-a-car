"""app/ytp/alerts.py, the watchdog's alerts and status page, and the app's /api/vpn badge."""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.helpers.fake_alert_target import FakeAlertTarget
from tests.helpers.fake_gluetun import API_KEY, FakeGluetun
from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from ytp import watchdog as wd
from ytp.alerts import Alert, Alerter
from ytp.clients.gluetun import GluetunControl
from ytp.clients.qbittorrent import QbitClient
from ytp.clients.watchdog import WatchdogStatusClient
from ytp.config import Settings
from ytp.main import create_app
from ytp.watchdog import StatusServer, Watchdog, WatchdogConfig

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
NOW = Alert(
    "down",
    "VPN is down",
    "Time: 2026-11-01T10:00:00+00:00\nReason: x",
    "2026-11-01T10:00:00+00:00",
    True,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def target() -> Iterator[FakeAlertTarget]:
    server = FakeAlertTarget().start()
    yield server
    server.stop()


@pytest.fixture
def gluetun() -> Iterator[FakeGluetun]:
    server = FakeGluetun().start()
    yield server
    server.stop()


@pytest.fixture
def qbit_server() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    yield server
    server.stop()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Wall:
    """A wall clock that moves with the fake monotonic clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def __call__(self) -> datetime:
        return datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC).replace(microsecond=0) + (
            datetime.fromtimestamp(self.clock.now - 1000.0, UTC) - datetime.fromtimestamp(0, UTC)
        )


# --- the sender ----------------------


def test_ntfy_format_is_a_plain_text_post_with_title_priority_and_tags(target):
    alerter = Alerter(target.url, fmt="ntfy")
    alerter.notify(NOW)
    assert target.wait_for(1)
    (got,) = target.received
    assert got["path"] == "/vpn-alerts-secret-topic"
    assert got["body"].decode() == NOW.message
    assert got["headers"]["title"] == "VPN is down"
    assert got["headers"]["priority"] == "urgent"
    assert got["headers"]["tags"] == "rotating_light"
    assert got["headers"]["content-type"].startswith("text/plain")
    assert "authorization" not in got["headers"]


def test_a_recovery_is_not_urgent(target):
    Alerter(target.url).notify(Alert("recovered", "VPN is back", "x", "t", urgent=False))
    assert target.wait_for(1)
    assert target.received[0]["headers"]["priority"] == "default"
    assert target.received[0]["headers"]["tags"] == "white_check_mark"


def test_webhook_format_is_json(target):
    Alerter(target.url, fmt="webhook").notify(NOW)
    assert target.wait_for(1)
    (body,) = target.json_bodies()
    assert body == {
        "event": "down",
        "title": "VPN is down",
        "message": NOW.message,
        "timestamp": NOW.timestamp,
        "urgent": True,
    }
    assert target.received[0]["headers"]["content-type"] == "application/json"


def test_the_token_is_sent_as_a_bearer_header(target):
    Alerter(target.url, token="the-token-value").notify(NOW)
    assert target.wait_for(1)
    assert target.received[0]["headers"]["authorization"] == "Bearer the-token-value"


def test_non_ascii_titles_cannot_break_the_ntfy_header(target):
    Alerter(target.url).notify(
        Alert("x", "VPN ist weg - Größe", "Nachricht mit Umlauten: äöü", "t")
    )
    assert target.wait_for(1)
    assert target.received[0]["body"].decode() == "Nachricht mit Umlauten: äöü"


def test_alerts_arrive_in_order(target):
    alerter = Alerter(target.url)
    for n in range(5):
        alerter.notify(Alert("k", f"title {n}", f"message {n}", "t"))
    assert target.wait_for(5)
    assert [r["body"].decode() for r in target.received] == [f"message {n}" for n in range(5)]


def test_notify_does_not_wait_for_a_slow_target(target):
    target.delay = 1.0
    alerter = Alerter(target.url)
    started = time.monotonic()
    for _ in range(3):
        alerter.notify(NOW)
    assert time.monotonic() - started < 0.3


def test_a_failing_target_is_retried_then_given_up_without_raising(target, caplog):
    target.status = 500
    alerter = Alerter(target.url, retry_pauses=(0.0, 0.0), sleep=lambda s: None)
    with caplog.at_level(logging.ERROR, logger="ytp.alerts"):
        alerter.notify(NOW)
        alerter.close()
    assert target.attempts == 3
    assert alerter.dropped == 1
    assert "giving up" in caplog.text


def test_a_target_that_recovers_gets_the_alert(target):
    target.fail_first = 2
    alerter = Alerter(target.url, retry_pauses=(0.0, 0.0, 0.0), sleep=lambda s: None)
    alerter.notify(NOW)
    assert target.wait_for(1)
    alerter.close()
    assert target.attempts == 3
    assert alerter.sent == 1


def test_an_unreachable_target_never_raises():
    alerter = Alerter(f"http://127.0.0.1:{free_port()}/topic", retry_pauses=(), timeout=0.5)
    alerter.notify(NOW)
    alerter.close(5)
    assert alerter.dropped == 1


@pytest.mark.security
def test_the_url_path_and_token_never_reach_the_log(target, caplog):
    alerter = Alerter(
        target.url, token="bearer-secret-value", retry_pauses=(), sleep=lambda s: None
    )
    target.status = 500
    with caplog.at_level(logging.DEBUG):
        alerter.notify(NOW)
        alerter.close()
    target.status = 200
    ok = Alerter(target.url, token="bearer-secret-value")
    with caplog.at_level(logging.DEBUG):
        ok.notify(NOW)
        ok.close()
    everything = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert "vpn-alerts-secret-topic" not in everything
    assert "bearer-secret-value" not in everything
    assert "vpn-alerts-secret-topic" not in repr(alerter)
    assert {getattr(r, "host", None) for r in caplog.records} >= {"127.0.0.1"}, (
        "only the host is named"
    )


@pytest.mark.parametrize("url", ["", "ftp://x/y", "ntfy.sh/topic", "http://"])
def test_a_bad_url_is_refused(url):
    with pytest.raises(ValueError, match="ALERT_URL"):
        Alerter(url)


def test_a_bad_format_is_refused(target):
    with pytest.raises(ValueError, match="ALERT_FORMAT"):
        Alerter(target.url, fmt="telegram")


# --- the watchdog raises them ----------------------


def make(gluetun, qbit_server, target, **config) -> tuple[Watchdog, Clock, Alerter]:
    clock = Clock()
    cfg = WatchdogConfig(
        control_url=gluetun.url, api_key=API_KEY, qbit_url=qbit_server.url, qbit_password=PASSWORD,
        threshold=2, **config,
    )  # fmt: skip
    alerter = Alerter(target.url, fmt="webhook")
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY),
        QbitClient(qbit_server.url, USER, PASSWORD, timeout=2),
        cfg,
        clock=clock,
        alerter=alerter,
        wall=Wall(clock),
    )
    return dog, clock, alerter


def steps(dog: Watchdog, clock: Clock, count: int) -> None:
    for _ in range(count):
        dog.step()
        clock.now += 1.0


def test_down_and_recovery_each_send_one_alert(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    gluetun.status = "stopped"
    steps(dog, clock, 10)
    assert target.wait_for(1)
    gluetun.status = "running"
    steps(dog, clock, 3)
    assert target.wait_for(2)
    time.sleep(0.2)
    down, back = target.json_bodies()
    assert (down["event"], down["urgent"]) == ("down", True)
    assert "Reason: the VPN is not running" in down["message"]
    assert "All torrents were paused" in down["message"]
    assert "Time: 2026-11-01T10:00" in down["message"]
    assert (back["event"], back["urgent"]) == ("recovered", False)
    assert "running again after" in back["message"]
    assert "stay paused" in back["message"]
    assert len(target.received) == 2


def test_a_blip_sends_nothing(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    gluetun.status = "stopped"
    steps(dog, clock, 1)
    gluetun.status = "running"
    steps(dog, clock, 5)
    time.sleep(0.3)
    assert target.received == []


def test_a_long_outage_sends_one_down_alert_not_one_per_second(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    gluetun.status = "stopped"
    steps(dog, clock, 40)
    assert target.wait_for(1)
    time.sleep(0.3)
    assert len(target.received) == 1


def test_a_flap_while_down_does_not_repeat_the_down_alert(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target, resume_after=30)
    dog.step()
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    gluetun.status = "running"
    steps(dog, clock, 5)
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    time.sleep(0.3)
    assert [b["event"] for b in target.json_bodies()] == ["down"]


def test_with_auto_resume_the_recovery_alert_comes_after_the_resume(gluetun, qbit_server, target):
    qbit_server.torrents = [
        {"hash": "a" * 40, "name": "a", "state": "downloading", "progress": 0.5}
    ]
    dog, clock, _ = make(gluetun, qbit_server, target, resume_after=10)
    dog.step()
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    gluetun.status = "running"
    steps(dog, clock, 5)
    assert target.wait_for(1)
    time.sleep(0.2)
    assert [b["event"] for b in target.json_bodies()] == ["down"], "not yet stable"
    steps(dog, clock, 10)
    assert target.wait_for(2)
    assert target.json_bodies()[1]["event"] == "recovered"
    assert "1 torrent(s) were resumed" in target.json_bodies()[1]["message"]


def test_the_down_alert_says_when_pausing_is_still_pending(gluetun, target):
    clock = Clock()
    cfg = WatchdogConfig(api_key=API_KEY, qbit_password=PASSWORD, threshold=1)
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY),
        QbitClient(f"http://127.0.0.1:{free_port()}", USER, PASSWORD, timeout=0.3),
        cfg, clock=clock, alerter=Alerter(target.url, fmt="webhook"), wall=Wall(clock),
    )  # fmt: skip
    gluetun.status = "stopped"
    steps(dog, clock, 2)
    assert target.wait_for(1)
    assert "still being tried" in target.json_bodies()[0]["message"]


def test_a_blind_watchdog_alerts_once_and_again_when_it_sees(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    good = dog.control
    dog.control = GluetunControl(gluetun.url, "wrong key")
    steps(dog, clock, 20)
    assert target.wait_for(1)
    dog.control = good
    steps(dog, clock, 2)
    assert target.wait_for(2)
    time.sleep(0.2)
    blind, sighted = target.json_bodies()
    assert (blind["event"], blind["urgent"]) == ("blind", True)
    assert "make watchdog-key" in blind["message"]
    assert (sighted["event"], sighted["urgent"]) == ("sighted", False)


@pytest.mark.security
def test_messages_hold_no_secrets_and_no_addresses(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    gluetun.status = "stopped"
    steps(dog, clock, 4)
    gluetun.status = "running"
    steps(dog, clock, 3)
    assert target.wait_for(2)
    text = json.dumps(target.json_bodies())
    for secret in (API_KEY, PASSWORD, "vpn-alerts-secret-topic"):
        assert secret not in text
    import re

    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), "no IP address in a message"


def test_without_an_alerter_nothing_changes(gluetun, qbit_server):
    clock = Clock()
    cfg = WatchdogConfig(
        api_key=API_KEY, qbit_password=PASSWORD, qbit_url=qbit_server.url, threshold=1
    )
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY), QbitClient(qbit_server.url, USER, PASSWORD), cfg,
        clock=clock,
    )  # fmt: skip
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    assert dog.state == "unhealthy"


# --- the snapshot and the status page ----------------------


def test_the_snapshot_follows_the_state(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    assert dog.snapshot()["state"] == "starting"
    steps(dog, clock, 1)
    snap = dog.snapshot()
    assert snap["state"] == "ok"
    assert snap["paused"] is False
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    snap = dog.snapshot()
    assert (snap["state"], snap["paused"], snap["reason"]) == (
        "down",
        True,
        "the VPN is not running",
    )
    assert snap["since"].startswith("2026-11-01T10:00")
    gluetun.status = "running"
    steps(dog, clock, 2)
    assert dog.snapshot()["state"] == "ok"
    dog.control = GluetunControl(gluetun.url, "wrong")
    steps(dog, clock, 1)
    assert dog.snapshot()["state"] == "blind"


def fetch(url: str, method: str = "GET"):
    request = urllib.request.Request(url, method=method, data=b"x" if method != "GET" else None)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_the_status_server_serves_the_snapshot_read_only(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    steps(dog, clock, 1)
    server = StatusServer(dog, 0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        status, body = fetch(f"{base}/status")
        assert status == 200
        assert body["state"] == "ok"
        assert fetch(f"{base}/other")[0] == 404
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert fetch(f"{base}/status", method)[0] == 405
    finally:
        server.stop()


@pytest.mark.security
def test_the_status_holds_no_secrets_or_addresses(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    text = json.dumps(dog.snapshot())
    for secret in (API_KEY, PASSWORD, "127.0.0.1", "vpn-alerts-secret-topic"):
        assert secret not in text


# --- the app's side: /api/vpn ----------------------


def test_the_status_client_passes_on_only_the_known_fields(gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    steps(dog, clock, 1)
    server = StatusServer(dog, 0)
    server.start()
    try:
        got = WatchdogStatusClient(f"http://127.0.0.1:{server.port}").fetch_status()
    finally:
        server.stop()
    assert set(got) == {
        "state", "since", "reason", "paused", "last_poll", "auto_resume",
        "egress", "egress_checked", "egress_country",
    }  # fmt: skip
    assert got["state"] == "ok"


def test_an_unreachable_or_odd_watchdog_is_unknown_not_an_error():
    client = WatchdogStatusClient(f"http://127.0.0.1:{free_port()}", timeout=0.5)
    assert client.fetch_status()["state"] == "unknown"


@pytest.mark.parametrize("body", [b"", b"nope", b"[]", b'{"state": "exploding"}', b'{"state": 7}'])
def test_an_unreadable_status_is_unknown(body):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()
    try:
        got = WatchdogStatusClient(f"http://127.0.0.1:{server.server_address[1]}").fetch_status()
    finally:
        server.shutdown()
        server.server_close()
    assert got["state"] == "unknown"


class FakeInternet:
    """The status endpoint must not do a real DNS + TCP check in the unit suite."""

    def refresh(self):
        return {"state": "ok", "checked_at": "2026-11-07T12:00:00Z"}


def make_app(tmp_path: Path, status_url: str) -> TestClient:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {
        **ENV,
        "DATA_ROOT": str(tmp_path),
        "CONFIG_DIR": str(config),
        "WATCHDOG_STATUS_URL": status_url,
    }
    client = TestClient(create_app(Settings.from_env(values)))
    client.app.state.internet = FakeInternet()
    return client


def test_api_vpn_shows_the_watchdogs_state(tmp_path, gluetun, qbit_server, target):
    dog, clock, _ = make(gluetun, qbit_server, target)
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    server = StatusServer(dog, 0)
    server.start()
    try:
        with make_app(tmp_path, f"http://127.0.0.1:{server.port}") as client:
            response = client.get("/api/vpn")
    finally:
        server.stop()
    assert response.status_code == 200
    body = response.json()
    assert (body["state"], body["paused"]) == ("down", True)


def test_api_vpn_is_unknown_when_the_watchdog_is_away_and_health_does_not_care(tmp_path):
    with make_app(tmp_path, f"http://127.0.0.1:{free_port()}") as client:
        client.app.state.started_at -= 120  # past the first minute (#43: "starting" before that)
        response = client.get("/api/vpn")
        assert response.status_code == 200
        assert response.json()["state"] == "unknown"
        assert client.get("/health").status_code == 200


def test_api_vpn_only_allows_get(tmp_path):
    with make_app(tmp_path, f"http://127.0.0.1:{free_port()}") as client:
        assert client.post("/api/vpn").status_code == 405


# --- configuration ----------------------


def base_env(**extra: str) -> dict[str, str]:
    return {"GLUETUN_CONTROL_APIKEY": "k" * 22, "QBIT_PASSWORD": "p" * 12, **extra}


def test_alerts_are_off_by_default():
    cfg = WatchdogConfig.from_env(base_env())
    assert cfg.alert_url == ""
    assert cfg.alert_format == "ntfy"
    assert cfg.status_port == 8090


def test_alert_settings_are_read():
    cfg = WatchdogConfig.from_env(
        base_env(
            ALERT_URL="http://ntfy.lan/topic",
            ALERT_TOKEN="t",
            ALERT_FORMAT="Webhook",
            WATCHDOG_STATUS_PORT="0",
        )
    )
    assert (cfg.alert_url, cfg.alert_token, cfg.alert_format, cfg.status_port) == (
        "http://ntfy.lan/topic", "t", "webhook", 0,
    )  # fmt: skip


@pytest.mark.parametrize(
    "env", [{"ALERT_FORMAT": "sms"}, {"ALERT_URL": "ntfy.sh/topic"}, {"ALERT_URL": "ftp://x/y"}]
)
def test_bad_alert_settings_are_reported_without_showing_the_url(env):
    with pytest.raises(wd.ConfigError) as caught:
        WatchdogConfig.from_env(base_env(**env))
    assert "ALERT_" in str(caught.value)
    assert env.get("ALERT_URL", "zzz") not in str(caught.value)


@pytest.mark.security
def test_alert_secrets_are_not_in_the_config_repr():
    cfg = WatchdogConfig(alert_url="http://x/secret-topic", alert_token="secret-token")
    assert "secret-topic" not in repr(cfg)
    assert "secret-token" not in repr(cfg)


def test_the_app_setting_for_the_status_url():
    s = Settings.from_env({**ENV, "WATCHDOG_STATUS_URL": "http://dog:9000/"})
    assert s.watchdog_status_url == "http://dog:9000"
    with pytest.raises(Exception, match="WATCHDOG_STATUS_URL"):
        Settings.from_env({**ENV, "WATCHDOG_STATUS_URL": "https://dog"})
