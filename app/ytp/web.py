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

**Scripts and styles are versioned (issue #43).** `GET /` and `/dashboard` are the page with
`?v=<hash of the static files>` on every `/static/` URL it names, and the page itself is never
cached (`no-cache`). A `/static/` file asked for with `?v=` is cached for a year (`immutable`), one
asked for without it is revalidated every time. After an update the page therefore always names
new URLs, and a browser can no longer combine a new page with old scripts (that is how a form
once got submitted natively and no job was created). The hash is also in the page
(`<meta name="ytp-static-version">`) and at `GET /api/version`, so an open page notices that the
server has been updated and asks for a reload. The form's own address is `/reload-needed`
(see below) so that it never submits to a page that looks fine.

The JSON endpoints additionally need `Content-Type: application/json`, which a cross-site HTML
form cannot send.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

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


VERSION_PLACEHOLDER = "__STATIC_VERSION__"
STATIC_REFERENCE = re.compile(r'((?:src|href)=")(/static/[A-Za-z0-9._-]+\.(?:js|css|svg))(")')
IMMUTABLE = "public, max-age=31536000, immutable"

RELOAD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>youtube-premium</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 36em; margin: 3em auto; padding: 0 1em">
<h1>Bitte die Seite neu laden / Please reload the page</h1>
<p>Die Skripte der Seite sind nicht geladen oder veraltet, deshalb wurde nichts abgeschickt.
Mit Strg+F5 (am Handy: Seite neu laden) geht es weiter. <a href="/">Zur Startseite</a></p>
<p>The page's scripts did not load or are out of date, so nothing was sent. Press Ctrl+F5
(on a phone: reload the page) and try again. <a href="/">Back to the start page</a></p>
</body></html>"""


def static_version(directory: Path = STATIC_DIR) -> str:
    """A short hash of everything under /static/: it changes exactly when a file does."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def render_page(template: str, version: str) -> str:
    """The page with the version in the meta tag and on every /static/ URL it names."""
    page = STATIC_REFERENCE.sub(rf"\1\2?v={version}\3", template)
    return page.replace(VERSION_PLACEHOLDER, version)


class VersionedStatic(StaticFiles):
    """`/static/`: a year of caching for URLs that carry `?v=`, revalidation for the rest."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            versioned = bool(parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v"))
            response.headers["Cache-Control"] = IMMUTABLE if versioned else "no-cache"
        return response


def register_ui(app: FastAPI) -> None:
    """Add `GET /` and `/dashboard` (the page), `/static/` (its files), `GET /api/version` and
    `POST /reload-needed` (where a form goes that the scripts did not take over)."""
    version = static_version()
    page = render_page((STATIC_DIR / "index.html").read_text(encoding="utf-8"), version)
    app.state.static_version = version

    async def index() -> HTMLResponse:
        # no-cache: the page names the versioned files of this release.
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    async def current_version() -> JSONResponse:
        return JSONResponse({"version": version}, headers={"Cache-Control": "no-store"})

    async def reload_needed() -> HTMLResponse:
        # Nothing of the request is read or shown (the body holds the pasted link).
        return HTMLResponse(RELOAD_PAGE, status_code=400, headers={"Cache-Control": "no-store"})

    app.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    # The dashboard (#38) is the same page: the script shows the dashboard view for this address.
    app.add_api_route("/dashboard", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/version", current_version, methods=["GET"], include_in_schema=False)
    app.add_api_route("/reload-needed", reload_needed, methods=["POST"], include_in_schema=False)
    app.mount("/static", VersionedStatic(directory=STATIC_DIR), name="static")
