"""scripts/qbittorrent/setup_categories.py against a fake qBittorrent Web API on localhost."""

from __future__ import annotations

import http.server
import importlib.util
import json
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qbittorrent" / "setup_categories.py"
spec = importlib.util.spec_from_file_location("setup_categories", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

USER = "tester"
PASSWORD = "a long test password"  # a test value, not a secret


class FakeQbittorrent:
    """Just enough of the Web API: cookie login, categories, preferences. Records requests."""

    def __init__(self) -> None:
        self.categories: dict[str, dict[str, str]] = {}
        self.preferences: dict[str, object] = {
            "save_path": "/downloads/",
            "auto_tmm_enabled": False,
        }
        self.requests: list[tuple[str, str]] = []
        self.logins = 0
        # modern: 204 + no body / 401 (current qBittorrent); legacy: 200 "Ok." / 200 "Fails.";
        # banned: 403 for everyone (too many failed logins).
        self.login_style = "modern"

    def handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def reply(self, body: str, code: int = 200, headers: dict[str, str] | None = None):
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body.encode())

            def handle_any(self):
                size = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(size).decode(), keep_blank_values=True)
                one = {k: v[0] for k, v in form.items()}
                path = self.path.removeprefix("/api/v2/")
                fake.requests.append((path, self.path + "|" + json.dumps(one)))
                if path == "auth/login":
                    fake.logins += 1
                    ok = one == {"username": USER, "password": PASSWORD}
                    cookie = {"Set-Cookie": "SID=abc; HttpOnly; path=/"} if ok else {}
                    if fake.login_style == "banned":
                        return self.reply("Forbidden", 403)
                    if fake.login_style == "legacy":
                        return self.reply("Ok." if ok else "Fails.", headers=cookie)
                    if ok:
                        return self.reply("", 204, headers=cookie)
                    return self.reply("Unauthorized", 401)
                if "SID=abc" not in (self.headers.get("Cookie") or ""):
                    return self.reply("Forbidden", 403)
                if path == "torrents/categories":
                    return self.reply(json.dumps(fake.categories))
                if path in {"torrents/createCategory", "torrents/editCategory"}:
                    fake.categories[one["category"]] = {
                        "name": one["category"],
                        "savePath": one["savePath"],
                    }
                    return self.reply("")
                if path == "app/preferences":
                    return self.reply(json.dumps(fake.preferences))
                if path == "app/setPreferences":
                    fake.preferences.update(json.loads(one["json"]))
                    return self.reply("")
                return self.reply("not found", 404)

            do_GET = do_POST = handle_any

        return Handler


@pytest.fixture
def server() -> Iterator[tuple[FakeQbittorrent, str]]:
    fake = FakeQbittorrent()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield fake, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def run(url: str, *args: str, password: str = PASSWORD) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--url",
            url,
            "--user",
            USER,
            "--password-stdin",
            *args,
        ],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("style", ["modern", "legacy"])
