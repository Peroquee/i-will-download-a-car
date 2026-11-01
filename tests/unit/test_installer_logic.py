"""The installer's pure parts: the answer checks, .env handling, the texts, system parsing (#37)."""

# ruff: noqa: E501

from __future__ import annotations

import stat
import string
from datetime import datetime

import pytest
from ytp_install import answers as ans
from ytp_install import envfile, steps, system
from ytp_install.answers import Answers
from ytp_install.texts import TEXTS, t

from tests.helpers.compose import all_compose_variables, parse_env_file
from tests.helpers.installer import ROOT

ENV_EXAMPLE = parse_env_file(ROOT / ".env.example")

# --- answers -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["admin", "a.b-c_d", "x" * 64])
def test_good_user_names(value):
    assert ans.check_user(value) is None


@pytest.mark.parametrize("value", ["", "ab", "has space", "x" * 65, "a/b", "ü-ser", "a;b"])
def test_bad_user_names(value):
    assert ans.check_user(value) == "err.user"


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("a-long-enough-pass", None),
        ("short", "err.short"),
        ("with'quote-and-long-enough", "err.quote"),
        (" leading-space-long-enough", "err.control"),
        ("trailing-space-long-enough ", "err.control"),
        ("line\nbreak-long-enough", "err.control"),
        ('p$ss#with%special{chars}\\and"double', None),
    ],
)
def test_web_passwords(value, problem):
    assert ans.check_web_password(value) == problem


@pytest.mark.parametrize(
    ("dirs", "problem"),
    [
        (ans.LIBRARY_DEFAULTS, None),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": "Filme"}, "err.folder_twice"),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": "a/b"}, "err.folder"),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": ".."}, "err.folder"),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": "-rf"}, "err.folder"),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": "it's"}, "err.folder"),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": ""}, "err.folder"),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": "downloads"}, "err.folder_downloads"),
        ({**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": "Musik\n"}, "err.folder"),
    ],
)
def test_folder_names(dirs, problem):
    assert ans.check_folder_names(dirs) == problem


@pytest.mark.parametrize("value", ["/data", "/mnt/media", "/srv/my media"])
def test_good_data_roots(value):
    assert ans.check_data_root(value) is None


@pytest.mark.parametrize(
    "value", ["", "data", "/", "/a/../b", "/a/./b", "/a//b", "/it's", "relative/path"]
)
def test_bad_data_roots(value):
    assert ans.check_data_root(value) == "err.path"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("192.168.178.20", True),
        ("10.0.0.1", True),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("192.168.178", False),
        ("host", False),
    ],
)
def test_ipv4(value, ok):
    assert (ans.check_ipv4(value) is None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("192.168.178.0/24", True),
        ("10.0.0.0/8", True),
        ("192.168.178.20/24", False),
        ("0.0.0.0/0", False),
        ("10.0.0.0/31", False),
        ("fd00::/64", False),
        ("nonsense", False),
    ],
)
def test_subnets(value, ok):
    assert (ans.check_subnet(value) is None) is ok


def test_the_address_must_be_inside_the_network():
    assert ans.check_address_in_subnet("192.168.178.20", "192.168.178.0/24") is None
    assert ans.check_address_in_subnet("192.168.1.20", "192.168.178.0/24") == "err.not_in_subnet"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("http://192.168.178.108:8096", True),
        ("https://jf.example/", True),
        ("ftp://x", False),
        ("192.168.1.1", False),
        ("http://", False),
        ("http://a b", False),
        ("http://it's", False),
    ],
)
def test_urls(value, ok):
    assert (ans.check_http_url(value) is None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("0123456789abcdef0123456789abcdef", True),
        ("0123456789ABCDEF0123456789abcdef", True),
        ("0123", False),
        ("g" * 32, False),
        ("", False),
    ],
)
def test_jellyfin_keys(value, ok):
    assert (ans.check_jellyfin_key(value) is None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("2003:abcd:1200::/56", True),
        ("84.150.20.30, 2003:abcd:1200::/56", True),
        ("84.150.20.30 2003:abcd::1", True),
        ("", True),
        ("2003:zz::/56", False),
        ("hello", False),
    ],
)
def test_home_prefixes(value, ok):
    assert (ans.check_prefixes(value) is None) is ok


