"""app/ytp/config.py: the environment becomes validated settings, or a complete error list."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.compose import environment_as_dict, load_compose, parse_env_file, services
from tests.helpers.compose_cli import ENV_EXAMPLE, REPO_ROOT
from ytp.config import LIBRARY_VARS, ConfigError, Settings, valid_folder_name

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


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