def test_both_login_answers_count_as_success(server, style):
    """Current qBittorrent answers a good login with 204 and an EMPTY body (not 200 "Ok.");
    the script used to report "login refused" to the owner with the right password."""
    fake, url = server
    fake.login_style = style
    result = run(url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(fake.categories) == 4


@pytest.mark.security
@pytest.mark.parametrize("style", ["modern", "legacy"])
def test_a_wrong_password_is_refused_with_either_answer(server, style):
    fake, url = server
    fake.login_style = style
    result = run(url, password="wrong password")
    assert result.returncode == 1
    assert "login refused" in result.stderr
    assert fake.categories == {}


def test_a_401_mentions_the_port_mismatch_cause(server):
    """qBittorrent also answers 401 when the Host header's port is not its Web UI port
    (seen with a 127.0.0.1:18080->8080 mapping)."""
    _, url = server
    result = run(url, password="wrong password")
    assert "18080->8080" in result.stderr
    assert "Web UI port" in result.stderr


def test_a_banned_client_gets_the_wait_hint(server):
    fake, url = server
    fake.login_style = "banned"
    result = run(url)
    assert result.returncode == 1
    assert "too many failed logins" in result.stderr


def test_creates_the_four_categories_with_their_download_folders(server):
    fake, url = server
    result = run(url)
    assert result.returncode == 0, result.stderr
    assert {n: c["savePath"] for n, c in fake.categories.items()} == {
        "movies": "/data/downloads/movies",
        "series": "/data/downloads/series",
        "audiobooks": "/data/downloads/audiobooks",
        "music": "/data/downloads/music",
    }
    assert "created category movies" in result.stdout


def test_sets_default_and_incomplete_paths_and_automatic_management(server):
    fake, url = server
    run(url)
    assert fake.preferences["save_path"] == "/data/downloads"
    assert fake.preferences["temp_path_enabled"] is True
    assert fake.preferences["temp_path"] == "/data/downloads/incomplete"
    assert fake.preferences["auto_tmm_enabled"] is True
    assert fake.preferences["category_changed_tmm_enabled"] is True


def test_the_web_ui_settings_for_the_proxy_are_set_host_header_off_and_csrf_on(server):
    fake, url = server
    fake.preferences["web_ui_host_header_validation_enabled"] = True  # qBittorrent's default
    fake.preferences["web_ui_csrf_protection_enabled"] = False
    result = run(url)
    assert fake.preferences["web_ui_host_header_validation_enabled"] is False
    assert fake.preferences["web_ui_csrf_protection_enabled"] is True
    assert "set web_ui_host_header_validation_enabled = False" in result.stdout


def test_csrf_protection_is_never_switched_off(server):
    fake, url = server
    run(url)
    sent = [
        json.loads(body.split("|", 1)[1])["json"]
        for p, body in fake.requests
        if p == "app/setPreferences"
    ]
    assert all(json.loads(j).get("web_ui_csrf_protection_enabled", True) is True for j in sent)


def test_a_second_run_changes_nothing(server):
    fake, url = server
    run(url)
    fake.requests.clear()
    result = run(url)
    assert result.returncode == 0
    assert "nothing to change" in result.stdout
    writes = [p for p, _ in fake.requests if p.startswith(("torrents/create", "torrents/edit"))]
    assert writes == []
    assert "app/setPreferences" not in [p for p, _ in fake.requests]


def test_a_wrong_save_path_is_corrected_and_other_categories_are_kept(server):
    fake, url = server
    fake.categories = {
        "movies": {"name": "movies", "savePath": "/downloads/movies"},
        "my-own": {"name": "my-own", "savePath": "/somewhere"},
    }
    result = run(url)
    assert fake.categories["movies"]["savePath"] == "/data/downloads/movies"
    assert fake.categories["my-own"]["savePath"] == "/somewhere"
    assert "updated category movies" in result.stdout


def test_unrelated_preferences_are_not_touched(server):
    fake, url = server
    fake.preferences["upnp"] = True
    run(url)
    sent = [
        json.loads(body.split("|", 1)[1])["json"]
        for p, body in fake.requests
        if p == "app/setPreferences"
    ]
    assert len(sent) == 1
    assert "upnp" not in json.loads(sent[0])
    assert fake.preferences["upnp"] is True


@pytest.mark.security
def test_a_wrong_password_stops_before_any_change(server):
    fake, url = server
    result = run(url, password="wrong password")
    assert result.returncode == 1
    assert "login refused" in result.stderr
    assert fake.categories == {}
    assert "torrents/categories" not in [p for p, _ in fake.requests]


@pytest.mark.security
def test_the_password_never_appears_in_output_or_urls(server):
    fake, url = server
    result = run(url)
    assert PASSWORD not in result.stdout + result.stderr
    assert not [path for _, path in fake.requests if PASSWORD in path.split("|")[0]]


def test_dry_run_contacts_nothing(server):
    fake, url = server
    result = run(url, "--dry-run")
    assert result.returncode == 0
    assert "/data/downloads/audiobooks" in result.stdout
    assert fake.requests == []


def test_an_unreachable_qbittorrent_gets_a_clear_message():
    result = run("http://127.0.0.1:9")
    assert result.returncode == 1
    assert "cannot reach qBittorrent" in result.stderr
    assert "Traceback" not in result.stderr


def test_an_empty_password_is_refused(server):
    _, url = server
    result = run(url, password="")
    assert result.returncode == 1
    assert "no password" in result.stderr


def test_the_categories_match_the_download_folders_created_by_the_layout_script():
    layout = (ROOT / "scripts" / "host" / "create-layout.sh").read_text(encoding="utf-8")
    folders = re.search(r'DOWNLOAD_DIRS="([^"]+)"', layout)
    assert folders is not None
    assert set(folders.group(1).split()) == {*mod.CATEGORIES, "incomplete"}


def test_the_incomplete_folder_is_inside_downloads_but_is_not_a_category():
    assert mod.INCOMPLETE_DIR.startswith(mod.DOWNLOAD_ROOT + "/")
    assert "incomplete" not in mod.CATEGORIES


# --- --from-env: the password from .env instead of stdin ----------------------------------------


def run_from_env(url: str, env_text: str | None, tmp_path, *args: str):
    env_file = tmp_path / ".env"
    if env_text is not None:
        env_file.write_text(env_text)
    return subprocess.run(
        [
            sys.executable, "-I", str(SCRIPT), "--url", url, "--from-env",
            "--env-file", str(env_file), *args,
        ],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip


def test_from_env_reads_user_and_password_from_the_env_file(server, tmp_path):
    fake, url = server
    result = run_from_env(url, f"TZ=UTC\nQBIT_USER={USER}\nQBIT_PASSWORD='{PASSWORD}'\n", tmp_path)
    assert result.returncode == 0, result.stderr
    assert set(fake.categories) == {"movies", "series", "audiobooks", "music"}
    assert fake.logins == 1


def test_from_env_with_an_explicit_user_prefers_it(server, tmp_path):
    fake, url = server
    result = run_from_env(
        url, f"QBIT_USER=someone-else\nQBIT_PASSWORD={PASSWORD}\n", tmp_path, "--user", USER
    )
    assert result.returncode == 0, result.stderr
    assert fake.logins == 1


@pytest.mark.parametrize(
    "env_text", ["QBIT_PASSWORD=\n", "TZ=UTC\n", "# QBIT_PASSWORD=commented-out-value\n"]
)
def test_from_env_without_a_password_says_so_and_contacts_nothing(server, tmp_path, env_text):
    fake, url = server
    result = run_from_env(url, env_text, tmp_path)
    assert result.returncode == 1
    assert "QBIT_PASSWORD is empty or missing" in result.stderr
    assert fake.logins == 0


def test_from_env_with_a_missing_file_says_so(server, tmp_path):
    fake, url = server
    result = run_from_env(url, None, tmp_path)
    assert result.returncode == 1
    assert "cannot read" in result.stderr
    assert fake.logins == 0


@pytest.mark.security
def test_from_env_never_prints_the_password(server, tmp_path):
    _, url = server
    result = run_from_env(url, f"QBIT_USER={USER}\nQBIT_PASSWORD={PASSWORD}\n", tmp_path)
    assert PASSWORD not in result.stdout + result.stderr
    wrong = run_from_env(url, f"QBIT_USER={USER}\nQBIT_PASSWORD=not the password\n", tmp_path)
    assert wrong.returncode == 1
    assert "not the password" not in wrong.stdout + wrong.stderr


def test_from_env_and_password_stdin_exclude_each_other(server, tmp_path):
    _, url = server
    result = run_from_env(url, "QBIT_PASSWORD=x\n", tmp_path, "--password-stdin")
    assert result.returncode == 2
    assert "not allowed with" in result.stderr


def test_the_stdin_path_still_works_without_any_env_file(server):
    fake, url = server
    result = run(url)
    assert result.returncode == 0, result.stderr
    assert fake.logins == 1
