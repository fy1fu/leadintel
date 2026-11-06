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
* same site only, breadth-first, at most ``max_pages`` pages, at least ``per_host_interval_s`` between requests;
  logins, CAPTCHAs and bot walls are never worked around - they simply end up as what the page shows.

Results are returned as data (``CrawlResult``); storing them in the database comes with the M3 schema (0007).
Screenshots are written into the managed data folder and therefore need the maintenance lock, like Load Data.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from app.db.locks import LockLost, UnlockedWrite, ensure_write_allowed
from app.net.browser_proxy import BrowserEgressProxy
from app.net.safe_egress import BlockedDestination, SafeFetcher

USER_AGENT_TOKEN = "LeadIntelBot"
USER_AGENT = f"Mozilla/5.0 (compatible; {USER_AGENT_TOKEN}/0.1; local prospect research)"
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|ico|zip|rar|7z|gz|mp[34]|mov|avi|wmv|docx?|xlsx?|pptx?|exe|dmg|css|js|json|xml)$",
                       re.I)
PROXY_REFUSAL = "LeadIntel crawler proxy:"


@dataclass
class CrawlLimits:
    max_pages: int = 15
    per_host_interval_s: float = 1.0
    max_crawl_delay_s: float = 10.0
    page_timeout_s: float = 20.0
    max_html_bytes: int = 3_000_000
    screenshots: bool = True
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


def _norm(url: str) -> str:
    u, _ = urldefrag(url)
    return u


def _same_site(a: str, b: str) -> bool:
    ha, hb = (urlsplit(a).hostname or "").lower(), (urlsplit(b).hostname or "").lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h               # noqa: E731
    return strip(ha) == strip(hb) and urlsplit(a).port == urlsplit(b).port


class SiteCrawler:
    def __init__(self, *, fetcher: SafeFetcher, proxy: BrowserEgressProxy, browser, data_dir: Path,
                 limits: CrawlLimits | None = None, clock=time.monotonic, sleep=time.sleep):
        self.fetcher, self.proxy, self.browser = fetcher, proxy, browser
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
    def _save(self, site_dir: Path, name: str, data: bytes) -> str:
        if not site_dir.is_dir():
            ensure_write_allowed(site_dir)
            site_dir.mkdir(parents=True, exist_ok=True)
        path = site_dir / name
        ensure_write_allowed(path)
        path.write_bytes(data)
        return path.relative_to(self.data_dir).as_posix()

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
                                       permissions=[], ignore_https_errors=False)
        ctx.on("page", lambda p: p.close() if p.opener() is not None else None)   # pop-ups are closed at once
        try:
            queue, seen = deque([_norm(start_url)]), {_norm(start_url)}
            while queue and len(res.pages) < self.limits.max_pages:
                url = queue.popleft()
                if not rp.can_fetch(USER_AGENT_TOKEN, url):
                    res.skipped.append((url, "robots.txt"))
                    continue
                page_res, links = self._visit(ctx, url, res, site_dir, first=not res.pages)
                res.pages.append(page_res)
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
            res.skipped += [(u, "page limit") for u in queue]
            res.state = "crawled"
        except (UnlockedWrite, LockLost):
            raise                                       # writing without the maintenance lock is never tolerated
        except Exception as exc:  # noqa: BLE001 - one site's failure never stops a batch
            res.state, res.reason = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            ctx.close()
            res.finished_at = time.time()
        return res

    def _visit(self, ctx, url: str, res: CrawlResult, site_dir: Path, first: bool) -> tuple[PageResult, list[str]]:
        pr = PageResult(url=url)
        self._wait_turn(url, self._interval(res))
        page = ctx.new_page()
        t0 = time.monotonic()
        links: list[str] = []
        try:
            resp = page.goto(url, wait_until="load", timeout=self.limits.page_timeout_s * 1000)
            pr.status = resp.status if resp else None
            pr.final_url = page.url
            html = page.content()
            raw = html.encode("utf-8", "replace")
            pr.html_bytes, pr.html_truncated = min(len(raw), self.limits.max_html_bytes), len(raw) > self.limits.max_html_bytes
            body_text = page.inner_text("body") if page.query_selector("body") else ""
            if pr.status == 403 and body_text.startswith(PROXY_REFUSAL):
                pr.blocked_reason = body_text[len(PROXY_REFUSAL):].strip()
                return pr, []
            pr.title = (page.title() or "")[:300]
            pr.text_sha256 = hashlib.sha256(" ".join(body_text.split()).encode()).hexdigest()
            if pr.final_url and _same_site(pr.final_url, res.start_url):
                links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")[:500]
            pr.links = len(links)
            if self.limits.screenshots:
                pr.screenshot_desktop = self._save(site_dir, f"page-{len(res.pages) + 1:02d}-desktop.jpg",
                                                   page.screenshot(type="jpeg", quality=70))
                if first:
                    page.set_viewport_size({"width": self.limits.mobile[0], "height": self.limits.mobile[1]})
                    pr.screenshot_mobile = self._save(site_dir, f"page-{len(res.pages) + 1:02d}-mobile.jpg",
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
            page.close()
        return pr, links
