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

from tests.helpers.node import NODE, needs_node
from ytp.web import STATIC_DIR

pytestmark = pytest.mark.unit

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
def test_the_empty_sign_in_note_stays_hidden_over_the_name_field():
    """#49: the banner is red by design; empty, it looked like a broken field above the name."""
    out = page("/", answers=SIGNED_OUT, steps=[{"read": "#login-note"}])
    assert out["errors"] == [], out["errors"]
    note = out["reads"]["#login-note"]
    assert note["text"] == ""
    assert note["hidden"] is True


@needs_node
def test_a_session_that_ends_while_the_page_is_open_shows_the_login_again():
    out = page(
        "/",
        answers={"/api/jobs": {"__status": 401, "body": {"detail": "login required"}}},
        steps=[{"tick": 1}, {"read": "#login-note"}],
    )
    assert out["errors"] == [], out["errors"]
    assert out["auth"] == "out"
    note = out["reads"]["#login-note"]
    assert note["text"] == "Your session has ended. Please sign in again."
    assert note["hidden"] is False, "a note with a reason is shown"


@needs_node
def test_logout_ends_the_session_on_the_server_and_reloads():
    out = page("/", steps=[{"click": "#account-logout"}])
    assert ["POST", "/api/auth/logout"] in out["fetched"]
    assert out["reloads"] == 1


@needs_node
def test_the_account_button_shows_the_initial_and_the_dialog_the_name():
    out = page("/", steps=[{"read": "#account-open"}, {"read": "#account-who"}])
    assert out["reads"]["#account-open"]["text"] == "B"
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
        steps=[
            {"read": "#subs-list"},
            {"read": "#subs-youtube-list"},
            {"read": "#subs-podcast-group"},
        ],
    )
    assert user["errors"] == [], user["errors"]
    assert user["reads"]["#subs-youtube-list"]["children"] == 1
    assert user["reads"]["#subs-podcast-group"]["hidden"] is True
    assert "1 other person(s) follow this too" in user["reads"]["#subs-list"]["text"]
    assert "alice" not in user["reads"]["#subs-list"]["text"]


@needs_node
def test_subscriptions_are_grouped_into_youtube_channels_and_podcasts():
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
        "follower_count": 1,
    }
    podcast = {
        "id": 7,
        "kind": "podcast",
        "title": "Serial",
        "last_checked": None,
        "last_error": None,
        "target": "podcast",
        "audio_only": True,
        "following": True,
        "follower_count": 1,
    }
    out = page(
        "/",
        answers={
            "/api/subscriptions": {"subscriptions": [channel]},
            "/api/podcasts": {"podcasts": [podcast]},
        },
        steps=[
            {"read": "#subs-youtube-group"},
            {"read": "#subs-podcast-group"},
            {"read": "#subs-youtube-title"},
            {"read": "#subs-podcast-title"},
            {"read": "#subs-youtube-list"},
            {"read": "#subs-podcast-list"},
        ],
    )
    assert out["errors"] == [], out["errors"]
    assert out["reads"]["#subs-youtube-group"]["hidden"] is False
    assert out["reads"]["#subs-podcast-group"]["hidden"] is False
    assert out["reads"]["#subs-youtube-title"]["text"] == "YouTube channels"
    assert out["reads"]["#subs-podcast-title"]["text"] == "Podcasts"
    assert out["reads"]["#subs-youtube-list"]["children"] == 1
    assert out["reads"]["#subs-podcast-list"]["children"] == 1
    assert "Blender" in out["reads"]["#subs-youtube-list"]["text"]
    assert "Serial" in out["reads"]["#subs-podcast-list"]["text"]


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


# --- the whole back catalogue of a podcast (#45) -----------------------------------------------

FEED_URL = "https://podcast.example/feed.xml"
CATALOGUE = {
    "episodes": 312,
    "to_download": 312,
    "skipped": 0,
    "size": 18 * 1024**3,
    "sized": 312,
    "space_ok": True,
}
FEED_ANSWER = {
    "feed_url": FEED_URL,
    "title": "Test Cast",
    "folder": "Test Cast",
    "image": False,
    "subscribed": False,
    "subscription_id": None,
    "episodes": [
        {"key": "k" * 40, "title": "One", "published": "2026-10-01", "size": 1, "media_type": ""}
    ],
    "catalogue": CATALOGUE,
}
STARTED = {
    "started": True,
    "joined": False,
    "backlog": {"id": 7, "folder": "Test Cast", "state": "running", "total": 312, "done": 0},
}


def podcast_feed_steps(*more):
    """Paste a feed address and let the lookup answer."""
    return [
        {"set": "#link", "value": FEED_URL},
        {"fire": "#link", "type": "input"},
        {"tick": 1},
        {"read": "#pc-all-note"},
        *more,
    ]


