"""app/ytp/config.py: the environment becomes validated settings, or a complete error list."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.app import ENV
from tests.helpers.compose import environment_as_dict, load_compose, parse_env_file, services
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT
from ytp.config import LIBRARY_VARS, ConfigError, Settings, valid_folder_name


def settings(**overrides: str | None) -> Settings:
    env = {**ENV, **overrides}
    return Settings.from_env({k: v for k, v in env.items() if v is not None})


def problems(**overrides: str | None) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        settings(**overrides)
    return caught.value.problems


def test_defaults_with_only_the_required_library_folders():
    s = settings()
    assert s.log_level == "INFO"
    assert (s.host, s.port) == ("0.0.0.0", 8000)
    assert s.data_root == Path("/data")
    assert s.config_dir == Path("/config")
    assert s.jellyfin_url == ""
    assert not s.jellyfin_configured


def test_library_folders_keep_their_umlauts_and_resolve_below_data_root():
    s = settings(DATA_ROOT="/mnt/share")
    assert s.library_dirs["audiobooks"] == "Hörbücher"
    assert s.library_path("audiobooks") == Path("/mnt/share/Hörbücher")
    assert s.downloads_dir == Path("/mnt/share/downloads")


def test_every_missing_library_folder_is_reported_in_one_error():
    found = problems(MEDIA_DIR_MOVIES=None, MEDIA_DIR_YOUTUBE="  ")
    assert any("MEDIA_DIR_MOVIES" in p for p in found)
    assert any("MEDIA_DIR_YOUTUBE" in p for p in found)


def test_all_problems_are_listed_together():
    found = problems(LOG_LEVEL="loud", APP_PORT="http", DATA_ROOT="data", MEDIA_DIR_MUSIC=None)
    assert len(found) == 4


@pytest.mark.security
@pytest.mark.parametrize("name", ["../escape", "a/b", "..", ".", "-rf", "/abs", "x\\y", "a\nb"])
def test_library_folders_cannot_leave_data_root(name):
    assert any("single folder name" in p for p in problems(MEDIA_DIR_MOVIES=name))
    assert not valid_folder_name(name)


def test_two_libraries_cannot_share_a_folder():
    assert any("own folder" in p for p in problems(MEDIA_DIR_SERIES="Filme"))


@pytest.mark.parametrize(("raw", "expected"), [("debug", "DEBUG"), (" Warning ", "WARNING")])
def test_log_level_is_case_insensitive(raw, expected):
    assert settings(LOG_LEVEL=raw).log_level == expected


def test_unknown_log_level_is_refused():
    assert any("LOG_LEVEL" in p for p in problems(LOG_LEVEL="loud"))


@pytest.mark.parametrize("port", ["0", "65536", "web", "-1", ""])
def test_a_bad_port_is_refused(port):
    if port == "":  # empty means "not set": the default applies
        assert settings(APP_PORT=port).port == 8000
    else:
        assert any("APP_PORT" in p for p in problems(APP_PORT=port))


def test_relative_paths_are_refused():
    assert any("DATA_ROOT" in p for p in problems(DATA_ROOT="data"))
    assert any("CONFIG_DIR" in p for p in problems(CONFIG_DIR="config"))


def test_jellyfin_address_loses_a_trailing_slash_and_must_be_http():
    assert settings(JELLYFIN_URL="http://192.168.178.108:8096/").jellyfin_url == (
        "http://192.168.178.108:8096"
    )
    assert any("JELLYFIN_URL" in p for p in problems(JELLYFIN_URL="ftp://example"))
    assert any("JELLYFIN_URL" in p for p in problems(JELLYFIN_URL="just-a-name"))


def test_jellyfin_needs_both_address_and_key_to_count_as_configured():
    assert not settings(JELLYFIN_URL="http://j:8096").jellyfin_configured
    assert not settings(JELLYFIN_API_KEY="k").jellyfin_configured
    assert settings(JELLYFIN_URL="http://j:8096", JELLYFIN_API_KEY="k").jellyfin_configured


@pytest.mark.security
def test_the_api_key_never_shows_in_repr_str_or_summary():
    s = settings(JELLYFIN_URL="http://j:8096", JELLYFIN_API_KEY="s3cr3t-key-value")
    assert "s3cr3t-key-value" not in repr(s)
    assert "s3cr3t-key-value" not in str(s)
    assert "s3cr3t-key-value" not in str(s.summary())
    assert s.summary()["jellyfin_configured"] is True


def test_qbittorrent_defaults_to_gluetun_over_the_bridge():
    s = settings()
    assert (s.qbit_url, s.qbit_user, s.qbit_password) == ("http://gluetun:8080", "admin", "")
    assert not s.qbit_configured


def test_qbittorrent_settings_come_from_the_environment():
    s = settings(QBIT_URL="http://q.lan:9090/", QBIT_USER="mario", QBIT_PASSWORD="a password 12")
    assert (s.qbit_url, s.qbit_user, s.qbit_password) == (
        "http://q.lan:9090",
        "mario",
        "a password 12",
    )
    assert s.qbit_configured


@pytest.mark.parametrize("value", ["ftp://x", "gluetun:8080", "http://gluetun:8080/qbt"])
def test_a_bad_qbittorrent_url_is_reported_with_the_other_problems(value):
    found = problems(QBIT_URL=value, LOG_LEVEL="loud")
    assert any("QBIT_URL" in p for p in found)
    assert any("LOG_LEVEL" in p for p in found)


@pytest.mark.security
def test_the_qbittorrent_password_never_shows_in_repr_str_or_summary():
    s = settings(QBIT_PASSWORD="qbit-secret-value")
    for text in (repr(s), str(s), str(s.summary())):
        assert "qbit-secret-value" not in text
    assert s.summary()["qbit_configured"] is True


@pytest.mark.parametrize(
    ("value", "mask"), [(None, 0o002), ("002", 0o002), ("0027", 0o027), ("022", 0o022)]
)
def test_umask_is_an_octal_mask(value, mask):
    if mask is None:
        assert any("UMASK" in p for p in problems(UMASK=value))
    else:
        assert settings(UMASK=value).umask == mask


@pytest.mark.parametrize("value", ["abc", "999", "2", "00022", "-1"])
def test_a_bad_umask_is_reported(value):
    assert any("UMASK" in p for p in problems(UMASK=value))


def test_settings_are_immutable():
    with pytest.raises(AttributeError):
        settings().port = 1  # type: ignore[misc]


def test_the_environment_is_the_default_source(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LOG_LEVEL", "error")
    assert Settings.from_env().log_level == "ERROR"


def test_library_variables_match_env_example_and_the_compose_app_service():
    expected = set(LIBRARY_VARS.values())
    in_example = {n for n in parse_env_file(ENV_EXAMPLE) if n.startswith("MEDIA_DIR_")}
    app = environment_as_dict(services(load_compose(REPO_ROOT / "docker-compose.yml"))["app"])
    assert in_example == expected
    assert {n for n in app if n.startswith("MEDIA_DIR_")} == expected


def test_the_env_example_is_a_valid_configuration():
    example = {k: v for k, v in parse_env_file(ENV_EXAMPLE).items() if k != "DATA_ROOT"}
    assert Settings.from_env(example).library_dirs["audiobooks"] == "Hörbücher"


def test_the_youtube_probe_timeout_reaches_the_app_through_compose():
    """Regression (#73): the app read it, but compose never passed it, so `.env` did nothing."""
    app = environment_as_dict(services(load_compose(REPO_ROOT / "docker-compose.yml"))["app"])
    assert "YOUTUBE_PROBE_TIMEOUT" in app
    assert "YOUTUBE_PROBE_TIMEOUT" in parse_env_file(ENV_EXAMPLE)


def test_the_app_never_gets_the_host_side_settings():
    """`DATA_ROOT` in `.env` is the HOST path; compose mounts it at /data instead. The other
    three are dev-only (the image and the Caddyfile fix the port at 8000) (#73)."""
    app = environment_as_dict(services(load_compose(REPO_ROOT / "docker-compose.yml"))["app"])
    assert not {"APP_HOST", "APP_PORT", "CONFIG_DIR", "DATA_ROOT"} & set(app)


# --- networks podcast fetches never go to (issue #34 follow-up) --------------------------------


def test_the_blocked_networks_come_from_the_home_ip_and_the_extra_setting():
    settings = Settings.from_env(
        {
            **ENV,
            "HOME_PUBLIC_IP": "84.150.20.30, 2003:abcd:1200::/56",
            "FETCH_BLOCKED_NETWORKS": "2a02:1:2::/48",
        }
    )
    assert [str(n) for n in settings.fetch_blocked_networks] == [
        "84.150.20.30/32",
        "2003:abcd:1200::/56",
        "2a02:1:2::/48",
    ]
    assert Settings.from_env(ENV).fetch_blocked_networks == ()


def test_the_blocked_networks_are_never_shown():
    settings = Settings.from_env({**ENV, "HOME_PUBLIC_IP": "2003:abcd:1200::/56"})
    assert "2003" not in repr(settings)
    assert "2003" not in str(settings.summary())
    assert settings.summary()["fetch_blocked_networks"] == 1


@pytest.mark.parametrize("name", ["HOME_PUBLIC_IP", "FETCH_BLOCKED_NETWORKS"])
def test_a_bad_entry_is_named_by_position_not_by_value(name):
    with pytest.raises(ConfigError) as error:
        Settings.from_env({**ENV, name: "2003:abcd:1200::/56, secret-garbage"})
    assert name in str(error.value)
    assert "entry 2" in str(error.value)
    assert "secret-garbage" not in str(error.value)


# --- MAGNET_STALL_DAYS (#40) ---------------------------------------------------------


def test_magnet_stall_days_default_to_seven():
    assert settings().magnet_stall_days == 7


@pytest.mark.parametrize(("value", "expected"), [("0", 0), ("1", 1), ("14", 14), ("365", 365)])
def test_magnet_stall_days_accepts_zero_and_up_to_a_year(value, expected):
    assert settings(MAGNET_STALL_DAYS=value).magnet_stall_days == expected


@pytest.mark.parametrize("value", ["-1", "366", "seven", "1.5"])
def test_magnet_stall_days_rejects_anything_else(value):
    assert any("MAGNET_STALL_DAYS" in p for p in problems(MAGNET_STALL_DAYS=value))


# --- TRUSTED_PROXY (#76) ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["proxy", "caddy", "172.18.0.5", "::1"])
def test_a_trusted_proxy_name_or_address_is_accepted(name):
    assert settings(TRUSTED_PROXY=name).trusted_proxy == name


def test_an_empty_trusted_proxy_means_no_header_is_believed():
    assert settings(TRUSTED_PROXY="").trusted_proxy == ""
    assert settings(TRUSTED_PROXY="   ").trusted_proxy == ""
    assert settings(TRUSTED_PROXY=None).trusted_proxy == "proxy"
    assert settings(TRUSTED_PROXY="").summary()["trusted_proxy"] is None


@pytest.mark.parametrize("value", ["http://proxy", "proxy:8080", "pro xy", "a/b"])
def test_a_bad_trusted_proxy_is_refused(value):
    assert any("TRUSTED_PROXY" in p for p in problems(TRUSTED_PROXY=value))


# --- INTERNET_CHECK_HOST / INTERNET_CHECK_PORT (the dashboard's internet card) ---------


def test_the_internet_check_defaults_to_youtube_not_pypi():
    s = settings()
    assert (s.internet_check_host, s.internet_check_port) == ("www.youtube.com", 443)
    assert s.summary()["internet_check"] == "www.youtube.com:443"


@pytest.mark.parametrize(
    "host",
    ["example.org", "nas", "a-b.example.com", "EXAMPLE.org", "192.168.1.1", "::1", "2001:db8::1"],
)
def test_the_internet_check_accepts_host_names_and_addresses(host):
    assert settings(INTERNET_CHECK_HOST=host).internet_check_host == host


def test_the_internet_check_settings_are_used():
    s = settings(INTERNET_CHECK_HOST="1.1.1.1", INTERNET_CHECK_PORT="53")
    assert (s.internet_check_host, s.internet_check_port) == ("1.1.1.1", 53)


def test_an_empty_host_switches_the_check_off_but_an_unset_one_uses_the_default():
    assert settings(INTERNET_CHECK_HOST="").internet_check_host == ""
    assert settings(INTERNET_CHECK_HOST="   ").internet_check_host == ""
    assert settings(INTERNET_CHECK_HOST=None).internet_check_host == "www.youtube.com"
    assert settings(INTERNET_CHECK_HOST="").summary()["internet_check"] is None


@pytest.mark.parametrize(
    "host",
    [
        "https://example.org",
        "example.org:443",
        "example.org/path",
        "exa mple.org",
        "-bad.example.org",
        "bad-.example.org",
        "a..b",
        "example.org.",
        "x" * 64 + ".org",
        ("a" * 60 + ".") * 5 + "org",
        "ex_ample.org",
        "user@example.org",
    ],
)
def test_a_bad_internet_check_host_is_a_startup_error(host):
    assert any("INTERNET_CHECK_HOST" in p for p in problems(INTERNET_CHECK_HOST=host))


@pytest.mark.parametrize("port", ["1", "443", "65535"])
def test_the_internet_check_port_accepts_1_to_65535(port):
    assert settings(INTERNET_CHECK_PORT=port).internet_check_port == int(port)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "https", "44.3", "4 43"])
def test_a_bad_internet_check_port_is_a_startup_error(port):
    assert any("INTERNET_CHECK_PORT" in p for p in problems(INTERNET_CHECK_PORT=port))


def test_an_empty_internet_check_port_means_the_default():
    assert settings(INTERNET_CHECK_PORT="").internet_check_port == 443
