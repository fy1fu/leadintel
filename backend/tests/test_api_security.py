"""Local web-app hardening (M1 S1): Host validation, unsafe-request guard, token, caching and security headers.

Offline and side-effect free: the network-diagnostics service is a stub that only counts calls, and the extra
methods are exercised on a test-only route. No database, no network.
"""
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.security import ALLOWED_HOSTS, safe_link

BASE = "http://127.0.0.1:8765"
ORIGIN = "http://127.0.0.1:8765"
TOKEN = "s1-test-token-" + "x" * 30
RUN = "/api/network/diagnostics/run"


class StubDiagnostics:
    def __init__(self):
        self.runs = 0

    def run(self, only=None, include_disabled=False):
        self.runs += 1
        return "stub-run"

    def full_report(self):
        return {"generated_at": "now", "latest_run_id": None, "egress": {"proxy": False}, "capabilities": [],
                "services": [], "paid_providers": [], "paid_requests_made": [], "not_required": []}


def make(token=TOKEN, base=BASE):
    diag = StubDiagnostics()
    app = create_app(SimpleNamespace(diagnostics=diag), token=token)

    @app.api_route("/api/_test/echo", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def echo():  # test-only route: never part of the production app
        return {"ok": True}

    return TestClient(app, base_url=base), diag


def ok_headers(**extra):
    return {"X-LeadIntel-Token": TOKEN, "Origin": ORIGIN, **extra}


# ---------------------------------------------------------------- Host validation (DNS rebinding)

@pytest.mark.parametrize("host", ["attacker.example", "127.0.0.1.nip.io", "localhost.attacker.example",
                                  "testserver", "0.0.0.0", "[::1]", "192.168.1.10"])
@pytest.mark.parametrize("path", ["/", "/api/health", "/api/prospects", "/diagnostics/network"])
def test_foreign_host_is_rejected(host, path):
    c, _ = make()
    assert c.get(path, headers={"Host": host}).status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8000"])
def test_local_hosts_are_accepted(host):
    c, _ = make()
    assert c.get("/api/health", headers={"Host": host}).status_code == 200


def test_production_host_list_has_no_test_entries():
    assert ALLOWED_HOSTS == ["127.0.0.1", "localhost"]


def test_rebound_post_is_rejected_even_with_token():
    c, diag = make()
    r = c.post(RUN, headers={**ok_headers(), "Host": "attacker.example"})
    assert r.status_code == 400 and diag.runs == 0


# ---------------------------------------------------------------- unsafe requests: token + same origin

def test_same_origin_post_with_token_is_allowed():
    c, diag = make()
    assert c.post(RUN, headers=ok_headers()).status_code == 200 and diag.runs == 1


@pytest.mark.parametrize("headers", [
    {"Origin": ORIGIN},                                                          # no token
    {"X-LeadIntel-Token": "wrong", "Origin": ORIGIN},                             # wrong token
    {"X-LeadIntel-Token": "", "Origin": ORIGIN},                                  # empty token
    {"X-LeadIntel-Token": TOKEN, "Origin": "https://evil.example"},               # cross-site with a valid token
    {"X-LeadIntel-Token": TOKEN, "Origin": "null"},                               # opaque origin
    {"X-LeadIntel-Token": TOKEN, "Origin": "http://127.0.0.1:9999"},              # another local port
    {"X-LeadIntel-Token": TOKEN, "Origin": "https://127.0.0.1:8765"},             # other scheme
    {"X-LeadIntel-Token": TOKEN, "Origin": "http://127.0.0.1.nip.io:8765"},       # look-alike host
    {"X-LeadIntel-Token": TOKEN, "Sec-Fetch-Site": "cross-site"},                 # no Origin, cross-site
    {"X-LeadIntel-Token": TOKEN, "Sec-Fetch-Site": "same-site"},                  # no Origin, same-site is not enough
    {"X-LeadIntel-Token": TOKEN, "Sec-Fetch-Site": "none"},                       # no Origin, user-initiated
    {"X-LeadIntel-Token": TOKEN},                                                 # neither Origin nor Sec-Fetch-Site
])
def test_unsafe_request_without_token_or_same_origin_is_refused(headers):
    c, diag = make()
    assert c.post(RUN, headers=headers).status_code == 403
    assert diag.runs == 0, "a refused request must not reach the endpoint"


def test_missing_origin_needs_exactly_same_origin_fetch_site():
    c, diag = make()
    assert c.post(RUN, headers={"X-LeadIntel-Token": TOKEN, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert diag.runs == 1


def test_localhost_origin_on_the_same_port_is_allowed():
    c, diag = make(base="http://localhost:8765")
    assert c.post(RUN, headers={"X-LeadIntel-Token": TOKEN, "Origin": "http://localhost:8765"}).status_code == 200
    assert diag.runs == 1


def test_token_from_a_previous_launch_is_refused():
    old, _ = make(token="previous-launch-token")
    old_token = old.app.state.token
    c, diag = make()                                     # a new server start
    assert c.post(RUN, headers={"X-LeadIntel-Token": old_token, "Origin": ORIGIN}).status_code == 403
    assert diag.runs == 0


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_every_unsafe_method_is_guarded(method):
    c, _ = make()
    assert c.request(method, "/api/_test/echo", headers={"Origin": ORIGIN}).status_code == 403
    assert c.request(method, "/api/_test/echo", headers=ok_headers()).status_code == 200


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_safe_methods_need_no_token(method):
    c, _ = make()
    assert c.request(method, "/api/_test/echo").status_code == 200


def test_each_launch_gets_its_own_random_token():
    a = create_app(SimpleNamespace(diagnostics=StubDiagnostics()))
    b = create_app(SimpleNamespace(diagnostics=StubDiagnostics()))
    assert a.state.token != b.state.token and len(a.state.token) >= 43


# ---------------------------------------------------------------- token delivery, caching, headers

@pytest.mark.parametrize("path", ["/", "/diagnostics/network"])
def test_pages_carry_the_token_with_a_nonce_and_are_not_cached(path):
    c, _ = make()
    r1, r2 = c.get(path), c.get(path)
    assert f'<meta name="leadintel-token" content="{TOKEN}">' in r1.text
    assert "__LEADINTEL_TOKEN__" not in r1.text and "__LEADINTEL_NONCE__" not in r1.text
    assert r1.headers["cache-control"] == "no-store" and r1.headers["pragma"] == "no-cache"
    csp1, csp2 = r1.headers["content-security-policy"], r2.headers["content-security-policy"]
    nonce = csp1.split("'nonce-")[1].split("'")[0]
    scripts = re.findall(r"<script\b[^>]*>", r1.text)
    assert scripts and all(f'nonce="{nonce}"' in s for s in scripts), "every script tag carries this response's nonce"
    assert csp1 != csp2, "each page response gets a fresh nonce"
    for directive in ("default-src 'none'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'",
                      "connect-src 'self' https://tiles.openfreemap.org"):
        assert directive in csp1
    assert "'unsafe-inline'" not in csp1.split("script-src")[1].split(";")[0]


def test_api_responses_are_not_cached_and_carry_security_headers():
    c, _ = make()
    r = c.get("/api/health")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"


def test_refused_responses_also_carry_security_headers():
    c, _ = make()
    r = c.post(RUN, headers={"Origin": ORIGIN})
    assert r.status_code == 403 and r.headers["referrer-policy"] == "no-referrer"


def test_page_templates_on_disk_contain_no_token():
    index = Path(__file__).resolve().parents[1] / "app" / "api" / "static" / "index.html"
    assert "__LEADINTEL_TOKEN__" in index.read_text(encoding="utf-8")
    assert TOKEN not in index.read_text(encoding="utf-8")


def test_token_never_appears_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    c, _ = make()
    c.get("/")
    c.post(RUN, headers=ok_headers())
    c.post(RUN, headers={"X-LeadIntel-Token": "wrong", "Origin": ORIGIN})
    assert TOKEN not in caplog.text


def test_no_cors_is_granted():
    c, _ = make()
    r = c.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = c.options("/api/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 403 and "access-control-allow-origin" not in pre.headers


# ---------------------------------------------------------------- untrusted link values

@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "JAVASCRIPT:alert(1)", " javascript:alert(1)", "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "file:///C:/Windows/win.ini",
    "ftp://example.com/x", "//example.com", "https://user:pass@example.com/", "https://exa mple.com",
    'https://example.com/"onmouseover="x', "https://example.com/<script>", "", None,
])
def test_unsafe_website_values_are_not_links(value):
    assert safe_link("web", value) is None


@pytest.mark.parametrize("value", ["https://example.com", "http://example.com/a?b=1", "HTTPS://Example.com/x"])
def test_safe_website_values_are_links(value):
    assert safe_link("web", value) == value


def test_phone_and_email_links():
    assert safe_link("tel", "+1 (904) 555-0100") == "tel:+19045550100"
    assert safe_link("tel", "javascript:alert(1)") is None and safe_link("tel", "12") is None
    assert safe_link("mailto", "owner@example.com") == "mailto:owner@example.com"
    for bad in ("a@b.com?bcc=x@y.com", "javascript:alert(1)@x.com", "no-at-sign", "a@b", "<a@b.com>"):
        assert safe_link("mailto", bad) is None
    assert safe_link("ftp", "https://example.com") is None


def test_interface_assets_are_served_without_the_token_and_versioned():
    c, _ = make()
    page = c.get("/").text
    version = re.search(r'/assets/app\.js\?v=([0-9a-f]{12})"', page).group(1)
    assert f'/assets/app.css?v={version}"' in page and "__ASSET_VERSION__" not in page
    for name, ctype in (("app.js", "javascript"), ("app.css", "text/css")):
        r = c.get(f"/assets/{name}?v={version}")
        assert r.status_code == 200 and ctype in r.headers["content-type"]
        assert TOKEN not in r.text and "__LEADINTEL_TOKEN__" not in r.text
        assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/assets/../security.py").status_code == 404
    assert c.get("/assets/index.html").status_code == 404        # the token-bearing template is not a static file


def test_interface_script_only_links_vetted_values():
    js = (Path(__file__).resolve().parents[1] / "app" / "api" / "static" / "assets" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" in js and "esc(" in js
    assert 'safeWeb = h => (typeof h === "string" && /^https?:\\/\\//i.test(h))' in js
    assert "safeContact" in js and 'rel="noopener noreferrer"' in js
    assert "eval(" not in js and "new Function" not in js
