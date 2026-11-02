"""The page's real scripts against the real index.html in a small DOM (needs node).

`tests/helpers/page_harness.js` loads index.html, runs dashboard.js, auth.js and app.js as the
browser does and reports script errors, the requests made and what the page shows. This is
what found that `renderHowtoSources` was still called after its function had been removed:
`init()` threw before it attached a single handler, the warning stayed on the page, and the
owner's form was submitted natively (issue #43). The unit tests of the helpers and of
dashboard.js never ran `app.js`.
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
ACCOUNT = {"id": 1, "username": "boss", "role": "admin", "disabled": False, "last_login": None}
ANSWERS = {
    "/api/auth/me": {"account": ACCOUNT},
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


# --- the lookup of a YouTube address: why it failed, and the fields stay open (issue: the owner saw
# nothing when YouTube answered "not a bot") -----------------------------------------------------


def probe_steps(*extra):
    return [
        {"set": "#link", "value": VIDEO},
        {"fire": "#link", "type": "input"},
        {"tick": 1},
        {"read": "#probe-status"},
        *extra,
    ]


def posts(out, path):
    return [f for f in out["fetched"] if f == ["POST", path]]


BOT = {"__status": 502, "body": {"detail": "YouTube wants proof", "kind": "bot_check"}}


@needs_node
def test_a_bot_check_is_explained_next_to_the_address_in_german_and_the_fields_stay_editable():
    out = page(
        "/",
        language="de-DE",
        answers={"POST /api/youtube/probe": BOT},
        steps=probe_steps({"read": "#f-channel"}, {"read": "#f-yt-title"}),
    )
    assert out["errors"] == [], out["errors"]
    status = out["reads"]["#probe-status"]
    assert status["text"] == (
        "YouTube verlangt einen Nachweis, dass du kein Bot bist; das passiert oft über VPN. "
        "Kanal und Titel kannst du selbst eintragen."
    )
    assert "probe-failed" in status["cls"]
    for field in ("#f-channel", "#f-yt-title"):
        assert out["reads"][field]["disabled"] is False
        assert out["reads"][field]["value"] == ""


@needs_node
def test_after_a_failed_lookup_the_job_can_be_submitted_with_channel_and_title_typed_by_hand():
    out = page(
        "/",
        answers={"POST /api/youtube/probe": BOT},
        steps=probe_steps(
            {"set": "#f-channel", "value": "Blender Foundation"},
            {"set": "#f-yt-title", "value": "Big Buck Bunny"},
            {"submit": "#form"},
            {"read": "#result"},
        ),
    )
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/youtube/download"] in out["fetched"]
    assert "Job #1" in out["reads"]["#result"]["text"]


@needs_node
@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (504, "timeout", "YouTube did not answer"),
        (502, "network", "YouTube could not be reached"),
        (404, "unavailable", "deleted or no longer available"),
        (502, "something_new", "The lookup at YouTube did not work."),
    ],
)
def test_every_failure_of_the_lookup_says_why_and_that_it_can_be_typed_in(status, kind, expected):
    out = page(
        "/",
        answers={
            "POST /api/youtube/probe": {"__status": status, "body": {"detail": "x", "kind": kind}}
        },
        steps=probe_steps(),
    )
    text = out["reads"]["#probe-status"]["text"]
    assert expected.lower() in text.lower() or (
        kind == "unavailable" and "deleted" in text.lower()
    ), text
    assert text.endswith("You can enter channel and title yourself.")
    assert "probe-failed" in out["reads"]["#probe-status"]["cls"]


@needs_node
@pytest.mark.parametrize("event", ["input", "change", "blur", "paste"])
def test_every_way_an_address_arrives_starts_the_lookup(event):
    out = page(
        "/",
        steps=[
            {"set": "#link", "value": VIDEO},
            {"fire": "#link", "type": event},
            {"tick": 1},  # a paste is handled one timer later ...
            {"tick": 1},  # ... and then the lookup itself waits half a second
            {"read": "#f-yt-title"},
        ],
    )
    assert len(posts(out, "/api/youtube/probe")) == 1
    assert out["reads"]["#f-yt-title"]["value"] == "Big Buck Bunny"


@needs_node
def test_one_paste_that_fires_several_events_asks_youtube_once():
    steps = [{"set": "#link", "value": VIDEO}]
    for event in ("paste", "input", "change", "blur"):
        steps += [{"fire": "#link", "type": event}, {"tick": 1}]
    steps += [{"tick": 1}]
    out = page("/", steps=steps)
    assert len(posts(out, "/api/youtube/probe")) == 1


@needs_node
def test_a_failed_lookup_is_tried_again_on_the_next_change_but_a_good_one_is_not():
    again = [{"fire": "#link", "type": "blur"}, {"tick": 1}]
    failed = page("/", answers={"POST /api/youtube/probe": BOT}, steps=probe_steps(*again))
    assert len(posts(failed, "/api/youtube/probe")) == 2
    good = page("/", steps=probe_steps(*again))
    assert len(posts(good, "/api/youtube/probe")) == 1


@needs_node
def test_a_different_address_is_looked_up_again():
    other = "https://youtu.be/dQw4w9WgXcQ"
    out = page(
        "/",
        steps=probe_steps(
            {"set": "#link", "value": other}, {"fire": "#link", "type": "input"}, {"tick": 1}
        ),
    )
    assert len(posts(out, "/api/youtube/probe")) == 2


def test_the_lookup_answer_sits_in_the_address_row_before_the_mode_choice():
    from ytp.web import STATIC_DIR as static

    html = (static / "index.html").read_text(encoding="utf-8")
    assert (
        html.index('id="link-error"')
        < html.index('id="probe-status"')
        < html.index('id="mode-label"')
    )
    assert html.count('id="probe-status"') == 1


# --- sign-in, the account menu and the accounts page (#44) ------------------------------------

USER_ACCOUNT = {**ACCOUNT, "id": 2, "username": "alice", "role": "user"}
SIGNED_OUT = {"/api/auth/me": {"__status": 401, "body": {"detail": "login required"}}}
NO_ADMIN_YET = {
    "/api/auth/me": {"__status": 401, "body": {"detail": "login required", "setup": True}}
}
ACCOUNTS = {
    "/api/accounts": {
        "accounts": [
            ACCOUNT,
            {**USER_ACCOUNT, "last_login": "2026-11-01T10:00:00.000Z"},
            {**USER_ACCOUNT, "id": 3, "username": "bob", "disabled": True},
        ]
    }
}


def requests_made(out) -> list[str]:
    return [url for _, url in out["fetched"]]


@needs_node
def test_signed_in_the_page_starts_and_knows_the_role():
    out = page("/")
    assert out["errors"] == [], out["errors"]
    assert (out["auth"], out["role"]) == ("in", "admin")
    assert "/api/auth/me" in requests_made(out)
    assert "/api/jobs?limit=50" in requests_made(out)
    assert ["GET", "/api/alerts?limit=50"] in out["fetched"]


@needs_node
def test_a_user_gets_the_page_without_the_admin_requests():
    out = page("/", answers={"/api/auth/me": {"account": USER_ACCOUNT}})
    assert out["errors"] == [], out["errors"]
    assert (out["auth"], out["role"]) == ("in", "user")
    assert "/api/jobs?limit=50" in requests_made(out)
    assert not [u for u in requests_made(out) if u.startswith("/api/alerts")]
    assert not [u for u in requests_made(out) if u.startswith("/api/accounts")]


@needs_node
def test_a_user_on_the_dashboard_does_not_break_on_the_reduced_answer():
    reduced = {"active": [], "recent": [], "disk": None, "deploy": None, "vpn": {"current": {}}}
    out = page(
        "/dashboard",
        answers={"/api/auth/me": {"account": USER_ACCOUNT}, "/api/dashboard": reduced},
    )
    assert out["errors"] == [], out["errors"]


@needs_node
def test_nobody_signed_in_means_the_form_and_nothing_else():
    out = page("/", answers=SIGNED_OUT)
    assert out["errors"] == [], out["errors"]
    assert (out["auth"], out["role"]) == ("out", None)
    # nothing but the question "who am I" and the version check leaves the page
    assert set(requests_made(out)) <= {"/api/auth/me", "/api/version"}


@needs_node
def test_without_any_account_the_page_says_how_to_make_the_admin_in_german():
    out = page(
        "/",
        language="de-DE",
        answers=NO_ADMIN_YET,
        steps=[{"read": "#login-setup"}, {"read": "#login-form"}],
    )
    assert out["errors"] == [], out["errors"]
    setup = out["reads"]["#login-setup"]
    assert setup["hidden"] is False
    assert "make admin-password" in setup["text"]
    assert "docker compose up -d" in setup["text"]
    assert out["reads"]["#login-form"]["hidden"] is True, "no dead login form"


@needs_node
def test_with_accounts_the_setup_notice_stays_away():
    out = page("/", answers=SIGNED_OUT, steps=[{"read": "#login-setup"}, {"read": "#login-form"}])
    assert out["reads"]["#login-setup"]["hidden"] is True
    assert out["reads"]["#login-form"]["hidden"] is False


def sign_in(user="boss", password="a long test password"):  # a test value
    return [
        {"set": "#login-user", "value": user},
        {"set": "#login-password", "value": password},
        {"submit": "#login-form"},
        {"read": "#login-error"},
    ]


@needs_node
def test_a_good_login_reloads_the_page_without_a_native_submit():
    out = page(
        "/",
        answers={**SIGNED_OUT, "POST /api/auth/login": {"account": ACCOUNT}},
        steps=sign_in(),
    )
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/auth/login"] in out["fetched"]
    assert out["prevented"]["#login-form"] is True
    assert out["reloads"] == 1
    assert "a long test password" not in json.dumps(out["fetched"]), "never in an address"


@needs_node
@pytest.mark.parametrize(
    ("reply", "text"),
    [
        (
            {"__status": 401, "body": {"detail": "wrong name or password"}},
            "Wrong name or password.",
        ),
        (
            {"__status": 429, "body": {"retry_after": 45}},
            "Too many attempts. Please wait 45 seconds.",
        ),
        ({"__status": 500, "body": {}}, "Signing in did not work."),
    ],
)
def test_a_failed_login_says_why_and_stays(reply, text):
    out = page("/", answers={**SIGNED_OUT, "POST /api/auth/login": reply}, steps=sign_in())
    assert out["errors"] == [], out["errors"]
    assert out["reads"]["#login-error"]["text"] == text
    assert out["reloads"] == 0
    assert out["auth"] == "out"


@needs_node
def test_an_empty_login_is_not_sent():
    out = page("/", answers=SIGNED_OUT, steps=sign_in("", ""))
    assert ["POST", "/api/auth/login"] not in out["fetched"]
    assert out["reads"]["#login-error"]["text"] == "Please enter a name and a password."


@needs_node
def test_the_german_login_texts_exist():
    out = page(
        "/",
        language="de-DE",
        answers={**SIGNED_OUT, "POST /api/auth/login": {"__status": 401, "body": {}}},
        steps=sign_in(),
    )
    assert out["reads"]["#login-error"]["text"] == "Name oder Passwort falsch."


@needs_node
def test_a_session_that_ends_while_the_page_is_open_shows_the_login_again():
    out = page(
        "/",
        answers={"/api/jobs": {"__status": 401, "body": {"detail": "login required"}}},
        steps=[{"tick": 1}, {"read": "#login-note"}],
    )
    assert out["errors"] == [], out["errors"]
    assert out["auth"] == "out"
    assert out["reads"]["#login-note"]["text"] == "Your session has ended. Please sign in again."


@needs_node
def test_logout_ends_the_session_on_the_server_and_reloads():
    out = page("/", steps=[{"click": "#account-logout"}])
    assert ["POST", "/api/auth/logout"] in out["fetched"]
    assert out["reloads"] == 1


@needs_node
def test_the_account_button_shows_the_name():
    out = page("/", steps=[{"read": "#account-open"}, {"read": "#account-who"}])
    assert out["reads"]["#account-open"]["text"] == "boss"
    assert out["reads"]["#account-who"]["text"] == "Signed in as boss (Administrator)"


def change_password(current, new, repeat):
    return [
        {"set": "#account-current", "value": current},
        {"set": "#account-new", "value": new},
        {"set": "#account-repeat", "value": repeat},
        {"submit": "#account-form"},
        {"read": "#account-result"},
    ]


@needs_node
@pytest.mark.parametrize(
    ("current", "new", "repeat", "text"),
    [
        ("old password!!", "new password one", "new password two", "The two new passwords differ."),
        ("old password!!", "short", "short", "The new password needs at least 12 characters."),
        ("", "new password one", "new password one", "Please enter a name and a password."),
    ],
)
def test_the_password_form_checks_before_it_sends(current, new, repeat, text):
    out = page("/", steps=change_password(current, new, repeat))
    assert ["POST", "/api/auth/password"] not in out["fetched"]
    assert out["reads"]["#account-result"]["text"] == text


@needs_node
@pytest.mark.parametrize(
    ("reply", "text"),
    [
        ({"ok": True}, "The password was changed. You are signed out on other devices."),
        ({"__status": 403, "body": {"detail": "x"}}, "The current password is wrong."),
        ({"__status": 500, "body": {}}, "Changing the password did not work."),
    ],
)
def test_the_password_change_reports_the_answer(reply, text):
    out = page(
        "/",
        answers={"POST /api/auth/password": reply},
        steps=change_password("old password!!", "new password one", "new password one"),
    )
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/auth/password"] in out["fetched"]
    assert out["reads"]["#account-result"]["text"] == text


@needs_node
def test_the_accounts_page_lists_the_accounts_for_an_admin():
    out = page("/accounts", answers=ACCOUNTS, steps=[{"read": "#accounts-list"}])
    assert out["errors"] == [], out["errors"]
    assert ["GET", "/api/accounts"] in out["fetched"]
    assert out["reads"]["#accounts-list"]["children"] == 3


@needs_node
def test_a_user_is_sent_away_from_the_accounts_page_without_asking_for_it():
    out = page("/accounts", answers={"/api/auth/me": {"account": USER_ACCOUNT}})
    assert out["href"] == "/"
    assert ["GET", "/api/accounts"] not in out["fetched"]


@needs_node
def test_creating_an_account_posts_it_and_reloads_the_list():
    out = page(
        "/accounts",
        answers={
            **ACCOUNTS,
            "POST /api/accounts": {
                "__status": 201,
                "body": {"account": {**USER_ACCOUNT, "username": "carol"}},
            },
        },
        steps=[
            {"set": "#accounts-name", "value": "carol"},
            {"set": "#accounts-password", "value": "carol has a long password"},
            {"set": "#accounts-role", "value": "user"},
            {"submit": "#accounts-form"},
            {"read": "#accounts-result"},
        ],
    )
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/accounts"] in out["fetched"]
    assert out["fetched"].count(["GET", "/api/accounts"]) == 2
    assert out["reads"]["#accounts-result"]["text"] == 'Account "carol" created.'
    assert "carol has a long password" not in json.dumps(out["fetched"])


@needs_node
@pytest.mark.parametrize(
    ("reply", "text"),
    [
        (
            {"__status": 409, "body": {"kind": "exists", "detail": "x"}},
            "That name is already taken.",
        ),
        (
            {"__status": 422, "body": {"kind": "invalid", "detail": "the name needs 3"}},
            "The name needs 3 to 64 letters, digits, dots, underscores or dashes.",
        ),
    ],
)
def test_account_creation_errors_are_explained(reply, text):
    out = page(
        "/accounts",
        answers={**ACCOUNTS, "POST /api/accounts": reply},
        steps=[
            {"set": "#accounts-name", "value": "boss"},
            {"set": "#accounts-password", "value": "a long test password"},
            {"submit": "#accounts-form"},
            {"read": "#accounts-result"},
        ],
    )
    assert out["reads"]["#accounts-result"]["text"] == text


@needs_node
def test_the_followers_of_a_subscription_show_for_the_admin_and_only_a_count_for_a_user():
    channel = {
        "id": 1,
        "channel_id": "UCSMOQeBJ2RAnuFungnQOxLg",
        "channel_name": "Blender",
        "enabled": True,
        "backfill": 0,
        "created_at": "2026-10-01T00:00:00.000Z",
        "last_checked": None,
        "last_error": None,
        "target": "video",
        "audio_only": False,
        "following": True,
        "follower_count": 2,
    }
    admin = page(
        "/",
        answers={
            "/api/subscriptions": {"subscriptions": [{**channel, "followers": ["alice", "boss"]}]}
        },
        steps=[{"read": "#subs-list"}],
    )
    assert admin["errors"] == [], admin["errors"]
    assert "Followed by: alice, boss" in admin["reads"]["#subs-list"]["text"]
    user = page(
        "/",
        answers={
            "/api/auth/me": {"account": USER_ACCOUNT},
            "/api/subscriptions": {"subscriptions": [channel]},
        },
        steps=[{"read": "#subs-list"}],
    )
    assert user["errors"] == [], user["errors"]
    assert user["reads"]["#subs-list"]["children"] == 1
    assert "1 other person(s) follow this too" in user["reads"]["#subs-list"]["text"]
    assert "alice" not in user["reads"]["#subs-list"]["text"]


# --- the input page, the subscriptions view and the downloads line ------------------------------

JOBS = {
    "jobs": [
        {"id": 1, "status": "downloading", "progress": 30, "label": "A"},
        {"id": 2, "status": "downloading", "progress": 50, "label": "B"},
        {"id": 3, "status": "queued", "progress": None, "label": "C"},
    ],
    "counts": {"downloading": 2, "queued": 1, "failed": 1},
}


@needs_node
def test_the_input_page_shows_the_downloads_in_one_line_with_the_progress():
    out = page(
        "/",
        answers={"/api/jobs": JOBS},
        steps=[{"read": "#jobs-mini-text"}, {"read": "#jobs-mini-bar"}, {"read": "#jobs-mini"}],
    )
    assert out["errors"] == [], out["errors"]
    assert out["reads"]["#jobs-mini-text"]["text"] == "running: 2 · waiting: 1 · failed: 1"
    assert out["reads"]["#jobs-mini-bar"]["hidden"] is False
    assert out["reads"]["#jobs-mini-bar"]["value"] == "40"
    assert "jobs-mini-has-failed" in out["reads"]["#jobs-mini"]["cls"]


@needs_node
def test_with_nothing_running_the_line_says_so_and_has_no_bar():
    out = page("/", steps=[{"read": "#jobs-mini-text"}, {"read": "#jobs-mini-bar"}])
    assert out["reads"]["#jobs-mini-text"]["text"] == "nothing running right now"
    assert out["reads"]["#jobs-mini-bar"]["hidden"] is True


@needs_node
def test_the_subscriptions_view_shows_the_list_and_not_the_form():
    out = page(
        "/",
        "#subscriptions",
        steps=[
            {"read": "#form"},
            {"read": "#subscriptions"},
            {"read": "#jobs-mini"},
            {"read": "#nav-subs"},
        ],
    )
    assert out["errors"] == [], out["errors"]
    assert out["reads"]["#form"]["hidden"] is True
    assert out["reads"]["#jobs-mini"]["hidden"] is True
    assert out["reads"]["#subscriptions"]["hidden"] is False


@needs_node
def test_the_input_page_shows_the_form_and_not_the_subscriptions():
    out = page("/", steps=[{"read": "#form"}, {"read": "#subscriptions"}, {"read": "#jobs-mini"}])
    assert out["reads"]["#form"]["hidden"] is False
    assert out["reads"]["#jobs-mini"]["hidden"] is False
    assert out["reads"]["#subscriptions"]["hidden"] is True


SUBSCRIBED = {
    "POST /api/subscriptions": {
        "__status": 201,
        "body": {"channel_name": "Blender", "duplicate": False, "other_followers": 0},
    }
}


def subscribe_steps(backfill):
    return probe_steps(
        {"check": "#subscribe", "value": True},
        {"fire": "#subscribe", "type": "change"},
        {"read": "#yt-backfill-row"},
        {"set": "#yt-backfill", "value": backfill},
        {"submit": "#form"},
        {"read": "#result"},
        {"read": "#yt-backfill-error"},
    )


@needs_node
def test_ticking_subscribe_offers_the_older_videos_and_subscribes_after_the_download():
    out = page("/", answers=SUBSCRIBED, steps=subscribe_steps("5"))
    assert out["errors"] == [], out["errors"]
    assert out["reads"]["#yt-backfill-row"]["hidden"] is False
    assert ["POST", "/api/youtube/download"] in out["fetched"]
    assert ["POST", "/api/subscriptions"] in out["fetched"]
    assert 'Subscribed to the channel "Blender".' in out["reads"]["#result"]["text"]


@needs_node
def test_too_many_older_videos_are_refused_before_anything_is_sent():
    out = page("/", answers=SUBSCRIBED, steps=subscribe_steps("16"))
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/youtube/download"] not in out["fetched"]
    assert out["reads"]["#yt-backfill-error"]["text"] == "A number from 0 to 15."


@needs_node
def test_the_page_scrolls_to_its_anchor_once_the_sign_in_is_known():
    """The browser scrolls to #subscriptions on load, when that part is still hidden (the
    sign-in is not known yet): the script has to do it afterwards (found in the real browser)."""
    out = page("/", "#subscriptions")
    assert out["errors"] == [], out["errors"]
    assert "subscriptions" in out["scrolled"]


@needs_node
def test_a_visitor_is_not_scrolled_anywhere():
    out = page("/", "#subscriptions", answers=SIGNED_OUT)
    assert out["scrolled"] == []


@needs_node
def test_an_odd_hash_scrolls_nowhere_and_breaks_nothing():
    for hash_ in ("#", "#nope", "#a b", "#" + "x" * 50):
        out = page("/", hash_)
        assert out["errors"] == [], out["errors"]
        assert out["scrolled"] == []
