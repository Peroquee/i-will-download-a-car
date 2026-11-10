"""Runs INSIDE the Playwright container (see tests/system/test_c43_browser.py).

    python ui_checks.py http://127.0.0.1:PORT

A real headless Chromium against the real app: the sign-in (#44: a visitor sees only the form, a
wrong password is refused, the cookie is HttpOnly and SameSite=Strict, logout ends the session,
an admin and a user see different tabs), the form creates a job, the navigation and the VPN bar
lead where they say, and the failure modes of issue #43 (scripts that did not load, an update
while the page is open) end in a clear message instead of a silent native submit.
Each check prints its name; the run goes on after a failure and exits 1 at the end.

The admin (YTP_ADMIN / YTP_ADMIN_PASSWORD) exists when the app starts; the user "alice" is made
by the accounts page in a check below.
"""

from __future__ import annotations

import os
import re
import sys
import traceback

from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1].rstrip("/")
LINK = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
ADMIN = os.environ.get("YTP_ADMIN", "boss")
ADMIN_PASSWORD = os.environ.get("YTP_ADMIN_PASSWORD", "a long test password")  # a test value
USER = "alice"
USER_PASSWORD = "alice has a long password"  # a test value


def step(text: str) -> None:
    print(f"ok  {text}", flush=True)


def watch(page, label: str, problems: list[str]) -> None:
    """Print every script error, console error, failed request and bad /static/ answer of a page
    the moment it happens, from its very first navigation: a failing check then comes with the
    exact exception (and its line) that explains it."""

    def report(kind: str, text: str) -> None:
        line = f"{kind} [{label}]: {text}"
        problems.append(line)
        print(line, flush=True)

    page.on(
        "pageerror", lambda error: report("PAGE ERROR", f"{error} | {getattr(error, 'stack', '')}")
    )
    page.on(
        "console",
        lambda message: report("CONSOLE ERROR", f"{message.text} @ {message.location}")
        if message.type == "error"
        else None,
    )

    def request_failed(request) -> None:
        # A poll that is still running when the page navigates away is aborted by the browser:
        # that is not a failure of the page.
        if "ERR_ABORTED" not in str(request.failure):
            report("REQUEST FAILED", f"{request.url} {request.failure}")

    page.on("requestfailed", request_failed)
    page.on(
        "response",
        lambda response: report("BAD RESPONSE", f"{response.status} {response.url}")
        if response.status >= 400 and "/static/" in response.url
        else None,
    )


OVERFLOW_PROBE = """
() => {
  const width = window.innerWidth;
  const wide = [];
  for (const el of document.querySelectorAll('body *')) {
    const box = el.getBoundingClientRect();
    if (box.width > 0 && box.right > width + 1) {
      const cls = typeof el.className === 'string' ? el.className.replace(/ /g, '.') : '';
      const name = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (cls ? '.' + cls : '');
      wide.push(name + ' right=' + Math.round(box.right) + ' width=' + Math.round(box.width));
    }
  }
  return {inner: width, scrollWidth: document.documentElement.scrollWidth, wide: wide.slice(0, 12)};
}
"""

CHECKS = []


def fresh_page(ctx, **options):
    """A page in a new browser context (own cookies), not signed in. Not watched: a visitor's
    request for /api/auth/me answers 401, which the browser logs as a console error."""
    return ctx.browser.new_context(locale="en-US", **options).new_page()


def sign_in(page, name, password):
    page.goto(BASE + "/")
    page.fill("#login-user", name)
    page.fill("#login-password", password)
    page.click("#login-submit")
    expect(page.locator("#form")).to_be_visible(timeout=20000)


def signed_in(ctx, name=None, password=None, **options):
    page = fresh_page(ctx, **options)
    sign_in(page, name or ADMIN, password or ADMIN_PASSWORD)
    return page


