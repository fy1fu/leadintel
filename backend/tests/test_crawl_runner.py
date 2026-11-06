"""Website crawl runner (M3 step 3) with a real headless Chromium through the egress proxy.

Synthetic website on 127.0.0.2 (treated as public only via the test-only EgressPolicy hook) and a "private" service
on 127.0.0.1 that must never be reached. Skipped where the optional `crawl` extra (Playwright) is not installed.
"""
import ipaddress
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pw = pytest.importorskip("playwright.sync_api")

from app.config import REPO_ROOT  # noqa: E402
from app.costs.guard import CostGuard  # noqa: E402
from app.crawl.runner import CrawlLimits, SiteCrawler, USER_AGENT_TOKEN  # noqa: E402
from app.db.locks import UnlockedWrite  # noqa: E402
from app.net.browser_proxy import BrowserEgressProxy  # noqa: E402
from app.net.registry import load_registry  # noqa: E402
from app.net.safe_egress import EgressPolicy, SafeFetcher  # noqa: E402
from helpers import make_settings  # noqa: E402

TEST_NET = (ipaddress.ip_network("127.0.0.2/32"),)


class Site:
    """Synthetic small-business website; records every request with its arrival time."""
    def __init__(self, host, pages, robots=(200, "User-agent: *\nDisallow: /secret\n")):
        self.pages, self.robots, self.hits = pages, robots, []
        site = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                site.hits.append((self.path, time.monotonic(), self.headers.get("User-Agent", "")))
                if self.path == "/robots.txt":
                    code, body = site.robots
                    return self._send(code, body, "text/plain")
                p = site.pages.get(self.path)
                if p is None:
                    return self._send(404, "not found", "text/html")
                if isinstance(p, tuple) and p[0] == "redirect":
                    self.send_response(302)
                    self.send_header("Location", p[1])
                    self.end_headers()
                    return
                ctype = "application/pdf" if self.path.endswith(".pdf") else "text/html"
                return self._send(200, p, ctype)

            def _send(self, code, body, ctype):
                b = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer((host, 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def paths(self):
        return [h[0] for h in self.hits]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def html(title, body):
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


@pytest.fixture
def private():
    s = Site("127.0.0.1", {"/": html("private", "SECRET INTERNAL")})
    yield s
    s.close()


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=BrowserEgressProxy.chromium_args())
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium available: {exc}")
        yield b
        b.close()


def make_crawler(tmp_path, browser, ports, **limits):
    reg = load_registry(REPO_ROOT / "config" / "network.yaml")
    settings = make_settings(tmp_path)
    guard = CostGuard(reg, settings)
    policy = EgressPolicy(allowed_ports=frozenset(ports), _test_allow_networks=TEST_NET)
    fetcher = SafeFetcher(guard, ssl_context=ssl.create_default_context(), policy=policy,
                          user_agent=f"{USER_AGENT_TOKEN}/0.1")
    proxy = BrowserEgressProxy(policy=policy, guard=guard, idle_s=5).start()
    lim = CrawlLimits(per_host_interval_s=limits.pop("interval", 0.05), **limits)
    return SiteCrawler(fetcher=fetcher, proxy=proxy, browser=browser, data_dir=tmp_path / "data", limits=lim), proxy


@pytest.fixture
def site(private):
    base = "http://127.0.0.2:{port}"
    pages = {
        "/": html("Acme Lawn Care", '<h1>Acme</h1><a href="/about">About</a> <a href="/services#x">Services</a> '
                  '<a href="/secret">Hidden</a> <a href="/menu.pdf">Menu</a> <a href="https://example.org/x">Elsewhere</a> '
                  f'<a href="http://127.0.0.1:{private.port}/">Internal</a> <a href="mailto:a@b.c">Mail</a> '
                  '<a href="/go-inside">Redirect</a> <a href="/popup">Popup</a>'),
        "/about": html("About Acme", '<p>Family business since 1999.</p><a href="/">Home</a>'),
        "/services": html("Services", '<p>Mowing, edging.</p><a href="/contact">Contact</a>'),
        "/contact": html("Contact", '<p>Call (904) 555-0100</p>'),
        "/secret": html("Secret", "must never be fetched"),
        "/menu.pdf": "%PDF-1.4 not a page",
        "/go-inside": ("redirect", f"http://127.0.0.1:{private.port}/"),
        "/popup": html("Popup", f"<script>window.open('http://127.0.0.1:{private.port}/');"
                                "navigator.serviceWorker && navigator.serviceWorker.register('/sw.js').catch(()=>{});</script>"),
    }
    s = Site("127.0.0.2", pages)
    s.url = base.format(port=s.port) + "/"
    yield s
    s.close()


@pytest.mark.usefixtures("unlocked_files")
def test_crawls_the_site_politely_and_safely(tmp_path, browser, site, private):
    crawler, proxy = make_crawler(tmp_path, browser, {site.port, private.port}, interval=0.3)
    try:
        res = crawler.crawl(site.url, "acme-lawn")
    finally:
        proxy.stop()
    assert res.state == "crawled" and res.robots == "allowed", res.reason
    visited = [p.url.split(str(site.port))[1] for p in res.pages]
    assert visited[0] == "/" and {"/about", "/services", "/contact"} <= set(visited)
    assert "/secret" not in site.paths()                                   # robots.txt honoured
    assert "/menu.pdf" not in site.paths()                                 # not a web page
    assert private.hits == []                                              # the private service was never reached
    reasons = dict(res.skipped)
    assert reasons[f"{site.url}secret"] == "robots.txt" and reasons[f"{site.url}menu.pdf"] == "not a web page"
    assert reasons["https://example.org/x"] == "other site"
    inside = next(p for p in res.pages if p.url.endswith("/go-inside"))
    assert inside.blocked_reason and inside.status != 200                  # redirect into the private network refused
    home = res.pages[0]
    assert home.status == 200 and home.title == "Acme Lawn Care" and home.text_sha256 and home.links >= 5
    assert home.screenshot_desktop.endswith("-desktop.jpg") and home.screenshot_mobile.endswith("-mobile.jpg")
    for rel in (home.screenshot_desktop, home.screenshot_mobile):
        assert (tmp_path / "data" / rel).read_bytes()[:3] == b"\xff\xd8\xff"   # JPEG
    assert all(ua.startswith("Mozilla/5.0 (compatible; LeadIntelBot") or ua.startswith("LeadIntelBot")
               for _, _, ua in site.hits)
    times = sorted(t for p, t, _ in site.hits if p != "/robots.txt" and p != "/sw.js")
    # spacing is enforced when the crawler sends; arrival times can jitter a little under load
    assert all(b - a >= 0.2 for a, b in zip(times, times[1:])), [round(b - a, 3) for a, b in zip(times, times[1:])]


@pytest.mark.usefixtures("unlocked_files")
def test_page_limit_is_respected(tmp_path, browser):
    pages = {"/": html("Big", "".join(f'<a href="/p{i}">p{i}</a>' for i in range(40)))}
    pages |= {f"/p{i}": html(f"p{i}", "x") for i in range(40)}
    s = Site("127.0.0.2", pages, robots=(404, ""))
    crawler, proxy = make_crawler(tmp_path, browser, {s.port}, max_pages=5, screenshots=False)
    try:
        res = crawler.crawl(f"http://127.0.0.2:{s.port}/", "big")
    finally:
        proxy.stop(), s.close()
    assert res.robots == "none" and len(res.pages) == 5
    assert sum(1 for _, r in res.skipped if r == "page limit") == 36
    assert len([p for p in s.paths() if p.startswith("/p")]) == 4


@pytest.mark.parametrize("robots,state", [((500, "boom"), "robots_unavailable"),
                                          ((200, "User-agent: *\nDisallow: /\n"), "robots_disallowed"),
                                          ((200, "User-agent: LeadIntelBot\nDisallow: /\n"), "robots_disallowed")])
def test_robots_rules_stop_the_crawl(tmp_path, browser, robots, state):
    s = Site("127.0.0.2", {"/": html("x", "y")}, robots=robots)
    crawler, proxy = make_crawler(tmp_path, browser, {s.port})
    try:
        res = crawler.crawl(f"http://127.0.0.2:{s.port}/", "x")
    finally:
        proxy.stop(), s.close()
    assert res.state == state and res.pages == [] and s.paths() == ["/robots.txt"]


@pytest.mark.parametrize("start", ["http://127.0.0.1:{p}/", "http://10.0.0.5/", "http://localhost:{p}/",
                                   "file:///etc/passwd", "http://user:pw@127.0.0.2:{p}/"])
def test_unsafe_start_urls_are_refused_before_anything_runs(tmp_path, browser, private, start):
    crawler, proxy = make_crawler(tmp_path, browser, {private.port, 80})
    try:
        res = crawler.crawl(start.format(p=private.port), "x")
    finally:
        proxy.stop()
    assert res.state == "blocked" and res.pages == [] and private.hits == []


def test_crawl_delay_from_robots_is_honoured(tmp_path, browser):
    s = Site("127.0.0.2", {"/": html("x", '<a href="/a">a</a>'), "/a": html("a", "")},
             robots=(200, "User-agent: *\nCrawl-delay: 1\n"))
    crawler, proxy = make_crawler(tmp_path, browser, {s.port}, screenshots=False)
    try:
        res = crawler.crawl(f"http://127.0.0.2:{s.port}/", "x")
    finally:
        proxy.stop(), s.close()
    times = [t for p, t, _ in s.hits if p in ("/", "/a")]
    assert res.crawl_delay_s == 1.0 and len(times) == 2 and times[1] - times[0] >= 0.95


def test_screenshots_need_the_maintenance_lock(tmp_path, browser):
    s = Site("127.0.0.2", {"/": html("x", "y")}, robots=(404, ""))
    crawler, proxy = make_crawler(tmp_path, browser, {s.port})
    try:
        with pytest.raises(UnlockedWrite):
            crawler.crawl(f"http://127.0.0.2:{s.port}/", "x")
    finally:
        proxy.stop(), s.close()
    assert not (tmp_path / "data" / "crawl").exists()
