"""app/ytp/egress.py and its place in the watchdog (issue #23)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator

import pytest

from tests.helpers.fake_alert_target import FakeAlertTarget
from tests.helpers.fake_gluetun import API_KEY, FakeGluetun
from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from ytp import watchdog as wd
from ytp.alerts import Alerter
from ytp.clients.gluetun import (
    GluetunAuthError,
    GluetunControl,
    GluetunUnreachable,
    PublicIP,
)
from ytp.clients.qbittorrent import QbitClient
from ytp.egress import EgressChecker, mask_ip, parse_networks
from ytp.watchdog import Watchdog, WatchdogConfig

HOME_V4 = "84.150.20.30"
HOME_V6 = "2003:abcd:1200::/40"


@pytest.fixture
def gluetun() -> Iterator[FakeGluetun]:
    server = FakeGluetun().start()
    yield server
    server.stop()


@pytest.fixture
def qbit_server() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    server.torrents = [{"hash": "a" * 40, "name": "a", "state": "downloading", "progress": 0.5}]
    yield server
    server.stop()


@pytest.fixture
def target() -> Iterator[FakeAlertTarget]:
    server = FakeAlertTarget().start()
    yield server
    server.stop()


def checker(gluetun, qbit_server, **kw) -> EgressChecker:
    return EgressChecker(
        GluetunControl(gluetun.url, API_KEY, timeout=1.5),
        QbitClient(qbit_server.url, USER, PASSWORD, timeout=2) if qbit_server else None,
        **kw,
    )


def set_ip(gluetun, ip: str, **extra) -> None:
    gluetun.public = {"public_ip": ip, "country": "Germany", "organization": "AS1 Example", **extra}


# --- gluetun's public address ----------------------


def test_public_ip_parses_what_gluetun_reports(gluetun):
    info = GluetunControl(gluetun.url, API_KEY).public_ip()
    assert info == PublicIP(
        "185.107.12.34", "Germany", "Hesse", "Frankfurt", "AS64500 Example Hosting"
    )
    assert gluetun.hits[-1]["path"] == "/v1/publicip/ip"
    assert gluetun.hits[-1]["headers"]["x-api-key"] == API_KEY


def test_missing_optional_fields_are_empty(gluetun):
    gluetun.public = {"public_ip": "185.107.12.34"}
    info = GluetunControl(gluetun.url, API_KEY).public_ip()
    assert (info.country, info.organization) == ("", "")


@pytest.mark.parametrize("public", [{}, {"public_ip": ""}, {"public_ip": None}, {"public_ip": 5}])
def test_no_address_means_the_tunnel_is_probably_restarting(gluetun, public):
    gluetun.public = public
    with pytest.raises(GluetunUnreachable, match="no public address"):
        GluetunControl(gluetun.url, API_KEY).public_ip()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_role_without_the_route_is_a_setup_problem(gluetun, status):
    gluetun.public_http_status = status
    with pytest.raises(GluetunAuthError, match="make watchdog-key"):
        GluetunControl(gluetun.url, API_KEY).public_ip()


@pytest.mark.security
def test_the_public_ip_is_not_in_the_repr(gluetun):
    info = GluetunControl(gluetun.url, API_KEY).public_ip()
    assert "185.107.12.34" not in repr(info)


# --- masking and parsing ----------------------


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ("203.0.113.7", "203.0.x.x"),
        ("2003:abcd:1234::7", "2003:abcd:x"),
        ("not an ip", "x"),
        ("", "x"),
    ],
)
def test_mask_ip(value, masked):
    assert mask_ip(value) == masked


def test_parse_networks_accepts_addresses_prefixes_and_both_separators():
    nets = parse_networks(f"{HOME_V4}, {HOME_V6} 84.150.99.0/24")
    assert [str(n) for n in nets] == [f"{HOME_V4}/32", HOME_V6, "84.150.99.0/24"]
    assert parse_networks("") == ()


@pytest.mark.security
def test_a_bad_entry_is_named_by_position_not_echoed():
    with pytest.raises(ValueError, match="entry 2") as caught:
        parse_networks("84.150.20.1, my-secret-garbage")
    assert "my-secret-garbage" not in str(caught.value)


# --- the checks ----------------------


def test_a_normal_vpn_address_passes(gluetun, qbit_server):
    result = checker(gluetun, qbit_server, home_networks=parse_networks(HOME_V4)).check()
    assert result.status == "ok"
    assert result.problems == []
    assert (result.country, result.organization) == ("Germany", "AS64500 Example Hosting")
    assert result.masked_ip == "185.107.x.x"


@pytest.mark.parametrize("home", [HOME_V4, "84.150.20.0/24", "84.150.0.0/16"])
def test_an_egress_address_in_the_home_network_is_a_mismatch(gluetun, qbit_server, home):
    set_ip(gluetun, HOME_V4)
    result = checker(gluetun, qbit_server, home_networks=parse_networks(home)).check()
    assert result.status == "mismatch"
    assert "belongs to your home network" in result.problems[0]


def test_an_ipv6_egress_in_the_home_prefix_is_a_mismatch(gluetun, qbit_server):
    set_ip(gluetun, "2003:abcd:1234::77")
    result = checker(gluetun, qbit_server, home_networks=parse_networks(HOME_V6)).check()
    assert result.status == "mismatch"


def test_other_families_and_other_prefixes_do_not_match(gluetun, qbit_server):
    home = parse_networks(f"{HOME_V6} 84.150.20.0/24")
    set_ip(gluetun, "91.200.10.9")
    assert checker(gluetun, qbit_server, home_networks=home).check().status == "ok"
    set_ip(gluetun, "2a00:1450::1")
    assert checker(gluetun, qbit_server, home_networks=home).check().status == "ok"


@pytest.mark.parametrize(
    "ip", ["10.1.2.3", "192.168.178.20", "100.64.0.9", "127.0.0.1", "169.254.1.1", "fe80::1", "::1"]
)
def test_a_non_public_address_is_a_mismatch(gluetun, qbit_server, ip):
    set_ip(gluetun, ip)
    result = checker(gluetun, qbit_server).check()
    assert result.status == "mismatch"
    assert "not a public address" in result.problems[0]


def test_the_expected_country_is_checked_case_insensitively(gluetun, qbit_server):
    assert checker(gluetun, qbit_server, expected_countries=("germany",)).check().status == "ok"
    assert (
        checker(gluetun, qbit_server, expected_countries=("Netherlands", "GERMANY")).check().status
        == "ok"
    )
    result = checker(gluetun, qbit_server, expected_countries=("Netherlands",)).check()
    assert result.status == "mismatch"
    assert "country is Germany, expected netherlands" in result.problems[0]


def test_a_missing_country_cannot_be_judged(gluetun, qbit_server):
    set_ip(gluetun, "91.200.10.1", country="")
    result = checker(gluetun, qbit_server, expected_countries=("germany",)).check()
    assert result.status == "inconclusive"


def test_without_an_expected_country_any_country_is_fine(gluetun, qbit_server):
    set_ip(gluetun, "91.200.10.1", country="Atlantis")
    assert checker(gluetun, qbit_server).check().status == "ok"


@pytest.mark.parametrize("bound", ["", "eth0", None, "tun1"])
def test_qbittorrent_not_bound_to_the_vpn_interface_is_a_mismatch(gluetun, qbit_server, bound):
    qbit_server.preferences = {"current_network_interface": bound}
    result = checker(gluetun, qbit_server).check()
    assert result.status == "mismatch"
    assert "not bound to the VPN interface (tun0)" in result.problems[0]


def test_qbittorrent_being_away_does_not_fail_the_check(gluetun, qbit_server):
    c = checker(gluetun, qbit_server)
    qbit_server.stop()
    assert c.check().status == "ok"


def test_the_check_works_without_a_qbittorrent_client(gluetun):
    assert checker(gluetun, None).check().status == "ok"


def test_several_problems_are_all_listed(gluetun, qbit_server):
    set_ip(gluetun, HOME_V4)
    qbit_server.preferences = {"current_network_interface": "eth0"}
    result = checker(
        gluetun, qbit_server, home_networks=parse_networks(HOME_V4), expected_countries=("france",)
    ).check()
    assert len(result.problems) == 3


def test_gluetun_not_answering_is_inconclusive_not_a_mismatch(gluetun, qbit_server):
    c = checker(gluetun, qbit_server)
    gluetun.stop()
    assert c.check().status == "inconclusive"


def test_a_refused_key_is_inconclusive(gluetun, qbit_server):
    c = EgressChecker(GluetunControl(gluetun.url, "wrong"), None)
    assert c.check().status == "inconclusive"


def test_an_unreadable_address_is_inconclusive(gluetun, qbit_server):
    set_ip(gluetun, "not-an-address")
    assert checker(gluetun, qbit_server).check().status == "inconclusive"


@pytest.mark.security
def test_no_problem_text_contains_an_address(gluetun, qbit_server):
    set_ip(gluetun, HOME_V4)
    qbit_server.preferences = {"current_network_interface": "eth0"}
    result = checker(gluetun, qbit_server, home_networks=parse_networks(HOME_V4)).check()
    text = " ".join(result.problems)
    assert not re.search(r"\d{1,3}(\.\d{1,3}){3}", text)
    assert HOME_V4 not in text


# --- the watchdog acts on it ----------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(gluetun, qbit_server, target=None, *, confirm=2, **config) -> Watchdog:
    cfg = WatchdogConfig(
        control_url=gluetun.url, api_key=API_KEY, qbit_url=qbit_server.url,
        qbit_password=PASSWORD, egress_confirm=confirm, **config,
    )  # fmt: skip
    control = GluetunControl(gluetun.url, API_KEY, timeout=1.5)
    qbit = QbitClient(qbit_server.url, USER, PASSWORD, timeout=2)
    egress = EgressChecker(control, qbit, home_networks=parse_networks(HOME_V4))
    return Watchdog(
        control, qbit, cfg, clock=Clock(), egress=egress,
        alerter=Alerter(target.url, fmt="webhook") if target else None,
    )  # fmt: skip


def pauses(qbit_server) -> int:
    return len(qbit_server.calls("torrents/stop")) + len(qbit_server.calls("torrents/pause"))


def test_a_good_check_records_the_state_and_country(gluetun, qbit_server):
    dog = make(gluetun, qbit_server)
    assert dog.snapshot()["egress"] == "unknown"
    dog.check_egress()
    snap = dog.snapshot()
    assert (snap["egress"], snap["egress_country"]) == ("ok", "Germany")
    assert snap["egress_checked"]
    assert pauses(qbit_server) == 0


def test_without_a_checker_the_state_is_off(gluetun, qbit_server):
    dog = Watchdog(
        GluetunControl(gluetun.url, API_KEY), QbitClient(qbit_server.url, USER, PASSWORD),
        WatchdogConfig(api_key=API_KEY, qbit_password=PASSWORD),
    )  # fmt: skip
    dog.check_egress()
    assert dog.snapshot()["egress"] == "off"


def test_one_odd_answer_is_not_enough_two_pause_and_alert_once(gluetun, qbit_server, target):
    dog = make(gluetun, qbit_server, target, confirm=2)
    set_ip(gluetun, HOME_V4)
    dog.check_egress()
    assert (dog.egress_state, pauses(qbit_server)) == ("unknown", 0)
    dog.check_egress()
    assert dog.egress_state == "mismatch"
    assert pauses(qbit_server) == 1
    for _ in range(5):
        dog.check_egress()
    assert pauses(qbit_server) == 1, "paused once, not at every check"
    assert target.wait_for(1)
    time.sleep(0.3)
    (alert,) = target.json_bodies()
    assert (alert["event"], alert["urgent"]) == ("egress", True)
    assert "home network" in alert["message"]
    assert "All torrents were paused" in alert["message"]


def test_a_blip_in_between_resets_nothing_but_a_good_check_resets_the_count(gluetun, qbit_server):
    dog = make(gluetun, qbit_server, confirm=2)
    set_ip(gluetun, HOME_V4)
    dog.check_egress()
    gluetun.public = {"public_ip": "185.107.12.34", "country": "Germany"}
    dog.check_egress()
    set_ip(gluetun, HOME_V4)
    dog.check_egress()
    assert dog.egress_state != "mismatch", "ok in between: the count started again"
    assert pauses(qbit_server) == 0


def test_inconclusive_checks_never_count_or_pause(gluetun, qbit_server):
    dog = make(gluetun, qbit_server, confirm=1)
    gluetun.public = {}
    for _ in range(5):
        dog.check_egress()
    assert (dog.egress_state, pauses(qbit_server)) == ("unknown", 0)


def test_when_the_check_passes_again_the_torrents_stay_paused(gluetun, qbit_server, target):
    dog = make(gluetun, qbit_server, target, confirm=1, resume_after=1)
    dog.check_egress()
    set_ip(gluetun, HOME_V4)
    dog.check_egress()
    assert dog.egress_state == "mismatch"
    gluetun.public = {"public_ip": "185.107.12.34", "country": "Germany"}
    dog.check_egress()
    assert dog.egress_state == "ok"
    assert target.wait_for(2)
    events = [b["event"] for b in target.json_bodies()]
    assert events == ["egress", "egress_ok"]
    assert qbit_server.calls("torrents/start") == [], "never resumed automatically"


def test_a_pause_that_fails_is_retried_at_the_next_check(gluetun, qbit_server):
    dog = make(gluetun, qbit_server, confirm=1)
    set_ip(gluetun, HOME_V4)
    qbit_server.api_generation = "none"
    dog.check_egress()
    assert dog.egress_state == "mismatch"
    assert not dog._egress_paused
    qbit_server.api_generation = "5"
    dog.check_egress()
    assert dog._egress_paused


def test_no_egress_check_while_the_vpn_is_down_or_the_watchdog_is_blind(gluetun, qbit_server):
    dog = make(gluetun, qbit_server, confirm=1)
    set_ip(gluetun, HOME_V4)
    dog.state = "unhealthy"
    dog.check_egress()
    assert dog.egress_state == "unknown"
    dog.state = "healthy"
    dog.misconfigured = "x"
    dog.check_egress()
    assert dog.egress_state == "unknown"
    assert not [h for h in gluetun.hits if h["path"] == "/v1/publicip/ip"]


@pytest.mark.security
def test_logs_carry_the_address_masked_and_alerts_not_at_all(gluetun, qbit_server, target, caplog):
    dog = make(gluetun, qbit_server, target, confirm=1)
    set_ip(gluetun, HOME_V4)
    with caplog.at_level(logging.DEBUG):
        dog.check_egress()
    everything = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert HOME_V4 not in everything
    assert any(getattr(r, "egress", "") == "84.150.x.x" for r in caplog.records)
    assert target.wait_for(1)
    assert HOME_V4 not in target.received[0]["body"].decode()
    assert HOME_V4 not in str(dog.snapshot())


def test_the_egress_loop_runs_on_its_own_thread_and_stops(gluetun, qbit_server):
    dog = make(gluetun, qbit_server, egress_interval=0.05)
    dog.config = WatchdogConfig(**{**dog.config.__dict__, "egress_interval": 0.05})
    stop = threading.Event()
    thread = threading.Thread(target=dog.run, args=(stop, None))
    thread.start()
    deadline = time.monotonic() + 5
    while dog.snapshot()["egress"] != "ok" and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert dog.snapshot()["egress"] == "ok"
    assert not thread.is_alive()


def test_a_crashing_egress_check_does_not_stop_the_loop(gluetun, qbit_server, caplog):
    dog = make(gluetun, qbit_server)
    dog.config = WatchdogConfig(**{**dog.config.__dict__, "egress_interval": 0.05})
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RuntimeError("bug")

    dog.check_egress = flaky
    stop = threading.Event()
    thread = threading.Thread(target=dog._egress_loop, args=(stop, 0.0))
    with caplog.at_level(logging.ERROR, logger="ytp.watchdog"):
        thread.start()
        time.sleep(0.4)
        stop.set()
        thread.join(5)
    assert calls["n"] >= 3
    assert "egress check failed" in caplog.text


# --- configuration ----------------------


def base(**extra: str) -> dict[str, str]:
    return {"GLUETUN_CONTROL_APIKEY": "k" * 22, "QBIT_PASSWORD": "p" * 12, **extra}


def test_egress_defaults():
    cfg = WatchdogConfig.from_env(base())
    assert (cfg.egress_interval, cfg.egress_confirm) == (300.0, 2)
    assert (cfg.home_public_ip, cfg.egress_expected_country) == ("", "")


def test_egress_settings_are_read():
    cfg = WatchdogConfig.from_env(
        base(
            EGRESS_CHECK_INTERVAL="600",
            EGRESS_CONFIRM="3",
            HOME_PUBLIC_IP=HOME_V4,
            EGRESS_EXPECTED_COUNTRY="Germany",
        )
    )
    assert (cfg.egress_interval, cfg.egress_confirm, cfg.home_public_ip) == (600.0, 3, HOME_V4)


def test_zero_turns_the_check_off_and_a_too_fast_interval_is_refused():
    assert WatchdogConfig.from_env(base(EGRESS_CHECK_INTERVAL="0")).egress_interval == 0
    with pytest.raises(wd.ConfigError, match="at least 30 seconds"):
        WatchdogConfig.from_env(base(EGRESS_CHECK_INTERVAL="5"))


@pytest.mark.security
def test_a_bad_home_address_is_reported_without_echoing_it():
    with pytest.raises(wd.ConfigError) as caught:
        WatchdogConfig.from_env(base(HOME_PUBLIC_IP="84.150.20.1, super-secret-text"))
    message = str(caught.value)
    assert "HOME_PUBLIC_IP: entry 2" in message
    assert "super-secret-text" not in message
    assert "84.150.20.1" not in message


@pytest.mark.security
def test_the_home_address_is_not_in_the_config_repr():
    assert HOME_V4 not in repr(WatchdogConfig(home_public_ip=HOME_V4))
