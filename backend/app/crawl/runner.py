"""Website crawl runner (M3 step 3; research/M3_PREP.md §1). Prepares the evidence the website audit (WQS) needs.

Politeness and safety, in this order:
* the start URL must pass the same SSRF validation as every website fetch (``SafeFetcher.validate_url``) - a
  private or blocked start never launches a browser;
* ``robots.txt`` is fetched with the SSRF-safe fetcher and honoured for the crawler's user-agent token.
  4xx = no rules (crawl allowed); 5xx / network error = NOT crawled (conservative); a Crawl-delay is honoured
  (capped at 10 s);
* every browser request goes through ``BrowserEgressProxy`` (SSRF / DNS-rebinding protection), in a fresh
  browser context per site: no stored cookies, service workers blocked, downloads refused, pop-ups closed,
  permissions denied;
* only GET and HEAD requests leave the browser: a form post, beacon or upload started by a page script is
  aborted (the proxy cannot see methods inside an https tunnel, the browser's request routing can);
* same site only, breadth-first, at most ``max_pages`` pages, at least ``per_host_interval_s`` between requests;
  logins, CAPTCHAs and bot walls are never worked around - they simply end up as what the page shows.

Hostile pages: a page script that never yields (an endless loop after load) freezes every browser call, and
Playwright's own timeouts do not cover that. ``Watchdog`` therefore bounds each page visit: when one runs far past
its limits, the browser driver is killed (Chromium exits with it) and the crawl ends with the pages loaded so far.

Results are returned as data (``CrawlResult``); storing them in the database comes with the M3 schema (0007).
Screenshots are written into the managed data folder and therefore need the maintenance lock, like Load Data.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import threading
import time
from contextlib import contextmanager, nullcontext
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from app.crawl.checks import AXE_MAX_PAGES, AXE_PATH, AXE_RUN_JS, FACTS_JS, PERF_JS, Criterion, evaluate_site
from app.db.locks import LockLost, UnlockedWrite, ensure_write_allowed
from app.net.browser_proxy import BrowserEgressProxy
from app.net.safe_egress import BlockedDestination, SafeFetcher

USER_AGENT_TOKEN = "LeadIntelBot"
USER_AGENT = f"Mozilla/5.0 (compatible; {USER_AGENT_TOKEN}/0.1; local prospect research)"
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|ico|zip|rar|7z|gz|mp[34]|mov|avi|wmv|docx?|xlsx?|pptx?|exe|dmg|css|js|json|xml)$",
                       re.I)
PROXY_REFUSAL = "LeadIntel crawler proxy:"
log = logging.getLogger(__name__)


class Watchdog:
    """Kills the browser driver when one page visit runs far past its limits (see the module docstring)."""
    def __init__(self, kill):
        self._kill, self.fired = kill, False

    @contextmanager
    def guard(self, seconds: float):
        t = threading.Timer(seconds, self._fire)
        t.daemon = True
        t.start()
        try:
            yield
        finally:
            t.cancel()

    def _fire(self) -> None:
        self.fired = True
        try:
            self._kill()
        except Exception:  # noqa: BLE001
            log.exception("could not stop the browser driver")


def driver_killer(playwright_cm):
    """kill() for the driver process behind a ``sync_playwright()`` context manager (Playwright 1.63 internals; the
    tests fail if they move). Chromium is started over a pipe by the driver and exits when the driver goes."""
    proc = getattr(getattr(getattr(playwright_cm, "_connection", None), "_transport", None), "_proc", None)
    pid = getattr(proc, "pid", None)
    if pid is None:
        return None

    def kill():
        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
    return kill


@dataclass
class CrawlLimits:
    max_pages: int = 15
    per_host_interval_s: float = 1.0
    max_crawl_delay_s: float = 10.0
    page_timeout_s: float = 20.0
    max_html_bytes: int = 3_000_000
    screenshots: bool = True
    page_budget_s: float = 60.0      # watchdog: one whole page visit (navigation, checks, screenshots)
    desktop: tuple[int, int] = (1366, 900)
    mobile: tuple[int, int] = (390, 844)


@dataclass
class PageResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    html_bytes: int = 0
    html_truncated: bool = False
    text_sha256: str | None = None
    links: int = 0
    elapsed_ms: int = 0
    screenshot_desktop: str | None = None      # path relative to the data folder
    screenshot_mobile: str | None = None
    blocked_reason: str | None = None
    error: str | None = None
    facts: dict | None = None                  # app.crawl.checks.FACTS_JS result (read-only DOM facts)
    axe: list | None = None                    # axe-core violations (None = not run on this page)
    artifacts: list = field(default_factory=list)   # [{kind, path, sha256, bytes}]


@dataclass
class CrawlResult:
    start_url: str
    state: str = "pending"                     # crawled | blocked | robots_disallowed | robots_unavailable | failed
    reason: str | None = None
    robots: str | None = None                  # allowed | none (4xx) | unavailable
    crawl_delay_s: float | None = None
    pages: list[PageResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    mobile_home_facts: dict | None = None      # the home page measured at phone width
    perf_home: dict | None = None              # browser timing of the home page (lab measurement)
    refused_writes: int = 0                    # POST/PUT/... requests a page tried to send (all refused)

    def criteria(self, today=None) -> list[Criterion]:
        """Website-check criteria from the pages that loaded (none when nothing could be crawled)."""
        loaded = [p for p in self.pages if p.facts]
        if not loaded:
            return []
        axe = [p.axe for p in loaded] if any(p.axe is not None for p in loaded) else None
        broken = [p.url for p in self.pages if p.status is not None and p.status >= 400]
        return evaluate_site([p.facts for p in loaded], self.mobile_home_facts, today=today,
                             perf_home=self.perf_home, axe=axe, broken_pages=broken)


def _norm(url: str) -> str:
    u, _ = urldefrag(url)
    return u


def _same_site(a: str, b: str) -> bool:
    ha, hb = (urlsplit(a).hostname or "").lower(), (urlsplit(b).hostname or "").lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h               # noqa: E731
    return strip(ha) == strip(hb) and urlsplit(a).port == urlsplit(b).port


class SiteCrawler:
    def __init__(self, *, fetcher: SafeFetcher, proxy: BrowserEgressProxy, browser, data_dir: Path,
                 limits: CrawlLimits | None = None, clock=time.monotonic, sleep=time.sleep,
                 watchdog: Watchdog | None = None):
        self.fetcher, self.proxy, self.browser, self.watchdog = fetcher, proxy, browser, watchdog
        self.data_dir = Path(data_dir)
        self.limits = limits or CrawlLimits()
        self._clock, self._sleep = clock, sleep
        self._last_request: dict[str, float] = {}

    # ------------------------------------------------------------------ politeness
    def _robots(self, start: str, res: CrawlResult) -> RobotFileParser | None:
        u = urlsplit(start)
        robots_url = f"{u.scheme}://{u.netloc}/robots.txt"
        self._wait_turn(robots_url)
        r = self.fetcher.fetch(robots_url)
        rp = RobotFileParser()
        if r.blocked_reason:
            res.robots, res.state, res.reason = "unavailable", "blocked", f"robots.txt: {r.blocked_reason}"
            return None
        if r.error or r.status is None or r.status >= 500:
            res.robots, res.state = "unavailable", "robots_unavailable"
            res.reason = f"robots.txt could not be read ({r.error or r.status}); not crawled"
            return None
        if r.status >= 400:
            res.robots = "none"
            rp.parse([])
        else:
            res.robots = "allowed"
            rp.parse(r.body.decode("utf-8", "replace").splitlines())
        delay = rp.crawl_delay(USER_AGENT_TOKEN)
        if delay:
            res.crawl_delay_s = min(float(delay), self.limits.max_crawl_delay_s)
        return rp

    def _interval(self, res: CrawlResult) -> float:
        return max(self.limits.per_host_interval_s, res.crawl_delay_s or 0.0)

    def _wait_turn(self, url: str, interval: float | None = None) -> None:
        host = (urlsplit(url).hostname or "").lower()
        gap = self.limits.per_host_interval_s if interval is None else interval
        last = self._last_request.get(host)
        if last is not None:
            wait = last + gap - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    # ------------------------------------------------------------------ files
    MAX_SCREENSHOT_BYTES = 4_000_000

    def _save(self, pr: PageResult, site_dir: Path, kind: str, name: str, data: bytes) -> str | None:
        if len(data) > self.MAX_SCREENSHOT_BYTES:          # an absurdly large capture is not kept
            return None
        if not site_dir.is_dir():
            ensure_write_allowed(site_dir)
            site_dir.mkdir(parents=True, exist_ok=True)
        path = site_dir / name
        ensure_write_allowed(path)
        path.write_bytes(data)
        rel = path.relative_to(self.data_dir).as_posix()
        pr.artifacts.append({"kind": kind, "path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        return rel

    # ------------------------------------------------------------------ crawl
    def crawl(self, start_url: str, site_key: str) -> CrawlResult:
        res = CrawlResult(start_url=start_url, started_at=time.time())
        try:
            self.fetcher.validate_url(start_url)
        except BlockedDestination as exc:
            res.state, res.reason = "blocked", exc.reason
            res.finished_at = time.time()
            return res
        rp = self._robots(start_url, res)
        if rp is None:
            res.finished_at = time.time()
            return res
        if not rp.can_fetch(USER_AGENT_TOKEN, start_url):
            res.state, res.reason = "robots_disallowed", "robots.txt disallows the start page"
            res.finished_at = time.time()
            return res
        site_dir = self.data_dir / "crawl" / re.sub(r"[^a-z0-9_.-]", "_", site_key.lower())[:80] / time.strftime("%Y%m%d-%H%M%S")
        ctx = self.browser.new_context(proxy=self.proxy.playwright_proxy(), user_agent=USER_AGENT,
                                       viewport={"width": self.limits.desktop[0], "height": self.limits.desktop[1]},
                                       service_workers="block", accept_downloads=False, java_script_enabled=True,
                                       permissions=[], ignore_https_errors=False,
                                       bypass_csp=True)   # lets the vendored axe-core load; site runs isolated anyway
        ctx.on("page", lambda p: p.close() if p.opener() is not None else None)   # pop-ups are closed at once
        ctx.set_default_timeout(self.limits.page_timeout_s * 1000)     # no browser call waits longer than a page load

        def only_reads(route):          # nothing is ever sent to the site: no form posts, beacons or uploads,
            if route.request.method in ("GET", "HEAD"):     # including those a page script starts on its own
                route.continue_()
            else:
                res.refused_writes += 1
                route.abort("blockedbyclient")
        ctx.route("**/*", only_reads)
        try:
            queue, seen = deque([_norm(start_url)]), {_norm(start_url)}
            while queue and len(res.pages) < self.limits.max_pages:
                url = queue.popleft()
                if not rp.can_fetch(USER_AGENT_TOKEN, url):
                    res.skipped.append((url, "robots.txt"))
                    continue
                page_res, links = self._visit(ctx, url, res, site_dir, first=not res.pages)
                res.pages.append(page_res)
                if self._dead():
                    page_res.error = (f"the page did not respond within {self.limits.page_budget_s:.0f} s "
                                      "(possibly a script that never finishes); the crawl stopped here")
                    break
                for link in links:
                    n = _norm(link)
                    if n in seen:
                        continue
                    seen.add(n)
                    if urlsplit(n).scheme not in ("http", "https"):
                        continue
                    if not _same_site(n, start_url):
                        res.skipped.append((n, "other site"))
                    elif _SKIP_EXT.search(urlsplit(n).path):
                        res.skipped.append((n, "not a web page"))
                    else:
                        queue.append(n)
            stopped = self._dead()
            res.skipped += [(u, "crawl stopped" if stopped else "page limit") for u in queue]
            res.state = "crawled"
            if stopped:
                res.reason = "a page stopped responding; the results come from the pages loaded before it"
        except (UnlockedWrite, LockLost):
            raise                                       # writing without the maintenance lock is never tolerated
        except Exception as exc:  # noqa: BLE001 - one site's failure never stops a batch
            res.state, res.reason = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if not self._dead():                 # after the watchdog, any new browser call would wait forever
                ctx.close()
            res.finished_at = time.time()
        return res

    def _visit(self, ctx, url: str, res: CrawlResult, site_dir: Path, first: bool) -> tuple[PageResult, list[str]]:
        pr = PageResult(url=url)
        self._wait_turn(url, self._interval(res))
        guard = self.watchdog.guard(self.limits.page_budget_s) if self.watchdog else nullcontext()
        with guard:
            return self._visit_page(ctx, pr, url, res, site_dir, first)

    def _visit_page(self, ctx, pr: PageResult, url: str, res: CrawlResult, site_dir: Path,
                    first: bool) -> tuple[PageResult, list[str]]:
        page = ctx.new_page()
        t0 = time.monotonic()
        links: list[str] = []
        try:
            resp = page.goto(url, wait_until="load", timeout=self.limits.page_timeout_s * 1000)
            pr.status = resp.status if resp else None
            try:      # let an immediate script / meta-refresh navigation finish before reading: reading a page while it
                page.wait_for_timeout(250)                   # swaps renderer can hang (the watchdog is the backstop)
                page.wait_for_load_state("load")
            except Exception:  # noqa: BLE001 - still navigating: the reads below fail cleanly or the watchdog stops them
                if self._dead():
                    raise
            pr.final_url = page.url
            html = page.content()
            raw = html.encode("utf-8", "replace")
            pr.html_bytes, pr.html_truncated = min(len(raw), self.limits.max_html_bytes), len(raw) > self.limits.max_html_bytes
            body_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""   # no auto-wait
            if body_text.startswith(PROXY_REFUSAL):      # also after a page script or meta refresh moved the page
                pr.blocked_reason = body_text[len(PROXY_REFUSAL):].strip()
                return pr, []
            if pr.status is not None and pr.status >= 400:      # an error page is not website content
                pr.error = f"HTTP {pr.status}"
                return pr, []
            pr.title = (page.title() or "")[:300]
            pr.text_sha256 = hashlib.sha256(" ".join(body_text.split()).encode()).hexdigest()
            pr.facts = page.evaluate(FACTS_JS)
            if first:
                try:
                    res.perf_home = page.evaluate(PERF_JS)
                except Exception:  # noqa: BLE001 - timing is optional
                    if self._dead():
                        raise
                    res.perf_home = None
            if sum(1 for p in res.pages if p.axe is not None) < AXE_MAX_PAGES:
                try:
                    page.add_script_tag(path=str(AXE_PATH))
                    pr.axe = page.evaluate(AXE_RUN_JS)
                except Exception:  # noqa: BLE001 - accessibility rules are optional per page
                    if self._dead():
                        raise
                    pr.axe = None
            if pr.final_url and _same_site(pr.final_url, res.start_url):
                links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")[:500]
            pr.links = len(links)
            if self.limits.screenshots:
                pr.screenshot_desktop = self._save(pr, site_dir, "screenshot_desktop", f"page-{len(res.pages) + 1:02d}-desktop.jpg",
                                                   page.screenshot(type="jpeg", quality=70))
            if first and pr.final_url and _same_site(pr.final_url, res.start_url):
                page.set_viewport_size({"width": self.limits.mobile[0], "height": self.limits.mobile[1]})
                page.wait_for_timeout(150)                       # let the layout settle at phone width
                res.mobile_home_facts = page.evaluate(FACTS_JS)
                if self.limits.screenshots:
                    pr.screenshot_mobile = self._save(pr, site_dir, "screenshot_mobile", f"page-{len(res.pages) + 1:02d}-mobile.jpg",
                                                      page.screenshot(type="jpeg", quality=70))
        except (UnlockedWrite, LockLost):
            raise
        except Exception as exc:  # noqa: BLE001 - navigation errors are page results, not crashes
            msg = str(exc).split("\n")[0]
            if "ERR_TUNNEL_CONNECTION_FAILED" in msg or "ERR_PROXY" in msg:
                pr.blocked_reason = "refused by the crawler proxy"
            else:
                pr.error = msg[:300]
        finally:
            pr.elapsed_ms = int((time.monotonic() - t0) * 1000)
            if not self._dead():
                page.close()
        return pr, links

    def _dead(self) -> bool:
        """True once the watchdog stopped the browser driver: Playwright then hangs on every NEW call."""
        return bool(self.watchdog and self.watchdog.fired)