def test_prefixes_are_written_in_one_style():
    assert (
        ans.normalise_prefixes("84.150.20.30  2003:abcd:1200::/56,fd00::1")
        == "84.150.20.30, 2003:abcd:1200::/56, fd00::1"
    )


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("mullvad", True),
        ("proton vpn", True),
        ("nord-vpn", True),
        ("Mullvad", False),
        ("", False),
        ("a", False),
        ("x;y", False),
    ],
)
def test_provider_names(value, ok):
    assert (ans.check_provider(value) is None) is ok


def test_wireguard_addresses():
    assert ans.check_wireguard_addresses("10.64.222.21/32") is None
    assert ans.check_wireguard_addresses("10.64.222.21/32, fc00::1/128") is None
    assert ans.check_wireguard_addresses("nope") == "err.wg_addresses"
    assert ans.check_wireguard_addresses("  ") == "err.empty"


# --- what the answers become -------------------------------------------------------------------------


def complete(**overrides) -> Answers:
    values = dict(
        lan_address="192.168.178.20",
        lan_subnet="192.168.178.0/24",
        vpn_user="u",
        vpn_password="p-secret-1",
        qbit_password="q-secret-12345",
    )
    values.update(overrides)
    return Answers(**values)


def test_every_setting_the_installer_writes_exists_in_the_template():
    for a in (
        complete(),
        complete(
            vpn_kind="builtin",
            vpn_provider="mullvad",
            vpn_type="wireguard",
            wireguard_key="k",
            wireguard_addresses="10.0.0.2/32",
        ),
        complete(vpn_kind="builtin", vpn_provider="nord"),
    ):
        assert set(a.env_values()) <= set(ENV_EXAMPLE), set(a.env_values()) - set(ENV_EXAMPLE)


def test_every_setting_the_installer_writes_is_used_by_compose_or_documented():
    used = all_compose_variables(ROOT / "docker-compose.yml")
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    for name in complete().env_values():
        assert name in used or f"`{name}`" in configuration, name


def test_secret_settings_are_marked_and_masked():
    values = complete(jellyfin_key="0" * 32, alert_url="http://ntfy.lan/x").masked()
    for key in ans.SECRET_KEYS:
        assert values.get(key) in {"(set)", "", None}, key
    assert "p-secret-1" not in str(values)
    assert values["OPENVPN_USER"] == "u"


def test_the_other_vpn_types_credentials_are_left_alone():
    ovpn = complete().env_values()
    assert "WIREGUARD_PRIVATE_KEY" not in ovpn
    wireguard = complete(
        vpn_kind="builtin",
        vpn_provider="mullvad",
        vpn_type="wireguard",
        wireguard_key="k",
        wireguard_addresses="10.0.0.2/32",
    ).env_values()
    assert "OPENVPN_PASSWORD" not in wireguard
    assert wireguard["VPN_TYPE"] == "wireguard"


def test_answers_can_be_read_back_from_an_env_file():
    original = complete(
        jellyfin_url="http://jf:8096",
        home_prefix="2003:abcd::/56",
        dirs={**ans.LIBRARY_DEFAULTS, "MEDIA_DIR_MUSIC": "Songs"},
    )
    text = envfile.apply_settings((ROOT / ".env.example").read_text(), original.env_values())
    back = Answers.from_env(envfile.parse_env(text))
    for name in (
        "lan_address",
        "lan_subnet",
        "vpn_user",
        "vpn_password",
        "qbit_password",
        "jellyfin_url",
        "home_prefix",
        "data_root",
        "timezone",
        "web_user",
    ):
        assert getattr(back, name) == getattr(original, name), name
    assert back.dirs["MEDIA_DIR_MUSIC"] == "Songs"
    assert back.vpn_kind == "ovpn"


