"""Local web-app hardening (M1 S1).

LeadIntel's API listens on 127.0.0.1, but any web page open in the same browser is untrusted. Defences:

* Host validation (DNS rebinding): only ``127.0.0.1`` and ``localhost`` are accepted as Host. A rebound
  attacker domain keeps its own name in the Host header and is refused (Starlette TrustedHostMiddleware).
* Unsafe requests (every method except GET and HEAD, OPTIONS included) need BOTH
    1. ``X-LeadIntel-Token`` equal to this server process's token (constant-time comparison), and
    2. a same-origin signal: ``Origin`` exactly ``http://127.0.0.1:<port>`` or ``http://localhost:<port>`` (the
       server's own listening port); if Origin is absent, ``Sec-Fetch-Site`` must be exactly ``same-origin``;
       if both are absent the request is refused. ``Origin: null`` is refused.
  There is no CORS middleware, so cross-origin pages can neither read the token nor send the custom header.
* The token is generated per server start (``secrets.token_urlsafe(32)``), kept in memory only, delivered only
  inside the HTML pages that need it, and never written to disk, logs, URLs, cookies or browser storage.
  Pages carrying it, and all API responses, are sent with ``Cache-Control: no-store``.
* Security headers on every response, and a Content Security Policy with a per-response script nonce on HTML.
* ``safe_link`` turns untrusted source URLs, phones and emails into link targets only for safe schemes.
"""
from __future__ import annotations

import hmac
import re
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]          # fixed for production; tests use base_url http://127.0.0.1:<port>
TOKEN_HEADER = "X-LeadIntel-Token"
SAFE_METHODS = frozenset({"GET", "HEAD"})
TOKEN_PLACEHOLDER = "__LEADINTEL_TOKEN__"
NONCE_PLACEHOLDER = "__LEADINTEL_NONCE__"
MAP_HOSTS = "https://tiles.openfreemap.org"          # style, tiles, sprites and glyphs (checked 2026-11-05)
MAP_LIB = "https://unpkg.com/maplibre-gl@4.7.1/"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'none'",
        f"script-src 'self' 'nonce-{nonce}' {MAP_LIB}",
        f"style-src 'self' 'unsafe-inline' {MAP_LIB}",       # inline styles only; scripts are nonce-bound
        f"img-src 'self' data: blob: {MAP_HOSTS}",
        f"connect-src 'self' {MAP_HOSTS}",
        f"font-src 'self' {MAP_HOSTS}",
        "worker-src blob:",
        "child-src blob:",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ])


def same_origin(request: Request) -> bool:
    """Exact same-origin check for unsafe requests (see module docstring)."""
    origin = request.headers.get("origin")
    if origin is not None:
        server = request.scope.get("server") or (None, None)
        port = server[1]
        if port is None:
            return False
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
    return request.headers.get("sec-fetch-site") == "same-origin"


def token_ok(request: Request, token: str) -> bool:
    sent = request.headers.get(TOKEN_HEADER.lower())
    return bool(sent) and hmac.compare_digest(sent.encode(), token.encode())


class LocalRequestGuard(BaseHTTPMiddleware):
    """Refuses unsafe requests without the per-launch token and a same-origin signal; adds security headers."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method.upper() not in SAFE_METHODS:
            if not (same_origin(request) and token_ok(request, self._token)):
                resp: Response = PlainTextResponse("Forbidden: LeadIntel only accepts this request from its own page.",
                                                   status_code=403)
                return _harden(resp, request)
        return _harden(await call_next(request), request)


def _harden(resp: Response, request: Request) -> Response:
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.url.path.startswith("/api/") or resp.headers.get("content-type", "").startswith("text/html"):
        h["Cache-Control"] = "no-store"
        h["Pragma"] = "no-cache"
    return resp


def render_page(html: str, token: str) -> tuple[str, str]:
    """Inserts the token and a fresh script nonce into a page template; returns (html, nonce)."""
    nonce = new_nonce()
    return html.replace(TOKEN_PLACEHOLDER, token).replace(NONCE_PLACEHOLDER, nonce), nonce


_SAFE_WEB = {"http", "https"}
_PHONE = re.compile(r"^\+?[0-9]{7,15}$")
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def safe_link(kind: str, value: str | None) -> str | None:
    """Link target for an untrusted value, or None when it must be shown as plain text.

    kind: 'web' (http/https only), 'tel' (digits, optional leading +), 'mailto' (plain address).
    Anything else - javascript:, data:, vbscript:, file:, credentials in URLs, odd characters - gives None.
    """
    if not value:
        return None
    v = value.strip()
    if kind == "web":
        try:
            parts = urlsplit(v)
        except ValueError:
            return None
        if parts.scheme.lower() not in _SAFE_WEB or not parts.hostname or parts.username or parts.password:
            return None
        if any(c in v for c in "\"'<>\\ \t\r\n"):
            return None
        return v
    if kind == "tel":
        digits = re.sub(r"[\s().-]", "", v)
        return f"tel:{digits}" if _PHONE.match(digits) else None
    if kind == "mailto":
        return f"mailto:{v}" if _EMAIL.match(v) and len(v) <= 254 else None
    return None
