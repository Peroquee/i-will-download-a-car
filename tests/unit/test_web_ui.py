"""The submission form (issue #25): serving, same-origin protection, content rules, i18n, and
the browser-side helpers (run with node when it is installed; skipped otherwise)."""

# ruff: noqa: E501  (long literals in tables and the node one-liners)

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from ytp.config import Settings
from ytp.main import create_app
from ytp.metadata import METADATA_TYPES
from ytp.web import STATIC_DIR, cross_site

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
LOGIC_JS = (STATIC_DIR / "logic.js").read_text(encoding="utf-8")
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


@pytest.fixture
def client(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    settings = Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config)})
    with TestClient(create_app(settings), base_url="http://nas.lan") as test_client:
        yield test_client


# --- serving ----------------------


def test_the_form_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert "<html" in response.text
    assert 'src="/static/app.js"' in response.text


@pytest.mark.parametrize(
    "name", ["app.css", "app.js", "logic.js", "favicon.svg", "favicon-alert.svg"]
)
def test_the_static_files_are_served(client, name):
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert len(response.content) > 100


@pytest.mark.security
@pytest.mark.parametrize(
    "path",
    ["/static/", "/static", "/static/nope.js", "/static/../config.py", "/static/%2e%2e/config.py",
     "/static/..%2fconfig.py", "/static/../../etc/passwd"],
)  # fmt: skip
def test_nothing_but_the_four_files_comes_out_of_static(client, path):
    response = client.get(path)
    assert response.status_code in {404, 307, 308}
    assert b"class Settings" not in response.content


def test_there_is_no_api_documentation_page(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- security headers ----------------------


@pytest.mark.security
@pytest.mark.parametrize("path", ["/", "/static/app.js", "/health", "/api/vpn", "/nope"])
def test_every_answer_carries_the_security_headers(client, path):
    headers = client.get(path).headers
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"


@pytest.mark.security
def test_no_cors_headers_are_ever_sent(client):
    response = client.options(
        "/api/magnet",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in client.get("/api/vpn").headers


# --- same-origin check ------------------------------------------------------------------------------------


def post_probe(client, **headers):
    """A state-changing request whose body is invalid on purpose: 422 = it got through, 403 = refused."""
    return client.post("/api/youtube/probe", json={"url": "not a link"}, headers=headers)


@pytest.mark.security
@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl and scripts send neither header
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://nas.lan"},
        {"Origin": "http://nas.lan"},
        {"Origin": "HTTP://NAS.LAN"},
    ],
)
def test_requests_from_this_site_or_from_programs_pass(client, headers):
    assert post_probe(client, **headers).status_code == 422


@pytest.mark.security
@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site", "Origin": "http://nas.lan"},
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://evil.example"},
        {"Origin": "http://evil.example"},
        {"Origin": "http://nas.lan:8081"},  # another port is another origin
        {"Origin": "http://nas.lan.evil.example"},
        {"Origin": "null"},
        {"Origin": ""},
        {"Origin": "http://"},
    ],
)
def test_requests_from_other_sites_are_refused_before_anything_happens(client, headers):
    response = post_probe(client, **headers)
    assert response.status_code == 403
    assert response.json() == {
        "detail": "This request does not come from this site and was refused."
    }
    assert "content-security-policy" in response.headers