def test_the_placeholder_is_not_taken_as_an_answer():
    back = Answers.from_env(envfile.parse_env((ROOT / ".env.example").read_text()))
    assert back.vpn_user == ""
    assert back.vpn_password == ""


# --- .env handling ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "written"),
    [
        ("plain", "plain"),
        ("", ""),
        ("2003:abcd::/56", "2003:abcd::/56"),
        ("has space", "'has space'"),
        ("p$ss#word", "'p$ss#word'"),
        ("a\\b", "'a\\b'"),
        ("Hörbücher", "'Hörbücher'"),
        ('say "hi"', "'say \"hi\"'"),
    ],
)
def test_values_are_quoted_so_compose_reads_them_back(value, written):
    assert envfile.format_value(value) == written
    assert envfile.parse_env(f"K={written}")["K"] == value


@pytest.mark.parametrize("value", ["it's", "two\nlines", "nul\x00"])
def test_values_that_cannot_be_written_are_refused(value):
    with pytest.raises(ValueError, match="cannot be written"):
        envfile.format_value(value)


def test_apply_settings_keeps_everything_else_and_appends_new_keys():
    text = "# comment\nA=1\n\nB=2\nA=dup\n"
    out = envfile.apply_settings(text, {"A": "9", "C": "new value"})
    assert out == "# comment\nA=9\n\nB=2\nC='new value'\n"


def test_missing_settings_come_with_the_templates_defaults():
    assert envfile.missing_settings("A=1\nB=2\nC=\n", "A=x\n") == {"B": "2", "C": ""}
    assert envfile.missing_settings("A=1\n", "A=\n") == {}


def test_a_changed_env_is_backed_up_and_both_are_private(tmp_path):
    path = tmp_path / ".env"
    now = datetime(2026, 11, 2, 9, 30, 5)
    assert envfile.write_env(path, "A=1\n", now) is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert envfile.write_env(path, "A=1\n", now) is None, "same content: no backup, no write"
    backup = envfile.write_env(path, "A=2\n", now)
    assert backup is not None
    assert backup.name == ".env.bak-20261102-093005"
    assert backup.read_text() == "A=1\n"
    assert path.read_text() == "A=2\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_the_backup_files_are_ignored_by_git():
    assert ".env.bak-*" in (ROOT / ".gitignore").read_text()


# --- texts ----------------------------------------------------------------------------------------------


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_both_languages_have_the_same_keys_and_placeholders():
    for key, by_lang in TEXTS.items():
        assert set(by_lang) == {"de", "en"}, key
        assert fields(by_lang["de"]) == fields(by_lang["en"]), key


def test_no_text_is_empty_and_german_differs_from_english():
    same = [k for k, v in TEXTS.items() if v["de"] == v["en"]]
    assert all(v["de"].strip() and v["en"].strip() for v in TEXTS.values())
    assert set(same) <= {"step.running_never"}, same


def test_every_text_key_the_code_uses_exists():
    import re

    keys = set()
    for path in (ROOT / "scripts" / "install" / "ytp_install").glob("*.py"):
        code = path.read_text(encoding="utf-8")
        keys |= set(
            re.findall(r'\b(?:tr|t|conv\.\w+|ctx\.tr)\(\s*(?:self\.lang,\s*)?"([a-z_0-9.]+)"', code)
        )
        keys |= set(
            re.findall(
                r'"((?:err|step|menu|done|update|checks|wait|env|backup|summary)\.[a-z_0-9.]+)"',
                code,
            )
        )
    missing = {
        k
        for k in keys
        if k not in TEXTS and not k.endswith(".") and k not in {"de", "en", "backup.py"}
    }
    assert not missing, sorted(missing)


def test_each_step_has_a_title_and_a_failure_hint():
    for step_id in steps.STEP_IDS:
        assert f"step.{step_id}.title" in TEXTS
        assert f"step.{step_id}.hint" in TEXTS