def podcast_page(catalogue=None, steps=(), answers=None):
    feed = {**FEED_ANSWER, "catalogue": {**CATALOGUE, **(catalogue or {})}}
    return page(
        "/",
        steps=podcast_feed_steps(*steps),
        answers={"POST /api/podcasts/probe": feed, **(answers or {})},
    )


@needs_node
def test_the_podcast_form_says_how_many_episodes_and_how_much_space_all_would_take():
    out = podcast_page()
    assert out["errors"] == [], out["errors"]
    assert (
        out["reads"]["#pc-all-note"]["text"]
        == "In the feed: 312 episodes to download, about 18.0 GB."
    )


@needs_node
def test_it_also_says_how_many_are_skipped_and_what_it_does_not_know():
    out = podcast_page({"to_download": 300, "skipped": 12, "size": None, "sized": 0})
    assert out["reads"]["#pc-all-note"]["text"] == (
        "In the feed: 300 episodes to download, size unknown; 12 already there or in the system "
        "and skipped."
    )


@needs_node
def test_when_everything_is_there_it_says_so():
    out = podcast_page({"to_download": 0, "skipped": 312})
    assert out["reads"]["#pc-all-note"]["text"] == (
        "Everything in this feed is already there or in the system."
    )


@needs_node
def test_the_german_note_is_german():
    out = page(
        "/",
        language="de-DE",
        steps=podcast_feed_steps(),
        answers={"POST /api/podcasts/probe": FEED_ANSWER},
    )
    assert out["reads"]["#pc-all-note"]["text"].startswith("Im Feed: 312 Folgen zum Laden")


def submit_all(*extra):
    return [
        {"check": "#pc-all", "value": True},
        {"submit": "#form"},
        {"read": "#pc-all-confirm"},
        {"read": "#result"},
        *extra,
    ]


@needs_node
def test_all_episodes_asks_first_and_only_the_second_press_starts_it():
    out = podcast_page(
        steps=submit_all({"submit": "#form"}, {"read": "#result"}),
        answers={"POST /api/podcasts/backlog": {"__status": 201, "body": STARTED}},
    )
    assert out["errors"] == [], out["errors"]
    confirm = out["reads"]["#pc-all-confirm"]
    assert confirm["hidden"] is False
    assert "312 episodes, about 18.0 GB, newest first" in confirm["text"]
    assert out["fetched"].count(["POST", "/api/podcasts/backlog"]) == 1, "only after the 2nd press"
    assert "Back catalogue started: 312 episodes" in out["reads"]["#result"]["text"]


@needs_node
def test_ticking_the_box_again_takes_the_question_back():
    out = podcast_page(
        steps=[
            {"check": "#pc-all", "value": True},
            {"submit": "#form"},
            {"fire": "#pc-all", "type": "change"},
            {"read": "#pc-all-confirm"},
            {"submit": "#form"},
        ],
    )
    assert out["reads"]["#pc-all-confirm"]["hidden"] is True
    assert ["POST", "/api/podcasts/backlog"] not in out["fetched"], "asked again, not started"


@needs_node
def test_with_too_little_room_it_says_so_and_never_starts():
    out = podcast_page(
        {"space_ok": False},
        steps=submit_all({"submit": "#form"}),
    )
    text = out["reads"]["#pc-all-confirm"]["text"]
    assert "not enough free space" in text
    assert ["POST", "/api/podcasts/backlog"] not in out["fetched"]


@needs_node
def test_nothing_new_in_the_feed_goes_straight_to_the_server_which_says_nothing_to_do():
    out = podcast_page(
        {"to_download": 0, "skipped": 312},
        steps=submit_all(),
        answers={"POST /api/podcasts/backlog": {"started": False, "total": 0, "skipped": 312}},
    )
    assert out["reads"]["#pc-all-confirm"]["hidden"] is True
    assert "Nothing to download" in out["reads"]["#result"]["text"]


@needs_node
def test_a_refusal_for_lack_of_space_is_explained():
    refusal = {"__status": 409, "body": {"detail": "x", "kind": "not_enough_space"}}
    out = page(
        "/",
        steps=podcast_feed_steps(*submit_all({"submit": "#form"}, {"read": "#result"})),
        answers={
            "POST /api/podcasts/probe": {**FEED_ANSWER, "catalogue": None},
            "POST /api/podcasts/backlog": refusal,
        },
    )
    text = out["reads"]["#result"]["text"]
    assert "could not be started" in text
    assert "not enough free space" in text


@needs_node
def test_joining_a_run_that_goes_is_said():
    joined = {**STARTED, "started": False, "joined": True}
    out = podcast_page(
        steps=submit_all({"submit": "#form"}, {"read": "#result"}),
        answers={"POST /api/podcasts/backlog": joined},
    )
    assert "already running; you are part of it" in out["reads"]["#result"]["text"]