def check(function):
    """Register a check. Each one prints its name BEFORE it runs, and on failure the exception's
    type and message (never an empty line); the run goes on with the next check."""
    CHECKS.append(function)
    return function


@check
def a_visitor_sees_only_the_sign_in_form_and_nothing_is_asked_of_the_app(ctx):
    page = fresh_page(ctx)
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/" in r.url else None)
    page.goto(BASE + "/")
    expect(page.locator("#login-form")).to_be_visible()
    for hidden in (
        "#form",
        "#topnav",
        "#jobs-mini",
        "#subs-list",
        "#vpn",
        "#vpn-indicator",
        "#account-open",
    ):
        expect(page.locator(hidden)).to_be_hidden()
    page.wait_for_timeout(1500)
    others = [u for u in asked if not u.endswith("/api/auth/me") and not u.endswith("/api/version")]
    assert not others, f"a visitor's page asked the app for: {others}"
    for path in ("/dashboard", "/accounts"):
        page.goto(BASE + path)
        expect(page.locator("#login-form")).to_be_visible()
        expect(page.locator("#dashboard")).to_be_hidden()
        expect(page.locator("#accounts")).to_be_hidden()


@check
def a_wrong_password_is_refused_with_a_message_and_no_cookie(ctx):
    page = fresh_page(ctx)
    page.goto(BASE + "/")
    page.fill("#login-user", ADMIN)
    page.fill("#login-password", "not the password!!")
    page.click("#login-submit")
    expect(page.locator("#login-error")).to_have_text("Wrong name or password.")
    expect(page.locator("#login-form")).to_be_visible()
    assert not [c for c in page.context.cookies() if c["name"] == "ytp_session"]


@check
def too_many_wrong_passwords_are_braked(ctx):
    page = fresh_page(ctx)
    page.goto(BASE + "/")
    for _ in range(5):
        page.fill("#login-user", "mallory")
        page.fill("#login-password", "guessing a password")
        page.click("#login-submit")
        expect(page.locator("#login-error")).to_have_text("Wrong name or password.")
        page.fill("#login-password", "")
    page.fill("#login-user", "mallory")
    page.fill("#login-password", "guessing a password")
    page.click("#login-submit")
    expect(page.locator("#login-error")).to_contain_text("Too many attempts. Please wait")


@check
def the_admin_signs_in_and_the_cookie_is_httponly_and_samesite_strict(ctx):
    page = fresh_page(ctx)
    sign_in(page, ADMIN, ADMIN_PASSWORD)
    expect(page.locator("#login-form")).to_be_hidden()
    expect(page.locator("#account-open")).to_have_text(ADMIN[0].upper())
    (cookie,) = [c for c in page.context.cookies() if c["name"] == "ytp_session"]
    assert cookie["httpOnly"] is True, cookie
    assert cookie["sameSite"] == "Strict", cookie
    assert "ytp_session" not in page.evaluate("document.cookie"), "a script can read the cookie"
    assert len(cookie["value"]) >= 40


@check
def logout_ends_the_session_on_the_server_so_a_copied_cookie_is_dead(ctx):
    page = signed_in(ctx)
    old = next(c["value"] for c in page.context.cookies() if c["name"] == "ytp_session")
    thief = fresh_page(ctx)
    thief.context.add_cookies([{"name": "ytp_session", "value": old, "url": BASE}])
    assert thief.request.get(BASE + "/api/auth/me").status == 200
    page.click("#account-open")
    page.click("#account-logout")
    expect(page.locator("#login-form")).to_be_visible()
    assert thief.request.get(BASE + "/api/auth/me").status == 401, "logout must end the session"
    assert not [c for c in page.context.cookies() if c["name"] == "ytp_session" and c["value"]]


@check
def scripts_start_and_urls_are_versioned(ctx):
    page = ctx.page
    page.goto(BASE + "/")
    expect(page.locator("#js-warning")).to_be_hidden()
    expect(page.locator("#form")).to_be_visible()
    sources = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
    assert sources, "no script on the page"
    assert all(re.search(r"\?v=[0-9a-f]{12}$", s) for s in sources), f"unversioned: {sources}"


