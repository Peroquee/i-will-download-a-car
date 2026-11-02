"""The web UI and the browser-facing security (issues #25, #13 note).

* `register_ui` serves the submission form: `GET /` (`GET /dashboard` is the same page) and the
  files under `/static/` (HTML, CSS and scripts, all from this app; no CDN, no external font,
  nothing is fetched from another site).
* `SecurityMiddleware` does two things for every request:

  1. **Same-origin check for state-changing requests** (POST, PUT, PATCH, DELETE). A page on another
     website must not be able to make the user's browser, which holds the proxy login, add
     downloads. Browsers state where a request comes from: `Sec-Fetch-Site` must be `same-origin`
     (or `none`: typed in or bookmarked), and if there is an `Origin` header its host must be the
     `Host` the request was sent to (the proxy passes Host through unchanged). `Origin: null`
     is refused. A request with neither header comes from a program such as curl, not from a page
     in a browser, and is allowed: it has to pass the proxy's login like any other.
  2. **Security headers** on every answer: a content security policy that allows only this
     origin's own scripts and styles (no inline code, no framing, no form posts elsewhere),
     `nosniff`, no referrer, no framing.

The JSON endpoints additionally need `Content-Type: application/json`, which a cross-site HTML
form cannot send.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        b"connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; "
        b"frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-resource-policy", b"same-origin"),
)

REFUSED_BODY = b'{"detail":"This request does not come from this site and was refused."}'


def cross_site(headers: dict[bytes, bytes]) -> bool:
    """True if the browser says this request was not made by a page of this site."""
    fetch_site = headers.get(b"sec-fetch-site")
    if fetch_site is not None and fetch_site.decode("latin-1").lower() not in ALLOWED_FETCH_SITES:
        return True
    origin = headers.get(b"origin")
    if origin is not None:
        host = headers.get(b"host", b"").decode("latin-1").lower()
        origin_host = urlsplit(origin.decode("latin-1")).netloc.lower()
        if not origin_host or origin_host != host:
            return True  # includes "null" and a different port
    return False


class SecurityMiddleware:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        if scope["method"] in UNSAFE_METHODS and cross_site(headers):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(REFUSED_BODY)).encode()),
                        *SECURITY_HEADERS,
                    ],
                }
            )
            await send({"type": "http.response.body", "body": REFUSED_BODY})
            return

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = [
                    *message.get("headers", []),
                    *[(k, v) for k, v in SECURITY_HEADERS if k not in present],
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def register_ui(app: FastAPI) -> None:
    """Add `GET /` (the form) and `/static/` (its files)."""

    async def index() -> FileResponse:
        # no-cache: the page and its scripts change with each release of the app.
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    app.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    # The dashboard (#38) is the same page: the script shows the dashboard view for this address.
    app.add_api_route("/dashboard", index, methods=["GET"], include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