@pytest.mark.security
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/magnet", {"magnet": MAGNET, "metadata": {"type": "music", "artist": "a", "album": "b"}}),
        ("POST", "/api/youtube/download", {"url": "https://youtu.be/dQw4w9WgXcQ", "channel": "c", "title": "t"}),
        ("POST", "/api/youtube/probe", {"url": "https://youtu.be/dQw4w9WgXcQ"}),
    ],
)  # fmt: skip
def test_every_state_changing_route_is_protected_and_nothing_is_created(client, method, path, body):
    response = client.request(method, path, json=body, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert client.app.state.db.query("SELECT COUNT(*) FROM jobs")[0][0] == 0


@pytest.mark.security
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_the_other_unsafe_methods_are_checked_too(client, method):
    response = client.request(method, "/api/magnet", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_reading_is_not_restricted_by_origin(client):
    assert client.get("/api/vpn", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_the_host_is_what_the_origin_is_compared_with(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    settings = Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config)})
    with TestClient(create_app(settings), base_url="http://192.168.178.123:8081") as c:
        ok = c.post(
            "/api/youtube/probe",
            json={"url": "x"},
            headers={"Origin": "http://192.168.178.123:8081"},
        )
        assert ok.status_code == 422
        refused = c.post(
            "/api/youtube/probe", json={"url": "x"}, headers={"Origin": "http://192.168.178.123"}
        )
        assert refused.status_code == 403


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, False),
        ({b"sec-fetch-site": b"same-origin"}, False),
        ({b"sec-fetch-site": b"Cross-Site"}, True),
        ({b"origin": b"http://h", b"host": b"h"}, False),
        ({b"origin": b"http://h", b"host": b"other"}, True),
        ({b"origin": b"null", b"host": b"h"}, True),
        ({b"origin": b"http://h"}, True),
    ],
)
def test_cross_site_decision_table(headers, expected):
    assert cross_site(headers) is expected


# --- the page's content rules ----------------------


