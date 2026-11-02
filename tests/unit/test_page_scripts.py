"""The page's real scripts against the real index.html in a small DOM (needs node).

`tests/helpers/page_harness.js` loads index.html, runs dashboard.js and app.js as the browser does
and reports script errors, the requests made and what the page shows. This is what found that
`renderHowtoSources` was still called after its function had been removed: `init()` threw before it
attached a single handler, the warning stayed on the page, and the owner's form was submitted
natively (issue #43). The unit tests of the helpers and of dashboard.js never ran `app.js`.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from ytp.web import STATIC_DIR

pytestmark = pytest.mark.unit

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed (CI installs it)")
HARNESS = STATIC_DIR.parents[2] / "tests" / "helpers" / "page_harness.js"
VIDEO = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
PLACEHOLDER = "__STATIC_VERSION__"  # the raw file; the server fills it in

PROBE = {
    "video_id": "aqz-KE-bpKQ",
    "url": VIDEO,
    "title": "Big Buck Bunny",
    "channel": "Blender",
    "channel_id": "UCSMOQeBJ2RAnuFungnQOxLg",
    "duration": 635,
    "duration_text": "10:35",
    "thumbnail": "",
}
ANSWERS = {
    "/api/vpn": {"state": "ok", "egress": "ok", "egress_country": "Switzerland"},
    "/api/jobs": {"jobs": [], "counts": {}},
    "/api/subscriptions": {"subscriptions": []},
    "/api/podcasts": {"podcasts": []},
    "/api/alerts": {"alerts": [], "unread": 0},
    "/api/version": {"version": PLACEHOLDER},
    "/api/dashboard": {
        "active": [],
        "recent": [],
        "disk": None,
        "deploy": None,
        "vpn": {"current": {"state": "ok"}, "history": []},
    },
    "POST /api/youtube/probe": PROBE,
    "POST /api/youtube/download": {
        "id": 1,
        "video_id": "aqz-KE-bpKQ",
        "type": "youtube",
        "target": "video",
        "status": "queued",
        "duplicate": False,
    },
}


def page(path="/", hash_="", language="en-US", steps=None, answers=None) -> dict:
    result = subprocess.run(
        [
            NODE,
            str(HARNESS),
            str(STATIC_DIR),
            path,
            hash_,
            language,
            json.dumps({**ANSWERS, **(answers or {})}),
            json.dumps(steps or []),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
@pytest.mark.parametrize(
    ("path", "hash_", "language"),
    [
        ("/", "", "en-US"),
        ("/", "", "de-DE"),
        ("/", "#subscriptions", "en"),
        ("/dashboard", "", "en-GB"),
        ("/dashboard", "#vpn", "de"),
        ("/dashboard", "#alert-7", "en"),
    ],
)
def test_the_scripts_run_to_the_end_without_an_error(path, hash_, language):
    out = page(path, hash_, language)
    assert out["errors"] == [], out["errors"]
    assert out["warningHidden"] is True, "the 'scripts did not load' notice is hidden at the end"


@needs_node
def test_the_page_asks_the_server_for_what_it_shows():
    fetched = page("/")["fetched"]
    for path in ("/api/vpn", "/api/jobs?limit=50", "/api/subscriptions", "/api/podcasts"):
        assert ["GET", path] in fetched
    assert ["GET", "/api/dashboard"] not in fetched
    assert ["GET", "/api/dashboard"] in page("/dashboard")["fetched"]


@needs_node
def test_a_youtube_link_is_looked_up_and_submitted_without_a_native_submit():
    out = page(
        "/",
        steps=[
            {"set": "#link", "value": VIDEO},
            {"fire": "#link", "type": "input"},
            {"tick": 1},
            {"read": "#f-yt-title"},
            {"read": "#f-channel"},
            {"submit": "#form"},
            {"read": "#result"},
        ],
    )
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/youtube/probe"] in out["fetched"]
    assert out["reads"]["#f-yt-title"]["value"] == "Big Buck Bunny"
    assert out["reads"]["#f-channel"]["value"] == "Blender"
    assert ["POST", "/api/youtube/download"] in out["fetched"], "the job was requested"
    assert out["prevented"]["#form"] is True, "the browser's own form submission is stopped"
    assert "Job #1" in out["reads"]["#result"]["text"]


@needs_node
def test_the_guard_stops_a_native_submit_even_if_the_rest_of_the_script_is_broken(tmp_path):
    """Whatever else fails, a submit must never reach the browser (it would put the link in the
    address): run only the first lines of app.js."""
    code = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    cut = code.index("function t(")
    broken = tmp_path / "static"
    shutil.copytree(STATIC_DIR, broken)
    (broken / "app.js").write_text(
        code[:cut] + "\n  throw new Error('broken on purpose');\n})();\n"
    )
    result = subprocess.run(
        [
            NODE,
            str(HARNESS),
            str(broken),
            "/",
            "",
            "en-US",
            "{}",
            json.dumps([{"submit": "#form"}]),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    out = json.loads(result.stdout)
    assert out["errors"], "the broken script was reported"
    assert out["prevented"]["#form"] is True
    assert out["warningHidden"] is False, "and the page still says that its scripts are not alive"


@needs_node
def test_a_language_switch_and_a_vpn_answer_render_without_error():
    out = page(
        "/",
        steps=[
            {"click": "#lang"},
            {"read": "#vpn-text"},
            {"click": "#lang"},
            {"read": "#vpn-text"},
        ],
    )
    assert out["errors"] == [], out["errors"]
    assert out["reads"]["#vpn-text"]["text"], "the bar says something"


@needs_node
def test_a_newer_server_version_shows_the_reload_banner_and_the_same_version_does_not():
    newer = page("/", steps=[{"tick": 1}], answers={"/api/version": {"version": "ffffffffffff"}})
    assert newer["banner"] is False, "the banner is shown (hidden is false)"
    same = page("/", steps=[{"tick": 1}])
    assert same["banner"] is True
    offline = page("/", steps=[{"tick": 1}], answers={"/api/version": None})
    assert offline["banner"] is True, "a server that does not answer is no reason for a banner"


@needs_node
def test_the_page_names_its_version_in_the_meta_tag():
    assert page("/")["version"] == PLACEHOLDER
