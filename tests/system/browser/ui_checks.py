"""Runs INSIDE the Playwright container (see tests/system/test_c43_browser.py).

    python ui_checks.py http://127.0.0.1:PORT

A real headless Chromium against the real app: the form creates a job, the navigation and the VPN
bar lead where they say, and the failure modes of issue #43 (scripts that did not load, an
update while the page is open) end in a clear message instead of a silent native submit.
Prints one line per check and exits 1 at the first failure.
"""

from __future__ import annotations

import os
import re
import sys
import traceback

from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1].rstrip("/")
LINK = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


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
    page.on(
        "requestfailed",
        lambda request: report("REQUEST FAILED", f"{request.url} {request.failure}"),
    )
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


def check(function):
    """Register a check. Each one prints its name BEFORE it runs, and on failure the exception's
    type and message (never an empty line); the run goes on with the next check."""
    CHECKS.append(function)
    return function


@check
def scripts_start_and_urls_are_versioned(ctx):
    page = ctx.page
    page.goto(BASE + "/")
    expect(page.locator("#js-warning")).to_be_hidden()
    sources = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
    assert sources, "no script on the page"
    assert all(re.search(r"\?v=[0-9a-f]{12}$", s) for s in sources), f"unversioned: {sources}"


@check
def navigation_reads_in_order(ctx):
    expect(ctx.page.locator("#topnav > *")).to_have_text(
        ["Input", "Dashboard", "Subscriptions", "How to"]
    )
    expect(ctx.page.locator("#nav-input")).to_have_attribute("aria-current", "page")


@check
def a_youtube_link_becomes_a_job(ctx):
    page = ctx.page
    page.fill("#link", LINK)
    expect(page.locator("#f-yt-title")).not_to_have_value("", timeout=20000)
    page.click("#submit")
    expect(page.locator("#jobs-list li")).to_have_count(1, timeout=20000)
    assert "link=" not in page.url, f"the link went into the address: {page.url}"
    assert page.url.rstrip("/") == BASE, f"the address changed: {page.url}"


@check
def the_dashboard_tab_opens_the_dashboard(ctx):
    page = ctx.page
    page.click("#nav-dashboard")
    expect(page).to_have_url(re.compile(r"/dashboard$"))
    expect(page.locator("#dashboard")).to_be_visible()
    expect(page.locator("#nav-dashboard")).to_have_attribute("aria-current", "page")
    expect(page.locator("#dash-recent-list li, #dash-active-list li").first).to_be_visible(
        timeout=15000
    )


@check
def the_vpn_bar_opens_the_vpn_part_from_the_dashboard(ctx):
    page = ctx.page
    page.click("#vpn-link")
    expect(page).to_have_url(re.compile(r"/dashboard#vpn$"))
    expect(page.locator("#dash-vpn")).to_be_in_viewport()
    expect(page.locator("#dash-vpn-explain")).not_to_be_empty()


@check
def the_vpn_bar_works_from_the_input_page_too(ctx):
    ctx.page.goto(BASE + "/")
    ctx.page.click("#vpn-link")
    expect(ctx.page).to_have_url(re.compile(r"/dashboard#vpn$"))


@check
def subscriptions_scrolls_and_the_guide_opens_and_closes(ctx):
    page = ctx.page
    page.click("#nav-subs")
    expect(page).to_have_url(re.compile(r"/#subscriptions$"))
    expect(page.locator("#subscriptions")).to_be_in_viewport()
    page.click("#howto-open")
    expect(page.locator("#howto")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#howto")).to_be_hidden()


@check
def on_a_phone_the_navigation_is_touchable_and_the_page_does_not_scroll_sideways(ctx):
    phone = ctx.browser.new_context(
        locale="en-US", viewport={"width": 360, "height": 740}, has_touch=True
    ).new_page()
    watch(phone, "phone", ctx.problems)
    phone.goto(BASE + "/")
    for item in phone.locator("#topnav > *").all():
        box = item.bounding_box()
        assert box, "an item of the navigation has no box"
        assert box["height"] >= 44, f"too small to touch: {box}"
    phone.locator("#nav-dashboard").tap()
    expect(phone).to_have_url(re.compile(r"/dashboard$"))
    for path in ("/", "/dashboard"):
        phone.goto(BASE + path)
        phone.wait_for_load_state()
        probe = phone.evaluate(OVERFLOW_PROBE)
        assert probe["scrollWidth"] <= probe["inner"] + 1, (
            f"{path} scrolls sideways on a phone: {probe}"
        )


@check
def without_its_scripts_the_form_warns_and_ends_on_the_reload_page(ctx):
    broken = ctx.browser.new_context(locale="en-US").new_page()
    broken.route("**/static/app.js*", lambda route: route.abort())
    broken.goto(BASE + "/")
    expect(broken.locator("#js-warning")).to_be_visible()
    broken.fill("#link", LINK)
    broken.press("#link", "Enter")
    broken.wait_for_url("**/reload-needed", timeout=15000)
    assert "link=" not in broken.url, f"the link is in the address: {broken.url}"
    body = broken.content().lower()
    assert "reload" in body, f"the reload page does not say reload: {body[:300]}"
    jobs = broken.request.get(BASE + "/api/jobs").json()["jobs"]
    assert len(jobs) == 1, f"the native submit made a job: {jobs}"


@check
def a_new_server_version_makes_the_open_page_offer_a_reload(ctx):
    stale = ctx.browser.new_context(locale="en-US").new_page()
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
