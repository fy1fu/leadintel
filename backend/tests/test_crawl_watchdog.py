"""Crawler watchdog (M3): a page script that never yields freezes every browser call (Playwright's own timeouts do not
cover that); the watchdog kills the driver and the crawl ends with the pages loaded before. Own module: it needs its
own Playwright instance, which cannot coexist with another one in the same thread."""
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")

from app.crawl.runner import Watchdog, driver_killer  # noqa: E402
from app.net.browser_proxy import BrowserEgressProxy  # noqa: E402
from test_website_fixtures import TODAY, Server, crawler_for, page  # noqa: E402


@pytest.fixture
def private():
    s = Server("127.0.0.1", {"/": page("private", "SECRET")})
    yield s
    s.close()


def test_watchdog_stops_a_page_that_never_yields(tmp_path, private):
    """An endless loop after load freezes every browser call; the watchdog ends the crawl with what was loaded."""
    s = Server("127.0.0.2", {"/": page("Home", '<h1>Home</h1><p>fine</p><a href="/hang">Hang</a>'),
                             "/hang": page("Hang", "<h1>H</h1><script>addEventListener('load', () => "
                                                   "setTimeout(() => { while (true) {} }, 10))</script>")})
    cm = pw.sync_playwright()
    p = cm.start()
    t0 = time.monotonic()
    try:
        kill = driver_killer(cm)
        assert kill is not None, "Playwright internals moved: update driver_killer"
        browser = p.chromium.launch(args=BrowserEgressProxy.chromium_args())
        wd = Watchdog(kill)
        crawler, proxy = crawler_for(tmp_path, browser, {s.port}, watchdog=wd, page_budget_s=8)
        try:
            res = crawler.crawl(s.origin + "/", "hang")
        finally:
            proxy.stop()
    finally:
        try:
            p.stop()
        except Exception:  # noqa: BLE001 - the driver is gone on purpose
            pass
        s.close()
    assert wd.fired and time.monotonic() - t0 < 40
    assert res.state == "crawled" and "stopped responding" in res.reason
    assert res.pages[0].facts and res.pages[1].facts is None and "did not respond" in res.pages[1].error
    assert {c.key for c in res.criteria(today=TODAY)}               # the home page is still assessed
