"""UI fixes from the first real browser test (issue #43): versioned scripts, a form that never
submits natively, a real navigation, a clickable VPN bar, no false "watchdog not answering"."""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from starlette.testclient import TestClient

from tests.helpers.i18n import translations
from tests.helpers.node import NODE, needs_node
from ytp.config import Settings
from ytp.main import create_app
from ytp.web import STATIC_DIR, render_page, static_version

pytestmark = pytest.mark.unit

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    settings = Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config)})
    with TestClient(create_app(settings), base_url="http://nas.lan") as test_client:
        yield test_client


# --- versioned static URLs ----------------------------------------------------------------


def test_every_static_url_in_the_page_carries_the_version(client):
    page = client.get("/").text
    version = client.app.state.static_version
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    assert len(urls) >= 5, urls  # icon, css, three scripts
    assert all(u.endswith(f"?v={version}") for u in urls), urls
    assert VERSION_PLACEHOLDER not in page


VERSION_PLACEHOLDER = "__STATIC_VERSION__"


def test_the_page_names_its_own_version_and_the_server_says_the_same(client):
    page = client.get("/dashboard").text
    version = client.app.state.static_version
    assert f'<meta name="ytp-static-version" content="{version}">' in page
    answer = client.get("/api/version")
    assert answer.json() == {"version": version}
    assert answer.headers["cache-control"] == "no-store"