@pytest.mark.security
def test_nothing_is_loaded_from_another_site():
    for name, text in {"html": HTML, "css": CSS, "app.js": APP_JS, "logic.js": LOGIC_JS}.items():
        assert not re.search(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""", text), name
        assert "@import" not in text, name
        assert not re.search(r"url\(\s*[\"']?(?:https?:)?//", text), name
        assert not re.search(r"fetch\(\s*[\"']https?:", text), name
        assert "googleapis" not in text, name
        assert "gstatic" not in text, name
        assert "cdn" not in text.lower(), name


@pytest.mark.security
def test_no_inline_code_so_the_policy_can_forbid_it():
    assert not re.search(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/json\")[^>]*>", HTML)
    assert not re.search(r"\son(?:click|load|submit|change|input|error)\s*=", HTML, re.IGNORECASE)
    assert " style=" not in HTML
    assert "<style" not in HTML
    assert "eval(" not in APP_JS + LOGIC_JS
    assert "innerHTML" not in APP_JS, "text is always set with textContent"


def test_the_page_is_mobile_friendly_and_declares_utf_8():
    assert '<meta charset="utf-8">' in HTML
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in HTML
    assert "@media (max-width: 520px)" in CSS
    assert "font-size: 16px" in CSS, "16 px inputs keep phones from zooming"
    assert "min-height: 44px" in CSS, "touch targets"


def test_every_input_has_a_label():
    ids = re.findall(r'<(?:input|select)[^>]*\bid="([\w-]+)"', HTML)
    for input_id in ids:
        if input_id.startswith("mode-") or input_id == "subscribe" or input_id == "pack":
            assert f'for="{input_id}"' in HTML
            continue
        assert f'for="{input_id}"' in HTML, f"{input_id} has no label"


def test_the_ids_the_script_uses_exist_in_the_page():
    used = set(re.findall(r'\$\("([\w-]+)"\)', APP_JS))
    present = set(re.findall(r'\bid="([\w-]+)"', HTML))
    assert used <= present, sorted(used - present)


# --- the fields match the server ----------------------


def test_the_media_types_offered_are_the_ones_the_server_knows():
    options = re.findall(r'<option value="(\w+)"', HTML)
    assert options == list(METADATA_TYPES)


def test_the_fields_per_type_are_exactly_the_servers_fields():
    from ytp.metadata import AudiobookMetadata, MovieMetadata, MusicMetadata, SeriesMetadata

    models = {
        "movies": MovieMetadata,
        "series": SeriesMetadata,
        "audiobooks": AudiobookMetadata,
        "music": MusicMetadata,
    }
    for media_type, model in models.items():
        block = re.search(
            rf'<div class="type-fields" data-type="{media_type}"[^>]*>(.*?)\n        </div>\n(?=\n|        <div class="type-fields"|      </section>)',
            HTML,
            re.S,
        )
        assert block, media_type
        fields = set(re.findall(r'data-field="(\w+)"', block.group(1)))
        assert fields == set(model.model_fields) - {"type"}, media_type


# --- i18n ----------------------


def translations() -> dict[str, dict[str, str]]:
    match = re.search(
        r'<script type="application/json" id="i18n">\s*(\{.*?\})\s*</script>', HTML, re.S
    )
    assert match
    return json.loads(match.group(1))


def test_german_and_english_have_exactly_the_same_keys():
    t = translations()
    assert set(t["de"]) == set(t["en"])
    assert len(t["de"]) > 70


def test_no_translation_is_empty_and_german_differs_from_english():
    t = translations()
    for lang in ("de", "en"):
        assert all(value.strip() for value in t[lang].values()), lang
    same = [k for k in t["de"] if t["de"][k] == t["en"][k]]
    assert set(same) <= {
        "link.label",
        "mode.youtube",
        "field.title",
        "type.music",
        "field.album",
        "mode.magnet",
        "yt.title",
    }, same


def test_every_key_used_in_the_page_and_the_script_exists():
    t = translations()["en"]
    used = set(re.findall(r'data-i18n(?:-placeholder)?="([\w.]+)"', HTML))
    used |= {k for k in re.findall(r'\bt\("([\w.]+)"', APP_JS) if not k.endswith(".")}
    used |= {
        f"vpn.{s}" for s in ("ok", "down", "blind", "starting", "unknown", "mismatch", "stale")
    }
    used |= {f"vpn.d.{s}" for s in ("down", "mismatch", "blind", "starting", "unknown")}
    used |= {f"status.{s}" for s in ("queued", "downloading", "organizing", "done", "failed")}
    used |= {f"time.{u}" for u in ("now", "minutes", "hours", "days")}
    missing = {k for k in used if k not in t}
    assert not missing, sorted(missing)


def test_every_server_error_and_probe_kind_has_a_text():
    from ytp.api import PROBE_STATUS

    t = translations()
    for lang in ("de", "en"):
        for kind in PROBE_STATUS:
            assert f"kind.{kind}" in t[lang], (lang, kind)
        for error_type in (
            "missing", "name_empty", "name_too_long", "name_path_separator", "name_control_character",
            "name_dots_only", "year_out_of_range", "int_parsing", "greater_than_equal",
            "less_than_equal", "magnet_invalid", "video_url_invalid", "extra_forbidden",
        ):  # fmt: skip
            assert f"err.{error_type}" in t[lang], (lang, error_type)


def test_the_page_names_umlauts_correctly():
    t = translations()["de"]
    assert t["type.audiobooks"] == "Hörbuch"
    assert "Künstler" in t["field.artist"]


# --- the browser helpers, with node ----------------------

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="node is not installed (runs on developer machines)"
)


def run_node(expression: str):
    script = f"const L = require({json.dumps(str(STATIC_DIR / 'logic.js'))}); console.log(JSON.stringify({expression}));"
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("  https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("http://music.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("MAGNET:?xt=urn:btih:abc", "magnet"),
        ("magnet:?xt=urn:btih:abc", "magnet"),
        ("https://vimeo.com/1", None),
        ("https://youtube.com.evil.example/x", None),
        ("hello", None),
        ("", None),
    ],
)
def test_the_link_type_is_detected(text, mode):
    assert run_node(f"L.detectMode({json.dumps(text)})") == mode


@needs_node
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://youtu.be/dQw4w9WgXcQ", True),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", True),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", True),
        ("https://www.youtube.com/watch?v=short", False),
        ("https://www.youtube.com/@channel", False),
        ("magnet:?xt=urn:btih:" + "a" * 40, False),
    ],
)
def test_only_a_video_address_triggers_the_lookup(text, expected):
    assert run_node(f"L.looksLikeVideoUrl({json.dumps(text)})") is expected


@needs_node
def test_payloads_match_what_the_server_expects(client):
    cases = [
        ("movies", {"title": " Big Buck Bunny ", "year": "2008"}),
        ("series", {"title": "Tatort", "season": "3", "episode": "7", "pack": False}),
        ("series", {"title": "Tatort", "season": "3", "episode": "7", "pack": True}),
        ("series", {"title": "Tatort", "season": "3", "episode": "", "pack": False}),
        ("audiobooks", {"author": "Michael Ende", "title": "Momo"}),
        ("music", {"artist": "Kraftwerk", "album": "Autobahn"}),
    ]
    for media_type, values in cases:
        payload = run_node(
            f"L.magnetPayload({json.dumps(MAGNET)}, {json.dumps(media_type)}, {json.dumps(values)})"
        )
        response = client.post("/api/magnet", json=payload)
        assert response.status_code in {201, 503}, (media_type, response.status_code, response.text)
        assert response.status_code != 422
    pack = run_node(f"L.magnetPayload({json.dumps(MAGNET)}, 'series', {json.dumps(cases[2][1])})")
    assert "episode" not in pack["metadata"], "a season pack has no episode"


@needs_node
@pytest.mark.parametrize(
    ("body", "field", "error_type"),
    [
        ({"magnet": "http://x/y", "metadata": {"type": "music", "artist": "a", "album": "b"}}, "link", "magnet_invalid"),
        ({"magnet": MAGNET, "metadata": {"type": "movies", "title": "", "year": 1066}}, "year", "year_out_of_range"),
        ({"magnet": MAGNET, "metadata": {"type": "movies", "title": "", "year": 2000}}, "title", "name_empty"),
        ({"magnet": MAGNET, "metadata": {"type": "series", "title": "t", "season": 0}}, "season", "greater_than_equal"),
        ({"magnet": MAGNET, "metadata": {"type": "audiobooks", "author": "a/b", "title": "t"}}, "author", "name_path_separator"),
        ({"magnet": MAGNET, "metadata": {"type": "music", "artist": "a", "album": "../x"}}, "album", "name_path_separator"),
    ],
)  # fmt: skip
def test_server_422s_are_mapped_to_the_right_form_field(client, body, field, error_type):
    detail = client.post("/api/magnet", json=body).json()["detail"]
    mapped = run_node(f"L.serverErrors({json.dumps(detail)})")
    assert field in mapped
    assert mapped[field]["type"] == error_type


@needs_node
def test_youtube_422s_are_mapped_too(client):
    detail = client.post(
        "/api/youtube/download", json={"url": "https://vimeo.com/1", "channel": "", "title": "a/b"}
    ).json()["detail"]
    mapped = run_node(f"L.serverErrors({json.dumps(detail)})")
    assert set(mapped) == {"link", "channel", "title"}
    assert mapped["link"]["type"] == "video_url_invalid"


@needs_node
def test_the_first_look_in_the_browser_catches_empty_fields_and_a_bad_year():
    year = 2026
    assert run_node(f"L.clientErrors('magnet', 'movies', '', {{title: '', year: ''}}, {year})") == {
        "link": "err.required", "title": "err.required", "year": "err.required",
    }  # fmt: skip
    assert run_node(
        f"L.clientErrors('magnet', 'movies', 'm', {{title: 't', year: '1066'}}, {year})"
    ) == {"year": "err.year_out_of_range"}
    assert (
        run_node(f"L.clientErrors('magnet', 'movies', 'm', {{title: 't', year: '2027'}}, {year})")
        == {}
    )
    assert run_node(
        f"L.clientErrors('magnet', 'series', 'm', {{title: 't', season: 'x'}}, {year})"
    ) == {"season": "err.int_parsing"}
    assert run_node(
        f"L.clientErrors('youtube', 'movies', 'u', {{channel: '', title: 'x'}}, {year})"
    ) == {"channel": "err.required"}


@needs_node
def test_the_text_template_fills_in_values():
    assert run_node("L.format('Job #{id} ({status})', {id: 7, status: 'done'})") == "Job #7 (done)"
    assert run_node("L.format('no {missing} here', {})") == "no {missing} here"


# --- the job list (#26) ----------------------


@needs_node
@pytest.mark.parametrize(
    ("iso", "expected"),
    [
        ("2026-11-01T10:00:00.000Z", {"unit": "now", "n": 0}),
        ("2026-11-01T09:59:30.000Z", {"unit": "now", "n": 0}),
        ("2026-11-01T09:58:00.000Z", {"unit": "minutes", "n": 2}),
        ("2026-11-01T07:00:00.000Z", {"unit": "hours", "n": 3}),
        ("2026-10-29T10:00:00.000Z", {"unit": "days", "n": 3}),
        ("2026-11-01T10:05:00.000Z", {"unit": "now", "n": 0}),  # a clock a little ahead
        ("not a time", None),
    ],
)
def test_the_age_of_a_timestamp(iso, expected):
    now = "Date.parse('2026-11-01T10:00:00.000Z')"
    assert run_node(f"L.age({json.dumps(iso)}, {now})") == expected


@needs_node
def test_job_kinds_map_to_known_translations():
    t = translations()["en"]
    for kind in ("movies", "series", "audiobooks", "music", "youtube"):
        assert run_node(f"L.kindKey({json.dumps(kind)})") in t
    assert run_node("L.kindKey('nonsense')") is None


def test_the_page_has_the_job_list_and_the_script_builds_rows_without_innerhtml():
    for element_id in ("jobs-list", "jobs-banner", "jobs-error", "jobs-empty", "jobs-filter"):
        assert f'id="{element_id}"' in HTML
    assert "innerHTML" not in APP_JS
    assert "/api/jobs" in APP_JS
    assert "visibilityState" in APP_JS, "polling pauses while the tab is hidden"


# --- the VPN bar (#28) ----------------------


@needs_node
@pytest.mark.parametrize(
    ("data", "view", "alert"),
    [
        ({"state": "ok"}, "ok", False),
        ({"state": "ok", "egress": "ok", "egress_country": "Switzerland"}, "ok", False),
        ({"state": "down", "paused": True}, "down", True),
        ({"state": "ok", "egress": "mismatch"}, "mismatch", True),
        ({"state": "unknown", "egress": "mismatch"}, "mismatch", True),
        ({"state": "blind"}, "blind", True),
        ({"state": "starting"}, "starting", False),
        ({"state": "unknown"}, "unknown", False),
        ({"state": "something new"}, "unknown", False),
        ({}, "unknown", False),
        (None, "unknown", False),
        ("ok", "unknown", False),
    ],
)
def test_the_vpn_view_is_never_ok_unless_the_watchdog_says_so(data, view, alert):
    result = run_node(f"L.vpnView({json.dumps(data)})")
    assert result["view"] == view
    assert result["alert"] is alert


@needs_node
def test_the_vpn_view_passes_on_only_safe_details():
    result = run_node(
        "L.vpnView({state: 'ok', egress: 'ok', egress_country: 'Switzerland', paused: true,"
        " since: '2026-11-01T10:00:00Z', ip: '1.2.3.4'})"
    )
    assert result == {
        "view": "ok", "alert": False, "paused": True, "since": "2026-11-01T10:00:00Z",
        "country": "Switzerland",
    }  # fmt: skip
    assert (
        run_node("L.vpnView({state: 'down', egress: 'ok', egress_country: 'X'})")["country"] is None
    )


def test_the_vpn_bar_is_part_of_the_page_before_the_content_and_marks_alerts():
    assert HTML.index('id="vpn"') < HTML.index('class="wrap"')
    assert 'id="vpn-detail"' in HTML
    assert "position: sticky" in CSS
    for view in ("down", "mismatch", "blind"):
        assert f".vpn-{view}" in CSS
    assert "favicon-alert.svg" in APP_JS
    assert "visibilityState" in APP_JS