@check
def navigation_reads_in_order(ctx):
    expect(ctx.page.locator("#topnav > *:visible")).to_have_text(
        ["Input", "Dashboard", "Subscriptions", "Accounts", "How to"]
    )
    expect(ctx.page.locator("#nav-input")).to_have_attribute("aria-current", "page")


@check
def a_youtube_link_becomes_a_job(ctx):
    page = ctx.page
    page.fill("#link", LINK)
    expect(page.locator("#f-yt-title")).not_to_have_value("", timeout=20000)
    page.click("#submit")
    expect(page.locator("#result")).to_contain_text("Job #", timeout=20000)
    assert len(page.request.get(BASE + "/api/jobs").json()["jobs"]) == 1
    # the input page has one line about the downloads, the jobs themselves are on the dashboard
    expect(page.locator("#jobs-mini-text")).not_to_have_text("", timeout=15000)
    assert "link=" not in page.url, f"the link went into the address: {page.url}"
    assert page.url.rstrip("/") == BASE, f"the address changed: {page.url}"


@check
def the_dashboard_tab_opens_the_dashboard(ctx):
    page = ctx.page
    page.click("#nav-dashboard")
    expect(page).to_have_url(re.compile(r"/dashboard$"))
    expect(page.locator("#dashboard")).to_be_visible()
    expect(page.locator("#nav-dashboard")).to_have_attribute("aria-current", "page")
    expect(page.locator("#dash-tab-overview")).to_have_attribute("aria-current", "page")
    expect(page.locator("#status-internet-state")).not_to_be_empty(timeout=15000)
    page.click("#dash-tab-jobs")
    expect(page).to_have_url(re.compile(r"/dashboard#jobs$"))
    expect(page.locator("#dash-jobs-view")).to_be_visible()
    expect(page.locator("#dash-recent-list li, #dash-active-list li").first).to_be_visible(
        timeout=15000
    )


@check
def the_vpn_bar_opens_the_vpn_part_from_the_dashboard(ctx):
    page = ctx.page
    page.click("#vpn-indicator")
    expect(page).to_have_url(re.compile(r"/dashboard#vpn$"))
    expect(page.locator("#dash-vpn")).to_be_in_viewport()
    expect(page.locator("#dash-vpn-now")).not_to_be_empty()
    expect(page.locator("#dash-tab-vpn")).to_have_attribute("aria-current", "page")


@check
def the_vpn_bar_works_from_the_input_page_too(ctx):
    ctx.page.goto(BASE + "/")
    ctx.page.click("#vpn-indicator")
    expect(ctx.page).to_have_url(re.compile(r"/dashboard#vpn$"))