@needs_node
def test_all_episodes_works_together_with_subscribing_and_without_one():
    out = podcast_page(
        steps=[
            {"check": "#pc-subscribe", "value": True},
            {"check": "#pc-all", "value": True},
            {"submit": "#form"},
            {"submit": "#form"},
            {"read": "#result"},
        ],
        answers={
            "POST /api/podcasts": {
                "__status": 201,
                "body": {"title": "Test Cast", "duplicate": False},
            },
            "POST /api/podcasts/backlog": {"__status": 201, "body": STARTED},
        },
    )
    assert ["POST", "/api/podcasts"] in out["fetched"]
    assert ["POST", "/api/podcasts/backlog"] in out["fetched"]


@needs_node
def test_all_episodes_alone_is_enough_to_send_the_form():
    out = podcast_page(
        steps=[
            {"check": "#pc-all", "value": True},
            {"submit": "#form"},
            {"read": "#pc-episode-error"},
        ]
    )
    assert out["reads"]["#pc-episode-error"]["text"] == ""


@needs_node
def test_without_any_choice_the_form_still_asks_for_one():
    out = podcast_page(steps=[{"submit": "#form"}, {"read": "#pc-episode-error"}])
    assert (
        out["reads"]["#pc-episode-error"]["text"]
        == "Please choose an episode or subscribe to the podcast."
    )


RUN = {
    "id": 7, "folder": "Test Cast", "state": "running", "total": 312, "done": 40, "failed": 0,
    "waiting": 270, "skipped": 0, "size": None, "paused_reason": None, "created_at": "",
}  # fmt: skip


def dashboard_with(runs, *steps):
    answers = {
        "/api/dashboard": {**ANSWERS["/api/dashboard"], "backlogs": runs},
        "DELETE /api/podcasts/backlog/7": {"ok": True},
    }
    return page("/dashboard", answers=answers, steps=[*steps])


@needs_node
def test_the_dashboard_shows_the_progress_of_a_run():
    out = dashboard_with([RUN], {"read": "#dash-backlog-list"}, {"read": "#dash-backlog-box"})
    assert out["errors"] == [], out["errors"]
    assert out["reads"]["#dash-backlog-box"]["hidden"] is False
    text = out["reads"]["#dash-backlog-list"]["text"]
    assert "Back catalogue: 40 of 312" in text
    assert "270 not queued yet" in text
    assert "running" in text


@needs_node
def test_without_runs_the_section_stays_away():
    out = dashboard_with([], {"read": "#dash-backlog-box"})
    assert out["reads"]["#dash-backlog-box"]["hidden"] is True


@needs_node
def test_a_run_with_failures_a_pause_and_a_finish_say_so():
    paused = {**RUN, "state": "paused", "failed": 2, "skipped": 5}
    out = dashboard_with([paused], {"read": "#dash-backlog-list"})
    text = out["reads"]["#dash-backlog-list"]["text"]
    assert "paused: not enough free space, it continues by itself" in text
    assert "2 failed" in text
    assert "5 already there" in text
    done = {**RUN, "state": "done", "done": 312, "waiting": 0}
    out = dashboard_with([done], {"read": "#dash-backlog-list"})
    assert "finished" in out["reads"]["#dash-backlog-list"]["text"]


@needs_node
def test_a_run_can_be_stopped_from_the_dashboard():
    out = dashboard_with(
        [RUN], {"click": "#dash-backlog-list button"}, {"read": "#dash-backlog-list"}
    )
    assert out["errors"] == [], out["errors"]
    assert ["DELETE", "/api/podcasts/backlog/7"] in out["fetched"]


@needs_node
def test_a_finished_run_has_no_stop_button():
    done = {**RUN, "state": "done"}
    out = dashboard_with([done], {"read": "#dash-backlog-list"})
    assert ["DELETE", "/api/podcasts/backlog/7"] not in out["fetched"]
    assert out["errors"] == []


# --- dismissing failed jobs: the dashboard buttons and the input page's red line ---

FAILED_JOB = {
    "id": 7, "type": "youtube", "kind": "youtube", "label": "Blender - Big Buck Bunny",
    "status": "failed", "error": "HTTP 403", "can_retry": True, "dismissed": False,
    "created_at": "", "updated_at": "",
}  # fmt: skip
DONE_JOB = {**FAILED_JOB, "id": 8, "status": "done", "error": None, "can_retry": False}


def dashboard_jobs(recent, dismissed=0, *steps, language="en-US"):
    answers = {
        "/api/dashboard": {**ANSWERS["/api/dashboard"], "recent": recent, "dismissed": dismissed},
        "POST /api/jobs/7/dismiss": {"id": 7, "dismissed": True},
        "DELETE /api/jobs/7/dismiss": {"id": 7, "dismissed": False},
        "POST /api/jobs/dismiss-failed": {"dismissed": 1},
    }
    return page("/dashboard", language=language, answers=answers, steps=[*steps])


