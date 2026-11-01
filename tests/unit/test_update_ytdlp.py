# ruff: noqa: E501
"""scripts/ops/update_ytdlp.py and the pinned versions (issue #30)."""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import re
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers.compose import parse_env_file

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "update_ytdlp.py"
spec = importlib.util.spec_from_file_location("ytp_update_ytdlp", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["ytp_update_ytdlp"] = mod
spec.loader.exec_module(mod)

REQUIREMENTS = "fastapi==0.162.0\nyt-dlp==2026.10.26\nuvicorn==0.54.0\n"


def release(version, yanked=False, uploaded="2026-10-26T12:00:00.000000Z"):
    return {version: [{"yanked": yanked, "upload_time_iso_8601": uploaded}]}


@pytest.fixture
def pypi() -> Iterator[dict]:
    """A local server that answers like PyPI's JSON API with whatever `state["body"]` holds."""
    state: dict = {"body": {"releases": {}}, "status": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/json"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def req(tmp_path) -> Path:
    path = tmp_path / "requirements.txt"
    path.write_text(REQUIREMENTS)
    return path


def run(req: Path, pypi: dict, *args: str) -> int:
    return mod.main(["--requirements", str(req), "--index-url", pypi["url"], *args])


def test_a_current_pin_is_reported_with_exit_0(req, pypi, capsys):
    pypi["body"] = {"releases": {**release("2026.10.20"), **release("2026.10.26")}}
    assert run(req, pypi) == 0
    assert "is current" in capsys.readouterr().out


def test_a_newer_release_is_exit_10_and_nothing_is_written(req, pypi, capsys):
    pypi["body"] = {"releases": {**release("2026.10.26"), **release("2026.11.03")}}
    assert run(req, pypi) == 10
    assert "can be updated" in capsys.readouterr().out
    assert req.read_text() == REQUIREMENTS


def test_apply_rewrites_only_the_pin_line(req, pypi, capsys):
    pypi["body"] = {"releases": {**release("2026.10.26"), **release("2026.11.03")}}
    assert run(req, pypi, "--apply") == 0
    assert req.read_text() == REQUIREMENTS.replace("2026.10.26", "2026.11.03")
    out = capsys.readouterr().out
    assert "make test-unit" in out
    assert "docker compose build app" in out
    assert not list(req.parent.glob("*.tmp"))


def test_apply_with_dry_run_writes_nothing(req, pypi, capsys):
    pypi["body"] = {"releases": {**release("2026.10.26"), **release("2026.11.03")}}
    assert run(req, pypi, "--apply", "--dry-run") == 10
    assert "yt-dlp==2026.10.26 -> yt-dlp==2026.11.03" in capsys.readouterr().out
    assert req.read_text() == REQUIREMENTS


def test_apply_is_a_no_op_when_current(req, pypi):
    pypi["body"] = {"releases": release("2026.10.26")}
    assert run(req, pypi, "--apply") == 0
    assert req.read_text() == REQUIREMENTS


def test_versions_compare_as_numbers_and_dotted_releases_count(req, pypi):
    pypi["body"] = {"releases": {**release("2026.10.26"), **release("2026.10.26.1")}}
    assert run(req, pypi) == 10
    pypi["body"] = {
        "releases": {**release("2026.9.5".replace("9.5", "09.05")), **release("2026.10.26")}
    }
    assert run(req, pypi) == 0


def test_yanked_prereleases_and_empty_releases_are_ignored(req, pypi):
    pypi["body"] = {
        "releases": {
            **release("2026.10.26"),
            **release("2026.12.01", yanked=True),
            **release("2027.01.01.dev1"),
            "2027.02.02": [],
        }
    }
    assert run(req, pypi) == 0


@pytest.mark.parametrize("body", [{}, {"releases": []}, {"releases": {"x": 1}}, []])
def test_an_unexpected_answer_is_exit_2(req, pypi, body, capsys):
    pypi["body"] = body
    assert run(req, pypi) == 2
    assert "error:" in capsys.readouterr().err


def test_a_server_error_or_no_server_is_exit_2(req, pypi):
    pypi["status"] = 500
    assert run(req, pypi) == 2
    assert mod.main(["--requirements", str(req), "--index-url", "http://127.0.0.1:1/json"]) == 2


def test_a_requirements_file_without_the_pin_is_exit_2(tmp_path, pypi):
    path = tmp_path / "r.txt"
    path.write_text("fastapi==0.162.0\n")
    assert run(path, pypi) == 2


def test_the_real_requirements_file_can_be_read_by_the_script():
    text = (ROOT / "app" / "requirements.txt").read_text()
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}(\.\d+)?", mod.pinned_version(text))


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


# --- pinned image tags ------------------------------------------------------------------------------


def test_every_image_tag_in_env_example_is_an_exact_version():
    tags = {k: v for k, v in parse_env_file(ROOT / ".env.example").items() if k.endswith("_TAG")}
    assert set(tags) == {"GLUETUN_TAG", "QBITTORRENT_TAG", "CADDY_TAG"}
    for name, value in tags.items():
        assert value not in {"latest", "stable", "edge", "dev", "nightly", "main"}, name
        assert re.search(r"\d+\.\d+\.\d+", value), f"{name}={value} is not a version"
