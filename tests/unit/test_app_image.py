"""The app's Dockerfile, requirements and compose service, checked as files.

CI has no Docker daemon, so the image itself cannot be built there: these tests pin what the
build and the compose service rely on, and tests/system/test_c3_app_image.py builds and runs
the real image on a host with a daemon (gated, like the other host tests).
"""

from __future__ import annotations

import re

import pytest

from tests.helpers.compose import environment_as_dict, load_compose, services
from tests.helpers.compose_cli import REPO_ROOT

APP = REPO_ROOT / "app"
DOCKERFILE = (APP / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = load_compose(REPO_ROOT / "docker-compose.yml")
APP_SERVICE = services(COMPOSE)["app"]


def instructions(name: str) -> list[str]:
    return re.findall(rf"^{name}\s+(.*)$", DOCKERFILE, re.MULTILINE)


def test_the_base_image_is_a_pinned_python_312_slim():
    (base,) = instructions("FROM")
    assert re.fullmatch(r"python:3\.12(\.\d+)?-slim-bookworm", base), base
    assert "latest" not in base


def test_the_image_installs_only_the_pinned_requirements_and_the_package():
    copies = instructions("COPY")
    assert copies == ["requirements.txt ./", "ytp ./ytp"]
    assert any("pip install -r requirements.txt" in line for line in DOCKERFILE.splitlines())


def test_the_image_has_a_health_check_on_the_health_endpoint():
    health = " ".join(re.findall(r"HEALTHCHECK.*?(?=\n\S|\Z)", DOCKERFILE, re.DOTALL))
    assert "http://127.0.0.1:8000/health" in health
    assert "--retries" in health


def test_the_container_runs_the_module_and_listens_on_8000():
    assert instructions("CMD") == ['["python", "-m", "ytp"]']
    assert instructions("EXPOSE") == ["8000"]


@pytest.mark.security
def test_the_image_does_not_switch_to_root_and_carries_no_secrets():
    assert "USER root" not in DOCKERFILE
    assert not re.search(r"(?i)^(ENV|ARG)\s+\w*(KEY|TOKEN|PASSWORD|SECRET)", DOCKERFILE, re.M)
    assert ".env" not in DOCKERFILE


def test_the_docker_context_leaves_out_caches():
    ignore = (APP / ".dockerignore").read_text(encoding="utf-8")
    assert "__pycache__" in ignore
    assert ".venv/" in ignore


def test_runtime_requirements_are_all_pinned_exactly():
    lines = [
        line
        for line in (APP / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==\d+(\.\d+)*", line), line
    names = {line.split("==")[0].lower() for line in lines}
    assert {"fastapi", "uvicorn"} <= names
    assert "pydantic-settings" not in names  # config is plain standard library
    assert "uvicorn" in names
    assert not any(n in names for n in ("uvloop", "httptools", "websockets", "watchfiles"))


def test_the_test_requirements_add_only_the_test_client_on_top_of_the_runtime_ones():
    text = (REPO_ROOT / "requirements-app.txt").read_text(encoding="utf-8")
    assert "-r app/requirements.txt" in text
    assert re.search(r"^httpx2==\d", text, re.MULTILINE)


def test_compose_builds_the_app_from_its_folder():
    assert APP_SERVICE["build"] == "./app"
    assert APP_SERVICE["image"].startswith("youtube-premium/app:")


def test_the_app_starts_with_the_default_stack_on_the_default_network():
    assert "profiles" not in APP_SERVICE
    for key in ("network_mode", "networks"):
        assert key not in APP_SERVICE, f"app must stay on the default bridge network ({key})"


@pytest.mark.security
def test_the_app_publishes_no_port_and_runs_as_the_media_owner():
    """Nothing outside this host reaches it until the reverse proxy with login exists (#29)."""
    assert "ports" not in APP_SERVICE
    assert "expose" not in APP_SERVICE
    assert (
        APP_SERVICE["user"] == "${PUID:?PUID must be set in .env}:${PGID:?PGID must be set in .env}"
    )
    assert not APP_SERVICE.get("privileged")
    assert "cap_add" not in APP_SERVICE


def test_compose_passes_the_app_what_its_config_reads():
    env = environment_as_dict(APP_SERVICE)
    assert {"LOG_LEVEL", "JELLYFIN_URL", "JELLYFIN_API_KEY"} <= set(env)
    assert {n for n in env if n.startswith("MEDIA_DIR_")} == {
        "MEDIA_DIR_MOVIES",
        "MEDIA_DIR_SERIES",
        "MEDIA_DIR_AUDIOBOOKS",
        "MEDIA_DIR_MUSIC",
        "MEDIA_DIR_PODCASTS",
        "MEDIA_DIR_YOUTUBE",
    }


def test_the_migrations_are_part_of_the_image():
    """The Dockerfile copies the whole ytp package; nothing may exclude the SQL files."""
    migrations = REPO_ROOT / "app" / "ytp" / "migrations"
    assert (migrations / "0001_initial.sql").is_file()
    dockerfile = (REPO_ROOT / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ytp ./ytp" in dockerfile
    ignored = (REPO_ROOT / "app" / ".dockerignore").read_text(encoding="utf-8")
    assert "sql" not in ignored
    assert "migrations" not in ignored


def test_the_web_ui_files_are_part_of_the_image():
    static = REPO_ROOT / "app" / "ytp" / "static"
    for name in ("index.html", "app.css", "app.js", "logic.js", "favicon.svg"):
        assert (static / name).is_file(), name
    ignored = (REPO_ROOT / "app" / ".dockerignore").read_text(encoding="utf-8")
    assert "static" not in ignored
    assert not (REPO_ROOT / "index.html").exists(), "the old draft at the repository root is gone"