@check
def subscriptions_scrolls_and_the_guide_opens_and_closes(ctx):
    page = ctx.page
    page.click("#nav-subs")
    expect(page).to_have_url(re.compile(r"/#subscriptions$"))
    expect(page.locator("#subscriptions")).to_be_in_viewport()
    # a view for managing them: no form, no downloads line, and the tab is the current one
    expect(page.locator("#form")).to_be_hidden()
    expect(page.locator("#jobs-mini")).to_be_hidden()
    expect(page.locator("#subscriptions input")).to_have_count(0)
    expect(page.locator("#nav-subs")).to_have_attribute("aria-current", "page")
    page.click("#nav-input")
    expect(page.locator("#form")).to_be_visible()
    expect(page.locator("#subscriptions")).to_be_hidden()
    page.click("#nav-subs")
    page.click("#howto-open")
    expect(page.locator("#howto")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#howto")).to_be_hidden()


@check
def the_subscription_settings_window_saves_resolution_and_check_interval(ctx):
    page = ctx.page
    page.goto(BASE + "/#subscriptions")
    row = page.locator("#subs-youtube-list li", has_text="Blender")
    expect(row).to_be_visible(timeout=15000)
    # the poll is switched off in this app (.env): that is what "server default" means here
    expect(row).to_contain_text("Server default (paused)")
    expect(row.locator("select")).to_have_count(0)  # the inline resolution row is gone
    button = row.get_by_role("button", name="Settings")
    button.click()
    dialog = page.locator("#sub-settings")
    expect(dialog).to_be_visible()
    expect(page.locator("#sub-settings-resolution")).to_be_focused()
    expect(page.locator("body")).to_have_class(re.compile(r"\bmodal-open\b"))
    # Esc closes it and the focus goes back to the button
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(button).to_be_focused()
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bmodal-open\b"))
    # the Close button too
    button.click()
    page.click("#sub-settings-close")
    expect(dialog).to_be_hidden()
    expect(button).to_be_focused()
    # change the resolution and the interval and save
    button.click()
    page.select_option("#sub-settings-resolution", "480")
    page.select_option("#sub-settings-interval", "120")
    page.select_option("#sub-settings-at-mode", "time")
    page.fill("#sub-settings-at", "06:00")
    page.click("#sub-settings-save")
    expect(dialog).to_be_hidden()
    expect(row).to_contain_text("every 2 h")
    expect(row).to_contain_text("from 06:00")
    expect(row).to_contain_text("The settings were saved.")
    expect(button).to_be_focused()  # the list was rebuilt: the focus came back to the new button
    # a value the server refuses is shown inside the window, which stays open
    button.click()
    page.select_option("#sub-settings-interval", "custom")
    page.fill("#sub-settings-minutes", "5")
    page.click("#sub-settings-save")
    expect(page.locator("#sub-settings-error")).to_contain_text("15 to 1440")
    expect(dialog).to_be_visible()
    page.click("#sub-settings-cancel")
    expect(dialog).to_be_hidden()


@check
def the_admin_saves_the_server_defaults_on_the_accounts_page(ctx):
    page = ctx.page
    page.goto(BASE + "/accounts")
    expect(page.locator("#server-defaults")).to_be_visible(timeout=15000)
    page.select_option("#sd-channel-interval", "60")
    page.select_option("#sd-podcast-interval", "360")
    page.click("#sd-save")
    expect(page.locator("#sd-result")).to_have_text("The server defaults were saved.")
    page.reload()
    expect(page.locator("#sd-channel-interval")).to_have_value("60")
    expect(page.locator("#sd-podcast-interval")).to_have_value("360")


@check
def on_a_phone_the_navigation_is_touchable_and_the_page_does_not_scroll_sideways(ctx):
    options = {"viewport": {"width": 360, "height": 740}, "has_touch": True}
    visitor = fresh_page(ctx, **options)
    visitor.goto(BASE + "/")
    expect(visitor.locator("#login-form")).to_be_visible()
    probe = visitor.evaluate(OVERFLOW_PROBE)
    assert probe["scrollWidth"] <= probe["inner"] + 1, f"the sign-in scrolls sideways: {probe}"
    phone = signed_in(ctx, **options)
    watch(phone, "phone", ctx.problems)
    phone.goto(BASE + "/")
    for item in phone.locator("#topnav > *").all():
        box = item.bounding_box()
        assert box, "an item of the navigation has no box"
        assert box["height"] >= 44, f"too small to touch: {box}"
    phone.locator("#nav-dashboard").tap()
    expect(phone).to_have_url(re.compile(r"/dashboard$"))
    for path in ("/", "/#subscriptions", "/dashboard", "/accounts"):
        phone.goto(BASE + path)
        phone.wait_for_load_state()
        probe = phone.evaluate(OVERFLOW_PROBE)
        assert probe["scrollWidth"] <= probe["inner"] + 1, (
            f"{path} scrolls sideways on a phone: {probe}"
        )


@check
def without_its_scripts_the_form_warns_and_ends_on_the_reload_page(ctx):
    broken = fresh_page(ctx)
    broken.route("**/static/app.js*", lambda route: route.abort())
    broken.goto(BASE + "/")
    expect(broken.locator("#js-warning")).to_be_visible()
    # Without its scripts the page does not know who is signed in, so the form stays hidden; a
    # submit that gets through anyway (here: by script) must still end on the reload page.
    expect(broken.locator("#form")).to_be_hidden()
    broken.evaluate("document.getElementById('form').requestSubmit()")
    broken.wait_for_url("**/reload-needed", timeout=15000)
    assert "link=" not in broken.url, f"the link is in the address: {broken.url}"
    body = broken.content().lower()
    assert "reload" in body, f"the reload page does not say reload: {body[:300]}"
    jobs = ctx.page.request.get(BASE + "/api/jobs").json()["jobs"]
    assert len(jobs) == 1, f"the native submit made a job: {jobs}"


@check
def a_new_server_version_makes_the_open_page_offer_a_reload(ctx):
    stale = signed_in(ctx)
    watch(stale, "stale", ctx.problems)
    stale.route(
        "**/api/version",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"version": "000000000000"}'
        ),
    )
    stale.goto(BASE + "/")
    expect(stale.locator("#update-banner")).to_be_visible(timeout=15000)


