"""The page's markup for signing in and for who sees what (#44). The behaviour of the scripts is in
test_page_scripts.py; the server decides all the same (test_auth_policy.py)."""

from __future__ import annotations

import re

import pytest

from ytp.web import STATIC_DIR

HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

pytestmark = pytest.mark.security


def tag_of(element_id: str) -> str:
    match = re.search(rf'<[a-z0-9]+\b[^>]*\bid="{element_id}"[^>]*>', HTML)
    assert match, f"no element with id {element_id}"
    return match.group(0)


@pytest.mark.parametrize(
    "element_id",
    ["vpn", "topnav", "form", "subscriptions", "dashboard", "accounts", "account-open"],
)
def test_what_needs_a_sign_in_is_marked_so(element_id):
    assert "data-auth-only" in tag_of(element_id)


def test_the_downloads_line_and_help_need_a_sign_in_too():
    assert re.search(
        r'<a id="jobs-mini" class="jobs-mini" href="/dashboard" data-view="input" data-auth-only',
        HTML,
    )
    assert re.search(r'<details class="help" data-view="input" data-auth-only', HTML)


@pytest.mark.parametrize(
    "element_id",
    [
        "alert-strip",
        "nav-accounts",
        "accounts",
        "alerts",
        "dash-disk",
        "dash-deploy",
        "dash-vpn-list",
    ],
)
def test_what_is_for_admins_is_marked_so(element_id):
    assert "data-admin-only" in tag_of(element_id)


def test_only_the_sign_in_form_is_for_visitors():
    assert "data-guest-only" in tag_of("login")
    assert HTML.count("data-guest-only") == 1


def test_the_page_starts_in_the_unknown_state_where_nothing_but_the_header_shows():
    assert re.search(r'<body data-auth="unknown">', HTML)


def test_the_style_sheet_hides_by_state_and_role():
    assert 'body:not([data-auth="in"]) [data-auth-only] { display: none !important; }' in CSS
    assert 'body:not([data-auth="out"]) [data-guest-only] { display: none !important; }' in CSS
    assert 'body:not([data-role="admin"]) [data-admin-only] { display: none !important; }' in CSS


def test_the_password_fields_are_styled_like_the_name_fields():
    """Issue #48: a password input matched none of the rules, so it was smaller and looked
    different from the text inputs next to it."""
    rule = re.search(r'input\[type="text"\][^{]*\{[^}]*\}', CSS)
    assert rule, "the text inputs are styled"
    assert 'input[type="password"]' in rule.group(0)
    focus = re.search(r'input\[type="text"\]:focus[^{]*\{', CSS)
    assert focus, "the text inputs have a focus style"
    assert 'input[type="password"]:focus' in focus.group(0)


def test_the_password_fields_are_password_fields_with_the_right_autocomplete():
    expected = {
        "login-password": "current-password",
        "account-current": "current-password",
        "account-new": "new-password",
        "account-repeat": "new-password",
        "accounts-password": "new-password",
    }
    for element_id, autocomplete in expected.items():
        tag = tag_of(element_id)
        assert 'type="password"' in tag, element_id
        assert f'autocomplete="{autocomplete}"' in tag, element_id
        assert "maxlength=" in tag, element_id


def test_every_form_of_the_page_stays_a_form_that_the_browser_never_sends_natively():
    for form_id in ("login-form", "account-form", "accounts-form"):
        tag = tag_of(form_id)
        assert 'method="post"' in tag
        assert 'action="/reload-needed"' in tag, "never the page itself (a password in a URL)"


def test_the_sign_in_script_loads_before_the_page_script_and_is_versioned_with_the_rest():
    assert HTML.index('src="/static/dashboard.js"') < HTML.index('src="/static/auth.js"')
    assert HTML.index('src="/static/auth.js"') < HTML.index('src="/static/app.js"')


def test_auth_js_sets_text_with_textcontent_only():
    source = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "document.write" not in source
    assert "localStorage" not in source, "no token or password is ever kept by a script"
    assert "document.cookie" not in source
