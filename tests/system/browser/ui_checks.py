"""Runs INSIDE the Playwright container (see tests/system/test_c43_browser.py).

    python ui_checks.py http://127.0.0.1:PORT

A real headless Chromium against the real app: the form creates a job, the navigation and the VPN
bar lead where they say, and the failure modes of issue #43 (scripts that did not load, an
update while the page is open) end in a clear message instead of a silent native submit.
Prints one line per check and exits 1 at the first failure.
"""

from __future__ import annotations

import re
import sys

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


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(locale="en-US", viewport={"width": 1200, "height": 900})
        page = context.new_page()
        problems: list[str] = []
        watch(page, "main", problems)
        page.goto(BASE + "/")

        # 1. the scripts are alive, and the page names versioned files
        expect(page.locator("#js-warning")).to_be_hidden()
        sources = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
        assert sources, "no script on the page"
        assert all(re.search(r"\?v=[0-9a-f]{12}$", s) for s in sources), sources
        step("scripts started, their URLs carry the version")

        # 2. the navigation: four places, in order, in English for this browser
        expect(page.locator("#topnav > *")).to_have_text(
            ["Input", "Dashboard", "Subscriptions", "How to"]
        )
        expect(page.locator("#nav-input")).to_have_attribute("aria-current", "page")
        step("navigation reads Input | Dashboard | Subscriptions | How to")

        # 3. a YouTube link becomes a job (no native submit)
        page.fill("#link", LINK)
        expect(page.locator("#f-yt-title")).not_to_have_value("", timeout=20000)
        page.click("#submit")
        expect(page.locator("#jobs-list li")).to_have_count(1, timeout=20000)
        assert "link=" not in page.url, page.url
        assert page.url.rstrip("/") == BASE, page.url
        step("submitting the link made a job, the address did not change")

        # 4. the dashboard through the navigation
        page.click("#nav-dashboard")
        expect(page).to_have_url(re.compile(r"/dashboard$"))
        expect(page.locator("#dashboard")).to_be_visible()
        expect(page.locator("#nav-dashboard")).to_have_attribute("aria-current", "page")
        expect(page.locator("#dash-recent-list li, #dash-active-list li").first).to_be_visible(
            timeout=15000
        )
        step("the Dashboard tab opens the dashboard and shows the job")

        # 5. the VPN bar leads to the VPN part of the dashboard (from the dashboard ...)
        page.click("#vpn-link")
        expect(page).to_have_url(re.compile(r"/dashboard#vpn$"))
        expect(page.locator("#dash-vpn")).to_be_in_viewport()
        expect(page.locator("#dash-vpn-explain")).not_to_be_empty()
        step("the VPN bar opens the explanation and history")
        # ... and from the input page
        page.goto(BASE + "/")
        page.click("#vpn-link")
        expect(page).to_have_url(re.compile(r"/dashboard#vpn$"))
        step("the VPN bar works from the input page too")

        # 6. Subscriptions and the guide
        page.click("#nav-subs")
        expect(page).to_have_url(re.compile(r"/#subscriptions$"))
        expect(page.locator("#subscriptions")).to_be_in_viewport()
        page.click("#howto-open")
        expect(page.locator("#howto")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#howto")).to_be_hidden()
        step("Subscriptions scrolls to its part, the guide opens and closes")

        # 7. phone width: every place of the navigation is reachable and big enough
        phone = browser.new_context(
            locale="en-US", viewport={"width": 360, "height": 740}, has_touch=True
        ).new_page()
        phone.goto(BASE + "/")
        for item in phone.locator("#topnav > *").all():
            box = item.bounding_box()
            assert box, "an item of the navigation has no box"
            assert box["height"] >= 44, box
        phone.locator("#nav-dashboard").tap()
        expect(phone).to_have_url(re.compile(r"/dashboard$"))
        assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        step("on a phone the navigation is touchable and the page does not scroll sideways")

        # 8. scripts that did not load: warning, and a native submit goes to the reload page
        broken = browser.new_context(locale="en-US").new_page()
        broken.route("**/static/app.js*", lambda route: route.abort())
        broken.goto(BASE + "/")
        expect(broken.locator("#js-warning")).to_be_visible()
        broken.fill("#link", LINK)
        broken.press("#link", "Enter")
        broken.wait_for_load_state()
        assert broken.url.endswith("/reload-needed"), broken.url
        assert "link=" not in broken.url
        assert "reload" in broken.content().lower()
        jobs = broken.request.get(BASE + "/api/jobs").json()["jobs"]
        assert len(jobs) == 1, "the native submit made no second job"
        step("without scripts the form shows a warning and ends on the reload page, no job")

        # 9. the server is updated while a page is open: the page asks for a reload
        stale = browser.new_context(locale="en-US").new_page()
        stale.route(
            "**/api/version",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body='{"version": "000000000000"}'
            ),
        )
        stale.goto(BASE + "/")
        expect(stale.locator("#update-banner")).to_be_visible(timeout=15000)
        step("a new server version makes the open page offer a reload")

        assert not problems, problems
        step("no JavaScript errors, failed requests or bad static answers on the main page")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(f"FAIL {error}", file=sys.stderr, flush=True)
        sys.exit(1)