@check
def a_failed_youtube_lookup_says_why_and_channel_and_title_can_be_typed(ctx):
    page = signed_in(ctx)
    # (not watched: the browser logs the 502 we stage here as a console error, on purpose)
    page.route(
        "**/api/youtube/probe",
        lambda route: route.fulfill(
            status=502,
            content_type="application/json",
            body='{"detail": "YouTube wants proof", "kind": "bot_check"}',
        ),
    )
    page.goto(BASE + "/")
    page.fill("#link", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    status = page.locator("#probe-status")
    expect(status).to_contain_text("not a bot", timeout=15000)
    expect(status).to_contain_text("You can enter channel and title yourself.")
    expect(page.locator("#f-channel")).to_be_editable()
    page.fill("#f-channel", "Rick")
    page.fill("#f-yt-title", "Never")
    page.click("#submit")
    expect(page.locator("#result")).to_contain_text("Job #", timeout=20000)
    assert len(page.request.get(BASE + "/api/jobs").json()["jobs"]) == 2


@check
def the_admin_makes_an_account_on_the_accounts_page(ctx):
    page = ctx.page
    page.click("#nav-accounts")
    expect(page).to_have_url(re.compile(r"/accounts$"))
    expect(page.locator("#accounts")).to_be_visible()
    expect(page.locator("#nav-accounts")).to_have_attribute("aria-current", "page")
    expect(page.locator("#accounts-list li")).to_have_count(1, timeout=15000)
    page.fill("#accounts-name", USER)
    page.fill("#accounts-password", USER_PASSWORD)
    page.select_option("#accounts-role", "user")
    page.click("#accounts-add")
    expect(page.locator("#accounts-result")).to_have_text(f'Account "{USER}" created.')
    expect(page.locator("#accounts-list li")).to_have_count(2, timeout=15000)
    expect(page.locator("#accounts-list")).to_contain_text(USER)


@check
def a_user_sees_other_tabs_than_the_admin_and_none_of_the_admins_jobs(ctx):
    page = signed_in(ctx, USER, USER_PASSWORD)
    expect(page.locator("#topnav > *:visible")).to_have_text(
        ["Input", "Dashboard", "Subscriptions", "How to"]
    )
    expect(page.locator("#nav-accounts")).to_be_hidden()
    expect(page.locator("#account-open")).to_have_text(USER[0].upper())
    expect(page.locator("#alert-strip")).to_be_hidden()
    expect(page.locator("#jobs-mini-text")).to_have_text("nothing running right now", timeout=15000)
    assert page.request.get(BASE + "/api/jobs").json()["jobs"] == []
    page.goto(BASE + "/dashboard")
    expect(page.locator("#dashboard")).to_be_visible()
    expect(page.locator("#dash-tab-alerts")).to_be_hidden()
    page.click("#dash-tab-vpn")
    expect(page.locator("#dash-vpn")).to_be_visible()
    expect(page.locator("#dash-disk")).to_be_hidden()
    expect(page.locator("#dash-vpn-list")).to_be_hidden()
    expect(page.locator("#dash-error")).to_have_text("")
    page.goto(BASE + "/accounts")
    expect(page).to_have_url(BASE + "/")
    assert page.request.get(BASE + "/api/accounts").status == 403
    assert page.request.get(BASE + "/api/alerts").status == 403
    expect(page.locator("#account-open")).to_have_text(USER[0].upper())


@check
def a_user_changes_the_own_password_and_the_other_devices_are_signed_out(ctx):
    here = signed_in(ctx, USER, USER_PASSWORD)
    elsewhere = signed_in(ctx, USER, USER_PASSWORD)
    new_password = USER_PASSWORD + " 2"
    here.click("#account-open")
    here.fill("#account-current", USER_PASSWORD)
    here.fill("#account-new", new_password)
    here.fill("#account-repeat", new_password)
    here.click("#account-change")
    expect(here.locator("#account-result")).to_contain_text("The password was changed")
    assert here.request.get(BASE + "/api/auth/me").status == 200
    assert elsewhere.request.get(BASE + "/api/auth/me").status == 401
    old = fresh_page(ctx)
    old.goto(BASE + "/")
    old.fill("#login-user", USER)
    old.fill("#login-password", USER_PASSWORD)
    old.click("#login-submit")
    expect(old.locator("#login-error")).to_have_text("Wrong name or password.")
    sign_in(fresh_page(ctx), USER, new_password)


@check
def a_page_on_another_site_cannot_act_with_the_admins_cookie(ctx):
    for origin in ("null", "http://evil.example"):
        response = ctx.page.request.post(
            BASE + "/api/accounts",
            data={"username": "mallory", "password": "a long test password"},
            headers={"Origin": origin},
        )
        assert response.status == 403, (origin, response.status)
    listed = ctx.page.request.get(BASE + "/api/accounts").json()["accounts"]
    assert sorted(a["username"] for a in listed) == sorted([ADMIN, USER])


@check
def no_script_errors_failed_requests_or_bad_static_answers(ctx):
    assert not ctx.problems, "; ".join(ctx.problems)


class Context:
    def __init__(self, browser, page, problems):
        self.browser, self.page, self.problems = browser, page, problems


def main() -> None:
    failures: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(
            locale="en-US", viewport={"width": 1200, "height": 900}
        ).new_page()
        problems: list[str] = []
        watch(page, "main", problems)
        # the admin of the main page signs in through the API (the form is checked above)
        login = page.request.post(
            BASE + "/api/auth/login", data={"username": ADMIN, "password": ADMIN_PASSWORD}
        )
        assert login.status == 200, f"the admin could not sign in: {login.status}"
        ctx = Context(browser, page, problems)
        for number, function in enumerate(CHECKS, start=1):
            name = function.__name__.replace("_", " ")
            print(f"run {number}. {name}", flush=True)
            try:
                function(ctx)
            except BaseException as error:
                detail = f"{type(error).__name__}: {error}".strip()
                print(f"FAIL {number}. {name}\n     {detail[:1500]}", flush=True)
                if os.environ.get("YTP_TRACEBACK"):
                    traceback.print_exc()
                failures.append(name)
            else:
                step(name)
        browser.close()
    if failures:
        print(
            f"FAILED {len(failures)} of {len(CHECKS)}: {', '.join(failures)}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    print(f"all {len(CHECKS)} checks passed", flush=True)


if __name__ == "__main__":
    main()
