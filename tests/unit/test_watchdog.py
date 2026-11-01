"""app/ytp/watchdog.py and the gluetun control client, against fake gluetun and qBittorrent."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers.fake_gluetun import API_KEY, FakeGluetun
from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from ytp import watchdog as wd
from ytp.clients.gluetun import (
    GluetunAuthError,
    GluetunControl,
    GluetunUnreachable,
)
from ytp.clients.qbittorrent import QbitClient, QbitRequestError
from ytp.watchdog import ConfigError, Watchdog, WatchdogConfig

A, B, C = "a" * 40, "b" * 40, "c" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


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
        {"hash": B, "name": "b", "state": "stoppedDL", "progress": 0.5},  # paused by the user
        {"hash": C, "name": "c", "state": "uploading", "progress": 1.0},
    ]
    yield server
    server.stop()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make(gluetun, qbit_server, clock=None, **config) -> tuple[Watchdog, Clock]:
    clock = clock or Clock()
    cfg = WatchdogConfig(
        control_url=gluetun.url,
        api_key=API_KEY,
        qbit_url=qbit_server.url,
        qbit_user=USER,
        qbit_password=PASSWORD,
        **config,
    )
    dog = Watchdog(
        GluetunControl(cfg.control_url, cfg.api_key, timeout=1.5),
        QbitClient(cfg.qbit_url, USER, PASSWORD, timeout=2),
        cfg,
        clock=clock,
    )
    return dog, clock


def steps(dog: Watchdog, clock: Clock, count: int, seconds: float = 1.0) -> None:
    for _ in range(count):
        dog.step()
        clock.now += seconds


def pauses(qbit_server: FakeQbittorrent) -> int:
    return len(qbit_server.calls("torrents/stop")) + len(qbit_server.calls("torrents/pause"))


# --- the control client ----------------------


def test_status_running_and_the_key_goes_in_the_header(gluetun):
    assert GluetunControl(gluetun.url, API_KEY).vpn_status() == "running"
    (hit,) = gluetun.hits
    assert hit["path"] == "/v1/vpn/status"
    assert hit["headers"]["x-api-key"] == API_KEY
    assert API_KEY not in hit["path"]


@pytest.mark.parametrize("word", ["stopped", "crashed", "STOPPED", "starting"])
def test_other_status_words_are_returned_lower_case(gluetun, word):
    gluetun.status = word
    assert GluetunControl(gluetun.url, API_KEY).vpn_status() == word.lower()


@pytest.mark.parametrize("key", ["wrong", ""])
def test_a_refused_key_is_an_auth_error_not_an_outage(gluetun, key):
    with pytest.raises(GluetunAuthError, match="make watchdog-key"):
        GluetunControl(gluetun.url, key).vpn_status()


def test_a_missing_route_is_also_a_setup_problem(gluetun):
    gluetun.http_status = 404
    with pytest.raises(GluetunAuthError):
        GluetunControl(gluetun.url, API_KEY).vpn_status()


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_count_as_unreachable(gluetun, status):
    gluetun.http_status = status
    with pytest.raises(GluetunUnreachable, match=str(status)):
        GluetunControl(gluetun.url, API_KEY).vpn_status()


@pytest.mark.parametrize("body", ["", "not json", "{}", '{"status": null}', "[]", '"x"'])
def test_garbled_answers_count_as_unreachable(gluetun, body):
    gluetun.body = body
    with pytest.raises(GluetunUnreachable):
        GluetunControl(gluetun.url, API_KEY).vpn_status()


def test_refused_and_timeout_count_as_unreachable():
    with pytest.raises(GluetunUnreachable, match="cannot reach gluetun"):
        GluetunControl(f"http://127.0.0.1:{free_port()}", API_KEY).vpn_status()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        slow = GluetunControl(f"http://127.0.0.1:{sock.getsockname()[1]}", API_KEY, timeout=0.3)
        started = time.monotonic()
        with pytest.raises(GluetunUnreachable):
            slow.vpn_status()
    assert time.monotonic() - started < 3


@pytest.mark.security
def test_the_key_is_in_no_repr_or_error(gluetun):
    assert API_KEY not in repr(GluetunControl(gluetun.url, API_KEY))
    with pytest.raises(GluetunAuthError) as error:
        GluetunControl(gluetun.url, "other-key").vpn_status()
    assert "other-key" not in str(error.value)
    with pytest.raises(GluetunUnreachable) as unreachable:
        GluetunControl(f"http://127.0.0.1:{free_port()}", API_KEY).vpn_status()
    assert API_KEY not in str(unreachable.value)


@pytest.mark.parametrize("url", ["", "gluetun:8000", "ftp://x", "http://gluetun:8000/v1"])
def test_a_bad_control_url_is_refused(url):
    with pytest.raises(ValueError, match="base_url"):
        GluetunControl(url, API_KEY)


# --- qBittorrent: pause and resume a set ----------------------


def test_pause_and_resume_just_these_torrents(qbit_server):
    client = QbitClient(qbit_server.url, USER, PASSWORD)
    client.pause_torrents([A.upper(), C])
    client.resume_torrents([A])
    assert qbit_server.calls("torrents/stop")[0].form == {"hashes": [f"{A}|{C}"]}
    assert qbit_server.calls("torrents/start")[0].form == {"hashes": [A]}


def test_the_hash_list_variants_fall_back_to_the_4x_names(qbit_server):
    qbit_server.api_generation = "4"
    client = QbitClient(qbit_server.url, USER, PASSWORD)
    client.pause_torrents([A])
    client.resume_torrents([A])
    assert qbit_server.calls("torrents/pause")[0].form == {"hashes": [A]}
    assert qbit_server.calls("torrents/resume")[0].form == {"hashes": [A]}


@pytest.mark.parametrize("hashes", [[], ["all"], ["x"], [A, "nope"], [f"{A}|{B}"], ["a" * 39]])
def test_only_real_infohashes_are_accepted(qbit_server, hashes):
    client = QbitClient(qbit_server.url, USER, PASSWORD)
    with pytest.raises(ValueError, match="infohashes"):
        client.pause_torrents(hashes)
    assert qbit_server.seen == []


# --- the watchdog: when it acts ----------------------


def test_a_healthy_vpn_changes_nothing(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server)
    steps(dog, clock, 20)
    assert dog.state == "healthy"
    assert pauses(qbit_server) == 0


def test_a_blip_shorter_than_the_threshold_is_ignored(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=3)
    gluetun.status = "stopped"
    steps(dog, clock, 2)
    gluetun.status = "running"
    steps(dog, clock, 5)
    gluetun.status = "stopped"
    steps(dog, clock, 2)  # the counter started again from zero
    assert dog.state == "healthy"
    assert pauses(qbit_server) == 0


def test_the_threshold_poll_pauses_everything_at_once(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=3)
    gluetun.status = "stopped"
    steps(dog, clock, 2)
    assert pauses(qbit_server) == 0
    dog.step()  # the third failure
    assert dog.state == "unhealthy"
    (call,) = qbit_server.calls("torrents/stop")
    assert call.form == {"hashes": ["all"]}
    assert dog.paused_by_us


def test_it_pauses_once_per_outage_not_every_second(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=2)
    gluetun.status = "crashed"
    steps(dog, clock, 30)
    assert pauses(qbit_server) == 1
    assert dog.pauses == 1


def test_gluetun_being_down_counts_as_a_failure(qbit_server):
    clock = Clock()
    cfg = WatchdogConfig(
        api_key=API_KEY, qbit_url=qbit_server.url, qbit_password=PASSWORD, threshold=2
    )
    dog = Watchdog(
        GluetunControl(f"http://127.0.0.1:{free_port()}", API_KEY, timeout=1),
        QbitClient(qbit_server.url, USER, PASSWORD, timeout=2),
        cfg,
        clock=clock,
    )
    steps(dog, clock, 2)
    assert dog.state == "unhealthy"
    assert pauses(qbit_server) == 1


def test_a_new_outage_pauses_again_after_a_recovery(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=2)
    for _ in range(2):
        gluetun.status = "stopped"
        steps(dog, clock, 3)
        gluetun.status = "running"
        steps(dog, clock, 3)
    assert pauses(qbit_server) == 2


# --- pausing that cannot be delivered ----------------------


def test_when_qbittorrent_is_unreachable_the_pause_is_retried_every_few_seconds(gluetun, caplog):
    clock = Clock()
    cfg = WatchdogConfig(api_key=API_KEY, qbit_password=PASSWORD, threshold=2)
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY),
        QbitClient(f"http://127.0.0.1:{free_port()}", USER, PASSWORD, timeout=0.5),
        cfg,
        clock=clock,
    )
    gluetun.status = "stopped"
    with caplog.at_level(logging.WARNING, logger="ytp.watchdog"):
        steps(dog, clock, 12)
    assert dog.state == "unhealthy"
    assert not dog.paused_by_us
    assert caplog.text.count("could not pause the torrents yet") == 1, "logged once per outage"


def test_the_pause_goes_through_once_qbittorrent_answers_again(gluetun, qbit_server):
    clock = Clock()
    port = qbit_server.port
    qbit_server.stop()
    cfg = WatchdogConfig(api_key=API_KEY, qbit_password=PASSWORD, threshold=2)
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY),
        QbitClient(f"http://127.0.0.1:{port}", USER, PASSWORD, timeout=0.5),
        cfg,
        clock=clock,
    )
    gluetun.status = "stopped"
    steps(dog, clock, 6)
    assert not dog.paused_by_us
    back = FakeQbittorrent().start(port)
    try:
        steps(dog, clock, 6)
        assert dog.paused_by_us
        assert len(back.calls("torrents/stop")) == 1
    finally:
        back.stop()


def test_if_the_vpn_is_back_before_a_pause_could_be_sent_nothing_is_paused(gluetun):
    clock = Clock()
    cfg = WatchdogConfig(api_key=API_KEY, qbit_password=PASSWORD, threshold=2)
    dead_qbit = FakeQbittorrent().start()
    url = dead_qbit.url
    dead_qbit.stop()
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY), QbitClient(url, USER, PASSWORD, timeout=0.5), cfg,
        clock=clock,
    )  # fmt: skip
    gluetun.status = "stopped"
    steps(dog, clock, 3)
    gluetun.status = "running"
    steps(dog, clock, 2)
    assert dog.state == "healthy"
    assert not dog.paused_by_us


def test_a_refused_qbittorrent_login_is_logged_as_an_error_once(gluetun, qbit_server, caplog):
    clock = Clock()
    cfg = WatchdogConfig(api_key=API_KEY, qbit_password="wrong", threshold=1)
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY),
        QbitClient(qbit_server.url, USER, "wrong", timeout=2),
        cfg,
        clock=clock,
    )
    gluetun.status = "stopped"
    with caplog.at_level(logging.ERROR, logger="ytp.watchdog"):
        steps(dog, clock, 20)
    assert caplog.text.count("could not pause") == 1
    assert qbit_server.logins == 1, "no hammering of qBittorrent's login"


# --- a watchdog that cannot see ----------------------


@pytest.mark.parametrize("http_status", [401, 403, 404])
def test_a_control_server_that_refuses_us_is_a_setup_problem_and_pauses_nothing(
    gluetun, qbit_server, http_status, caplog
):
    dog, clock = make(gluetun, qbit_server, threshold=2)
    if http_status == 401:
        dog.control = GluetunControl(gluetun.url, "the wrong key")
    else:
        gluetun.http_status = http_status  # right key, but the server refuses / lacks the route
    with caplog.at_level(logging.ERROR, logger="ytp.watchdog"):
        steps(dog, clock, 30)
    assert dog.misconfigured
    assert dog.state == "healthy"
    assert pauses(qbit_server) == 0
    assert caplog.text.count("NOT protecting anything") == 1, "loud once, not every second"


def test_it_recovers_when_the_key_is_fixed(gluetun, qbit_server, caplog):
    dog, clock = make(gluetun, qbit_server)
    good = dog.control
    dog.control = GluetunControl(gluetun.url, "nope")
    steps(dog, clock, 3)
    assert dog.misconfigured
    dog.control = good
    with caplog.at_level(logging.INFO, logger="ytp.watchdog"):
        steps(dog, clock, 2)
    assert dog.misconfigured is None
    assert "accepts the key now" in caplog.text


def test_the_vpn_really_failing_after_a_setup_problem_still_counts(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=2)
    good = dog.control
    dog.control = GluetunControl(gluetun.url, "nope")
    steps(dog, clock, 5)
    dog.control = good
    gluetun.status = "stopped"
    steps(dog, clock, 2)
    assert dog.state == "unhealthy"


# --- recovery and the optional auto-resume -------------------------


def outage(dog, clock, gluetun, seconds=3):
    gluetun.status = "stopped"
    steps(dog, clock, seconds)
    gluetun.status = "running"


def test_without_auto_resume_the_torrents_stay_paused_after_recovery(gluetun, qbit_server, caplog):
    dog, clock = make(gluetun, qbit_server, threshold=2, resume_after=0)
    dog.step()  # remember what is running
    outage(dog, clock, gluetun)
    with caplog.at_level(logging.INFO, logger="ytp.watchdog"):
        steps(dog, clock, 60)
    assert dog.state == "healthy"
    assert qbit_server.calls("torrents/start") == []
    assert "torrents stay paused" in caplog.text


def test_auto_resume_restarts_only_what_was_running_after_a_stable_period(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=2, resume_after=30)
    dog.step()  # B is paused by the user, A and C run
    outage(dog, clock, gluetun)
    steps(dog, clock, 25)
    assert qbit_server.calls("torrents/start") == [], "not stable long enough yet"
    assert dog.state == "unhealthy"
    steps(dog, clock, 10)
    (call,) = qbit_server.calls("torrents/start")
    assert call.form == {"hashes": [f"{A}|{C}"]}, "B stays paused: the user did that"
    assert dog.state == "healthy"
    assert dog.resumes == 1


def test_a_flap_while_waiting_restarts_the_stable_period(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=2, resume_after=30)
    dog.step()
    outage(dog, clock, gluetun)
    steps(dog, clock, 20)
    gluetun.status = "stopped"
    steps(dog, clock, 1)  # one failed poll: the stable period starts over
    gluetun.status = "running"
    steps(dog, clock, 25)
    assert qbit_server.calls("torrents/start") == []
    steps(dog, clock, 10)
    assert len(qbit_server.calls("torrents/start")) == 1
    assert pauses(qbit_server) == 1, "a single failed poll is a blip, not a second outage"


def test_a_resume_that_fails_is_retried(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=2, resume_after=5)
    dog.step()
    outage(dog, clock, gluetun)
    qbit_server.api_generation = "none"  # neither start nor resume exists: every try fails
    steps(dog, clock, 20)
    assert dog.state == "unhealthy"
    assert dog.resumes == 0
    qbit_server.api_generation = "5"
    steps(dog, clock, 8)
    assert dog.resumes == 1
    assert dog.state == "healthy"


def test_nothing_remembered_means_nothing_to_resume(gluetun, qbit_server):
    qbit_server.torrents = []
    dog, clock = make(gluetun, qbit_server, threshold=2, resume_after=5)
    dog.step()
    outage(dog, clock, gluetun)
    steps(dog, clock, 15)
    assert dog.state == "healthy"
    assert qbit_server.calls("torrents/start") == []


def test_the_running_torrents_are_refreshed_every_30_seconds_while_healthy(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server)
    steps(dog, clock, 29)
    assert len(qbit_server.calls("torrents/info")) == 1
    steps(dog, clock, 3)
    assert len(qbit_server.calls("torrents/info")) == 2


def test_the_remembered_list_is_not_replaced_during_an_outage(gluetun, qbit_server):
    dog, clock = make(gluetun, qbit_server, threshold=2, resume_after=5)
    dog.step()
    remembered = list(dog._remembered)
    outage(dog, clock, gluetun)
    qbit_server.torrents = []  # after the pause qBittorrent lists everything as stopped
    steps(dog, clock, 3)
    assert dog._remembered == remembered == [A, C]


# --- the loop and the health check ----------------------


def test_run_polls_until_stopped_and_survives_a_crashing_poll(
    gluetun, qbit_server, tmp_path, caplog
):
    dog, _ = make(gluetun, qbit_server, clock=time.monotonic, interval=0.05)
    calls = {"n": 0}
    real = dog.step

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bug in one poll")
        real()

    dog.step = flaky
    stop = threading.Event()
    beat = tmp_path / "alive"
    thread = threading.Thread(target=dog.run, args=(stop, beat))
    with caplog.at_level(logging.ERROR, logger="ytp.watchdog"):
        thread.start()
        time.sleep(0.5)
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert calls["n"] >= 5, "the loop kept going after the exception"
    assert "watchdog poll failed" in caplog.text
    assert beat.read_text() == "ok"


def test_the_heartbeat_says_misconfigured_when_it_cannot_read_the_status(
    gluetun, qbit_server, tmp_path
):
    dog, _ = make(gluetun, qbit_server, clock=time.monotonic, interval=0.05)
    dog.control = GluetunControl(gluetun.url, "wrong")
    stop = threading.Event()
    beat = tmp_path / "alive"
    thread = threading.Thread(target=dog.run, args=(stop, beat))
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(5)
    assert beat.read_text().startswith("misconfigured:")
    ok, message = wd.check(beat)
    assert not ok
    assert message.startswith("misconfigured")


def test_check_needs_a_fresh_ok_heartbeat(tmp_path):
    beat = tmp_path / "alive"
    assert wd.check(beat) == (False, "no heartbeat yet")
    beat.write_text("ok")
    assert wd.check(beat) == (True, "ok")
    old = time.time() - 60
    import os

    os.utime(beat, (old, old))
    ok, message = wd.check(beat)
    assert not ok
    assert "last poll" in message


def test_main_check_exit_codes(tmp_path, monkeypatch, capsys):
    beat = tmp_path / "alive"
    monkeypatch.setattr(wd, "HEARTBEAT_FILE", beat)
    assert wd.main(["--check"]) == 1
    beat.write_text("ok")
    assert wd.main(["--check"]) == 0
    assert capsys.readouterr().out.strip().endswith("ok")


# --- configuration ----------------------


def test_config_defaults_and_values():
    cfg = WatchdogConfig.from_env({"GLUETUN_CONTROL_APIKEY": "k" * 22, "QBIT_PASSWORD": "p" * 12})
    assert (cfg.control_url, cfg.qbit_url, cfg.qbit_user) == (
        "http://gluetun:8000",
        "http://gluetun:8080",
        "admin",
    )
    assert (cfg.interval, cfg.threshold, cfg.resume_after) == (1.0, 3, 0.0)
    custom = WatchdogConfig.from_env(
        {
            "GLUETUN_CONTROL_APIKEY": "k" * 22,
            "QBIT_PASSWORD": "p" * 12,
            "WATCHDOG_INTERVAL": "2.5",
            "WATCHDOG_THRESHOLD": "5",
            "WATCHDOG_RESUME_AFTER": "300",
            "GLUETUN_CONTROL_URL": "http://g:8000/",
        }
    )
    assert (custom.interval, custom.threshold, custom.resume_after) == (2.5, 5, 300.0)
    assert custom.control_url == "http://g:8000"


def test_all_problems_are_reported_together():
    with pytest.raises(ConfigError) as caught:
        WatchdogConfig.from_env({"WATCHDOG_INTERVAL": "fast", "WATCHDOG_THRESHOLD": "0"})
    message = str(caught.value)
    for fragment in (
        "GLUETUN_CONTROL_APIKEY",
        "QBIT_PASSWORD",
        "WATCHDOG_INTERVAL",
        "WATCHDOG_THRESHOLD",
    ):
        assert fragment in message


@pytest.mark.parametrize(
    ("name", "value"),
    [("WATCHDOG_INTERVAL", "0"), ("WATCHDOG_INTERVAL", "3600"), ("WATCHDOG_THRESHOLD", "1000"),
     ("WATCHDOG_RESUME_AFTER", "-1")],
)  # fmt: skip
def test_out_of_range_numbers_are_refused(name, value):
    env = {"GLUETUN_CONTROL_APIKEY": "k" * 22, "QBIT_PASSWORD": "p" * 12, name: value}
    with pytest.raises(ConfigError, match=name):
        WatchdogConfig.from_env(env)


@pytest.mark.security
def test_secrets_are_not_in_the_repr():
    cfg = WatchdogConfig(api_key="the-api-key-value", qbit_password="the-password-value")
    assert "the-api-key-value" not in repr(cfg)
    assert "the-password-value" not in repr(cfg)


def test_main_exits_2_with_a_json_error_on_bad_configuration(monkeypatch, capfd):
    for name in ("GLUETUN_CONTROL_APIKEY", "QBIT_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert wd.main([]) == 2
    out = capfd.readouterr().out
    assert '"msg": "invalid configuration"' in out
    assert "GLUETUN_CONTROL_APIKEY" in out


def test_qbit_request_errors_do_not_escape_the_pause(gluetun, qbit_server):
    qbit_server.api_generation = "none"
    dog, _ = make(gluetun, qbit_server, threshold=1)
    gluetun.status = "stopped"
    dog.step()  # must not raise
    assert not dog.paused_by_us
    assert issubclass(QbitRequestError, Exception)


def test_the_module_needs_nothing_but_the_standard_library_and_our_clients():
    source = (Path(wd.__file__)).read_text(encoding="utf-8")
    assert "fastapi" not in source
    assert "sqlite" not in source
    assert "ytp.db" not in source