def test_the_page_is_never_cached_but_a_versioned_file_is_for_a_year(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/dashboard").headers["cache-control"] == "no-cache"
    version = client.app.state.static_version
    for name in ("app.js", "logic.js", "dashboard.js", "app.css"):
        with_version = client.get(f"/static/{name}?v={version}")
        assert with_version.status_code == 200
        assert with_version.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert client.get(f"/static/{name}").headers["cache-control"] == "no-cache"


def test_a_missing_file_is_a_plain_404_not_an_immutable_one(client):
    answer = client.get("/static/nope.js?v=abc")
    assert answer.status_code == 404
    assert "immutable" not in answer.headers.get("cache-control", "")


def test_the_version_changes_exactly_when_a_file_does(tmp_path):
    (tmp_path / "a.js").write_text("one")
    (tmp_path / "b.css").write_text("two")
    first = static_version(tmp_path)
    assert static_version(tmp_path) == first
    (tmp_path / "a.js").write_text("one!")
    changed = static_version(tmp_path)
    assert changed != first
    (tmp_path / "a.js").write_text("one")
    assert static_version(tmp_path) == first, "back to the same content, back to the same version"
    (tmp_path / "c.svg").write_text("<svg/>")
    assert static_version(tmp_path) != first, "a new file counts too"


def test_render_page_versions_only_static_files_of_this_site():
    template = (
        '<meta name="v" content="__STATIC_VERSION__"><link href="/static/a.css">'
        '<script src="/static/b.js"></script><a href="/dashboard">x</a>'
        '<a href="https://example.org/static/c.js">y</a><img src="/static/p.png">'
    )
    page = render_page(template, "abc123def456")
    assert 'content="abc123def456"' in page
    assert 'href="/static/a.css?v=abc123def456"' in page
    assert 'src="/static/b.js?v=abc123def456"' in page
    assert 'href="/dashboard"' in page
    assert "https://example.org/static/c.js" in page, "another site's address is not ours to change"


def test_the_static_files_still_cannot_be_escaped_with_a_version_query(client):
    version = client.app.state.static_version
    for path in ("/static/../config.py", "/static/%2e%2e/config.py", "/static/..%2fconfig.py"):
        answer = client.get(f"{path}?v={version}")
        assert answer.status_code in {404, 307, 308}
        assert b"class Settings" not in answer.content


# --- the form never submits natively ---------------------------------------------------------


def test_no_form_has_a_get_fallback_every_form_goes_to_the_reload_page():
    forms = re.findall(r"<form [^>]*>", HTML)
    assert len(forms) >= 2
    for form in forms:
        assert 'method="post"' in form, form
        assert 'action="/reload-needed"' in form, form


def test_a_native_submit_lands_on_a_page_that_says_to_reload_and_echoes_nothing(client):
    answer = client.post(
        "/reload-needed",
        data={"link": "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "mode": "youtube"},
    )
    assert answer.status_code == 400
    assert "aqz-KE-bpKQ" not in answer.text
    assert "neu laden" in answer.text
    assert "reload" in answer.text.lower()
    assert answer.headers["cache-control"] == "no-store"
    assert client.app.state.db is not None
    assert client.get("/api/jobs").json()["counts"]["queued"] == 0, "no job was made"


@pytest.mark.parametrize(
    "headers",
    [
        {
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
        },  # what a browser sends for a native form post
        {"Origin": "null"},
        {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
        {},
    ],
)
def test_the_reload_page_answers_a_native_form_post_whatever_origin_it_carries(client, headers):
    """A browser sends a native form post with `Origin: null` (Referrer-Policy: no-referrer): the
    same-origin refusal would show JSON instead of the page that says what to do. The page changes
    nothing and reads nothing, so it is exempt; every other POST stays protected."""
    answer = client.post(
        "/reload-needed", data={"link": "https://youtu.be/dQw4w9WgXcQ"}, headers=headers
    )
    assert answer.status_code == 400
    assert "neu laden" in answer.text
    assert "dQw4w9WgXcQ" not in answer.text
    assert client.get("/api/jobs").json()["counts"]["queued"] == 0


@pytest.mark.security
@pytest.mark.parametrize(
    "path", ["/api/magnet", "/api/youtube/download", "/api/subscriptions", "/api/alerts/ack-all"]
)
def test_other_posts_with_a_null_origin_are_still_refused(client, path):
    answer = client.post(path, json={}, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert answer.status_code == 403
    assert "does not come from this site" in answer.text


def test_the_scripts_stop_a_native_submit_before_anything_else_can_fail():
    guard = APP_JS.index('document.addEventListener("submit"')
    assert guard < APP_JS.index("function t("), "the guard is the first thing the script does"
    assert "preventDefault" in APP_JS[guard : guard + 160]
    assert "true);" in APP_JS[guard : guard + 200], "in the capture phase, before any handler"


def test_a_warning_is_visible_until_the_scripts_have_run_and_they_hide_it():
    warning = re.search(r'<p id="js-warning"[^>]*>(.*?)</p>', HTML, re.S)
    assert warning
    assert "hidden" not in warning.group(0).split(">")[0], "visible by default"
    text = warning.group(1)
    assert "neu laden" in text
    assert "reload" in text.lower()
    assert 'getElementById("js-warning")' in APP_JS or '$("js-warning").hidden = true' in APP_JS
    assert APP_JS.rindex('$("js-warning").hidden = true') > APP_JS.index("startHowto();")


def test_an_old_cached_dashboard_script_cannot_take_the_form_down():
    assert "window.Dash ?" in APP_JS
    assert "if (dash) dash.render();" in APP_JS
    assert "if (dash) dash.start();" in APP_JS
    assert re.search(r"try \{\s*dash = window\.Dash", APP_JS)


def test_an_open_page_notices_an_update_and_offers_a_reload():
    assert "/api/version" in APP_JS
    assert 'meta[name="ytp-static-version"]' in APP_JS
    assert 'id="update-banner"' in HTML
    assert 'id="update-banner-reload"' in HTML
    assert "window.location.reload()" in APP_JS


# --- the navigation ---------------------------------------------------------------------------


def nav_html() -> str:
    nav = re.search(r'<nav id="topnav".*?</nav>', HTML, re.S)
    assert nav
    return nav.group(0)


def test_there_is_a_real_navigation_with_its_places_in_order():
    nav = nav_html()
    items = re.findall(r'id="(nav-[a-z]+|howto-open)"', nav)
    # the accounts tab (#44) is for admins only: the style sheet hides it from everybody else
    assert items == ["nav-input", "nav-dashboard", "nav-subs", "nav-accounts", "howto-open"]
    assert 'id="nav-accounts" class="nav-item" href="/accounts"' in nav
    assert "data-admin-only" in nav.split('id="nav-accounts"')[1].split(">")[0]
    assert 'href="/"' in nav
    assert 'href="/dashboard"' in nav
    assert 'href="/#subscriptions"' in nav
    assert 'id="subscriptions"' in HTML, "the subscriptions link has a target"
    assert HTML.index('id="topnav"') < HTML.index('id="form"')


def test_the_navigation_reads_eingabe_dashboard_abonnements_anleitung_and_its_english():
    t = translations(HTML)
    assert [t["de"][k] for k in ("nav.input", "nav.dashboard", "nav.subs", "howto.button")] == [
        "Eingabe",
        "Dashboard",
        "Abonnements",
        "Anleitung",
    ]
    assert [t["en"][k] for k in ("nav.input", "nav.dashboard", "nav.subs", "howto.button")] == [
        "Input",
        "Dashboard",
        "Subscriptions",
        "How to",
    ]


def test_the_guide_is_a_nav_item_and_the_language_switch_sits_at_the_bottom():
    assert 'id="howto-open"' in nav_html()
    assert 'id="lang"' not in nav_html(), "the language is not one of the tabs"
    assert HTML.index('id="topnav"') < HTML.index('id="lang"'), (
        "the language is below the navigation"
    )
    assert 'id="nav-link"' not in HTML


def test_the_navigation_is_big_enough_to_touch_and_marks_the_current_page():
    assert re.search(r"\.nav-item \{[^}]*min-height: 44px", CSS, re.S)
    assert '.nav-item[aria-current="page"]' in CSS
    assert "overflow-x: auto" in re.search(r"\.topnav \{[^}]*\}", CSS, re.S).group(0), (
        "phones scroll it"
    )


# --- the VPN bar --------------------------------------------------------------------------------


def test_the_vpn_banner_and_indicator_link_to_the_dashboard_part():
    banner = re.search(r'<div id="vpn".*?\n</div>', HTML, re.S)
    assert banner
    assert 'class="vpn-banner vpn-unknown"' in banner.group(0)
    assert "hidden" in banner.group(0).split(">")[0], "no banner for a normal load"
    link = re.search(
        r'<a id="vpn-link" class="vpn-link" href="/dashboard#vpn">(.*?)</a>',
        banner.group(0),
        re.S,
    )
    assert link, "the link wraps the dot, the text and the hint"
    inner = link.group(1)
    assert 'id="vpn-text"' in inner
    assert 'id="vpn-detail"' in inner
    assert 'class="vpn-dot"' in inner
    assert 'id="dash-vpn"' in HTML, "and the link has a target on the dashboard"
    assert re.search(r"\.vpn-banner \.vpn-link \{[^}]*min-height: 44px", CSS, re.S)
    indicator = re.search(r'<a id="vpn-indicator"[^>]*>', HTML)
    assert indicator, "the calm state is a dot beside the language switch"
    assert 'href="/dashboard#vpn"' in indicator.group(0)
    assert 'class="vpn-indicator vpn-unknown"' in indicator.group(0)
    assert "data-auth-only" in indicator.group(0)


def test_the_problem_banner_and_the_indicator_keep_their_roles():
    assert 'class="vpn-banner vpn-unknown" role="status"' in HTML
    assert "hidden" in re.search(r'<div id="vpn"[^>]*>', HTML).group(0)
    assert ".vpn-indicator:not(.vpn-ok) .vpn-dot" in CSS
    assert ".vpn-indicator:not(.vpn-ok) .vpn-dot { background: var(--danger-bg); }" in CSS
    assert ".vpn-banner .vpn-dot" in CSS


# --- "the watchdog does not answer" in the first minute --------------------------------------


class AwayWatchdog:
    def fetch_status(self):
        return {"state": "unknown", "reason": "the watchdog does not answer"}


class Watchdog:
    def __init__(self, status):
        self.status = status

    def fetch_status(self):
        return self.status


def test_a_watchdog_that_does_not_answer_yet_is_starting_in_the_first_minute(client):
    client.app.state.watchdog_status = AwayWatchdog()
    body = client.get("/api/vpn").json()
    assert body["state"] == "starting"
    assert "just started" in body["reason"]


def test_after_the_first_minute_it_is_unknown_again(client):
    client.app.state.watchdog_status = AwayWatchdog()
    client.app.state.started_at -= 61
    assert client.get("/api/vpn").json()["state"] == "unknown"


def test_a_real_answer_is_never_changed_by_the_grace(client):
    for status in (
        {"state": "ok", "egress": "ok"},
        {"state": "down", "paused": True},
        {"state": "blind"},
        {"state": "starting", "reason": "from the watchdog"},
    ):
        client.app.state.watchdog_status = Watchdog(status)
        assert client.get("/api/vpn").json() == status


def test_without_a_watchdog_configured_it_stays_unknown(client):
    client.app.state.watchdog_status = None
    assert client.get("/api/vpn").json()["state"] == "unknown"


# --- the pieces in a fake DOM (needs node) ---------------------------------------------------


def smoke(path: str, hash_: str, dashboard: dict | None = None) -> dict:
    ids = sorted(set(re.findall(r'\bid="([\w-]+)"', HTML)) - {"i18n"})
    scenario = {
        "ids": ids,
        "viewNodes": ["input", "dashboard"],
        "dictionary": translations(HTML)["en"],
        "answers": {
            "/api/alerts": {"alerts": [], "unread": 0},
            "/api/dashboard": dashboard
            or {
                "active": [],
                "recent": [],
                "vpn": {"current": {"state": "down", "paused": True}, "history": []},
                "disk": None,
                "deploy": None,
            },
        },
        "click": None,
    }
    result = subprocess.run(
        [
            NODE,
            str(STATIC_DIR.parents[2] / "tests/helpers/dashboard_smoke.js"),
            str(STATIC_DIR),
            path,
            hash_,
            json.dumps(scenario),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_opening_dashboard_vpn_brings_the_vpn_part_into_view_and_explains_the_state():
    out = smoke("/dashboard", "#vpn")
    section = out["nodes"]["dash-vpn"]
    assert section["scrolled"] is True
    assert "alert-target" in section["cls"]
    assert out["nodes"]["dash-vpn-explain"]["text"], "an explanation is shown for the state"
    assert out["nodes"]["nav-dashboard"]["attrs"].get("aria-current") == "page"


@needs_node
def test_the_dashboard_without_the_hash_does_not_scroll_to_the_vpn_part():
    out = smoke("/dashboard", "")
    assert out["nodes"]["dash-vpn"]["scrolled"] is False
    assert "alert-target" not in out["nodes"]["dash-vpn"]["cls"]


@needs_node
@pytest.mark.parametrize(
    ("state", "contains"),
    [("ok", "VPN is running"), ("down", "VPN"), ("blind", "watchdog"), ("starting", "watchdog")],
)
def test_every_vpn_state_gets_an_explanation(state, contains):
    dashboard = {
        "active": [], "recent": [], "disk": None, "deploy": None,
        "vpn": {"current": {"state": state}, "history": []},
    }  # fmt: skip
    out = smoke("/dashboard", "#vpn", dashboard)
    assert contains.lower() in out["nodes"]["dash-vpn-explain"]["text"].lower()


@needs_node
def test_the_subscriptions_tab_is_the_current_one_when_its_anchor_is_open():
    out = smoke("/", "#subscriptions")
    assert out["nodes"]["nav-subs"]["attrs"].get("aria-current") == "page"
    assert "aria-current" not in out["nodes"]["nav-input"]["attrs"]