def test_every_question_has_the_texts_it_needs():
    for key in (
        "vpn_kind",
        "ovpn_path",
        "data_root",
        "web_user",
        "qbit_auto",
        "lan_confirm",
        "jellyfin_url",
        "alert_url",
        "home_prefix",
    ):
        assert f"{key}.why" in TEXTS, key
    assert t("de", "web_password.ask", n=12).count("12") == 1


def test_the_texts_never_ask_for_a_password_in_the_chat():
    assert "never" in TEXTS["done.help"]["en"].lower()
    assert "nie" in TEXTS["done.help"]["de"].lower()


# --- the system ----------------------------------------------------------------------------------------------


def test_debian_12_is_recognised():
    assert system.is_debian_12("ID=debian\nVERSION_CODENAME=bookworm\n")
    assert not system.is_debian_12("ID=debian\nVERSION_CODENAME=trixie\n")
    assert not system.is_debian_12("ID=ubuntu\nVERSION_CODENAME=bookworm\n")
    assert not system.is_debian_12("")


def test_the_route_and_subnet_are_read_from_ip_output():
    route = "1.1.1.1 via 192.168.178.1 dev ens18 src 192.168.178.123 uid 0 \\    cache"
    assert system.parse_route_source(route) == "192.168.178.123"
    addr = "1: lo    inet 127.0.0.1/8 scope host lo\n2: ens18    inet 192.168.178.123/24 brd 192.168.178.255 scope global ens18\n3: docker0    inet 172.17.0.1/16 scope global docker0\n"
    assert system.parse_subnet(addr, "192.168.178.123") == "192.168.178.0/24"
    assert system.parse_subnet(addr, "10.1.1.1") is None
    assert system.parse_route_source("RTNETLINK answers: Network is unreachable") is None


def test_timezone_names(tmp_path):
    (tmp_path / "Europe").mkdir()
    (tmp_path / "Europe" / "Berlin").write_text("x")
    assert system.timezone_known("Europe/Berlin", tmp_path)
    assert not system.timezone_known("Europe/Nowhere", tmp_path)
    assert not system.timezone_known("../etc/passwd", tmp_path)
    assert not system.timezone_known("Europe/../Europe/Berlin", tmp_path)
    zone = tmp_path / "tz"
    zone.write_text("Europe/Nowhere\n")
    assert system.detect_timezone(zone) == "Europe/Berlin"


# --- compose and the steps ---------------------------------------------------------------------------------------


def test_the_services_the_installer_waits_for_are_the_composes():
    from tests.helpers.compose import load_compose, services

    compose = services(load_compose(ROOT / "docker-compose.yml"))
    started = {name for name, svc in compose.items() if "profiles" not in svc}
    assert set(steps.SERVICES) == started


def test_compose_ps_is_read_in_both_formats():
    lines = '{"Service":"app","State":"running","Health":"healthy"}\n{"Service":"gluetun","State":"exited","Health":""}\n'
    array = '[{"Service":"app","State":"running","Health":"healthy"},{"Service":"gluetun","State":"exited","Health":""}]'
    assert steps.parse_compose_ps(lines) == steps.parse_compose_ps(array)
    assert steps.parse_compose_ps(lines)["gluetun"] == ("exited", "")
    assert steps.parse_compose_ps("") == {}
    assert steps.parse_compose_ps("not json") == {}


def test_a_service_counts_when_running_and_healthy_or_without_a_health_check():
    ok = {s: ("running", "healthy") for s in steps.SERVICES} | {"proxy": ("running", "")}
    assert steps.unhealthy_services(ok) == []
    bad = dict(ok) | {"gluetun": ("running", "starting"), "app": ("restarting", "")}
    assert steps.unhealthy_services(bad) == ["gluetun", "app"]
    assert steps.unhealthy_services({}) == list(steps.SERVICES)