@needs_node
def test_a_failed_job_can_be_dismissed_from_the_dashboard():
    out = dashboard_jobs([FAILED_JOB], 0, {"click": "#dash-recent-list .dismiss"})
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/jobs/7/dismiss"] in out["fetched"]
    # The list is fetched again afterwards, so the row goes away.
    assert out["fetched"].count(["GET", "/api/dashboard"]) >= 2


@needs_node
def test_the_dismiss_button_says_ausblenden_in_german_and_names_the_job():
    out = dashboard_jobs([FAILED_JOB], 0, {"read": "#dash-recent-list"}, language="de")
    assert "Ausblenden" in out["reads"]["#dash-recent-list"]["text"]
    out = dashboard_jobs([FAILED_JOB], 0, {"read": "#dash-recent-list"})
    assert "Dismiss" in out["reads"]["#dash-recent-list"]["text"]


@needs_node
def test_a_finished_job_has_no_dismiss_button():
    out = dashboard_jobs([DONE_JOB], 0, {"read": "#dash-recent-list"})
    assert "Dismiss" not in out["reads"]["#dash-recent-list"]["text"]
    out = dashboard_jobs([DONE_JOB], 0, {"read": "#dash-dismiss-all"})
    assert out["reads"]["#dash-dismiss-all"]["hidden"] is True


@needs_node
def test_dismiss_all_failed_shows_only_with_failed_jobs_and_posts():
    out = dashboard_jobs([FAILED_JOB, DONE_JOB], 0, {"read": "#dash-dismiss-all"})
    assert out["reads"]["#dash-dismiss-all"]["hidden"] is False
    assert "Dismiss all failed" in out["reads"]["#dash-dismiss-all"]["text"]
    out = dashboard_jobs([FAILED_JOB], 0, {"click": "#dash-dismiss-all"})
    assert out["errors"] == [], out["errors"]
    assert ["POST", "/api/jobs/dismiss-failed"] in out["fetched"]


@needs_node
def test_the_show_dismissed_toggle_appears_only_when_there_are_dismissed_jobs():
    out = dashboard_jobs([DONE_JOB], 0, {"read": "#dash-show-dismissed"})
    assert out["reads"]["#dash-show-dismissed"]["hidden"] is True
    out = dashboard_jobs([DONE_JOB], 2, {"read": "#dash-show-dismissed"})
    assert out["reads"]["#dash-show-dismissed"]["hidden"] is False
    assert out["reads"]["#dash-show-dismissed"]["text"] == "Show dismissed (2)"
    out = dashboard_jobs([DONE_JOB], 2, {"read": "#dash-show-dismissed"}, language="de")
    assert out["reads"]["#dash-show-dismissed"]["text"] == "Ausgeblendete anzeigen (2)"


@needs_node
def test_the_toggle_asks_for_the_dismissed_jobs_and_a_dismissed_job_can_be_shown_again():
    shown = {**FAILED_JOB, "dismissed": True}
    out = dashboard_jobs([DONE_JOB], 1, {"click": "#dash-show-dismissed"})
    assert out["errors"] == [], out["errors"]
    assert ["GET", "/api/dashboard?dismissed=1"] in out["fetched"]
    out = dashboard_jobs([shown], 1, {"read": "#dash-recent-list"}, {"read": "#dash-dismiss-all"})
    assert "Show again" in out["reads"]["#dash-recent-list"]["text"]
    assert out["reads"]["#dash-dismiss-all"]["hidden"] is True  # nothing left to dismiss
    out = dashboard_jobs([shown], 1, {"click": "#dash-recent-list .dismiss"})
    assert ["DELETE", "/api/jobs/7/dismiss"] in out["fetched"]


@needs_node
def test_a_dismissed_failed_job_does_not_colour_the_input_line_red():
    gone = {"jobs": [], "counts": {"queued": 0, "failed": 0, "dismissed": 1}}
    out = page("/", answers={"/api/jobs": gone}, steps=[
        {"read": "#jobs-mini"}, {"read": "#jobs-mini-text"},
    ])  # fmt: skip
    assert "failed" not in out["reads"]["#jobs-mini-text"]["text"]
    assert "jobs-mini-has-failed" not in out["reads"]["#jobs-mini"]["cls"]
    still = {"jobs": [], "counts": {"queued": 0, "failed": 1, "dismissed": 0}}
    out = page("/", answers={"/api/jobs": still}, steps=[
        {"read": "#jobs-mini"}, {"read": "#jobs-mini-text"},
    ])  # fmt: skip
    assert "failed: 1" in out["reads"]["#jobs-mini-text"]["text"]
    assert "jobs-mini-has-failed" in out["reads"]["#jobs-mini"]["cls"]
