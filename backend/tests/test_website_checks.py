"""Website checks (M3 step 4): fixture sites with planted defects, crawled with a real browser through the proxy.
Each check must report exactly what was planted; what cannot be assessed is None (never a failure)."""
import datetime as dt

import pytest

from app.crawl.checks import DIMENSIONS, evaluate_site, summarize

TODAY = dt.date(2026, 11, 6)


# ---------------------------------------------------------------- pure evaluation (offline)
def test_unknown_is_never_counted_as_poor():
    facts = {"protocol": "http:", "title": "Only page", "h1": 1, "words": 10}      # almost nothing known
    crit = {c.key: c for c in evaluate_site([facts], None, today=TODAY)}
    for key in ("no_mixed_content", "no_horizontal_scroll_on_phone", "titles_unique", "image_alt_text", "form_labels",
                "jquery_current", "cms_current", "recent_copyright_year"):
        assert crit[key].passed is None, key
    s = summarize(list(crit.values()))
    assert set(s) <= set(DIMENSIONS) and all(v["assessed"] + v["not_assessed"] > 0 for v in s.values())
    assert evaluate_site([], None) == []


def test_https_with_mixed_content_and_versions():
    facts = {"protocol": "https:", "insecure_resources": ["http://cdn.example/x.js"], "jquery": "3.7.1",
             "generator": "WordPress 6.6.2", "copyright_year": 2025, "title": "t"}
    crit = {c.key: c for c in evaluate_site([facts], {"overflow_x": False, "viewport_width": 390}, today=TODAY)}
    assert crit["https"].passed is True and crit["no_mixed_content"].passed is False
    assert "http://cdn.example/x.js" in crit["no_mixed_content"].evidence
    assert crit["jquery_current"].passed is True and crit["cms_current"].passed is True
    assert crit["recent_copyright_year"].passed is True and crit["no_horizontal_scroll_on_phone"].passed is True


# ---------------------------------------------------------------- fixture sites through the browser
pw = pytest.importorskip("playwright.sync_api")
from app.net.browser_proxy import BrowserEgressProxy  # noqa: E402
from test_crawl_runner import Site, html, make_crawler  # noqa: E402

LOREM = " ".join(["Reliable lawn care for Jacksonville homes, with weekly mowing, edging and seasonal clean-ups."] * 25)
GOOD = {
    "/": f"""<!doctype html><html lang="en"><head><title>Acme Lawn Care | Jacksonville</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta name="description" content="Family-run lawn care in Jacksonville since 1999: mowing, edging and seasonal clean-ups.">
      <script type="application/ld+json">{{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme"}}</script>
      </head><body><h1>Acme Lawn Care</h1><p>{LOREM}</p><img src="/logo.png" alt="Acme logo">
      <a href="tel:+19045550100">Call (904) 555-0100</a> <a href="/contact">Contact</a> <a href="/about">About</a>
      <footer>© 2026 Acme Lawn Care</footer></body></html>""",
    "/contact": """<!doctype html><html lang="en"><head><title>Contact Acme</title>
      <meta name="viewport" content="width=device-width"></head><body><h1>Contact</h1>
      <form><label for="n">Name</label><input id="n" name="n"><label>Message <textarea name="m"></textarea></label>
      <button>Send</button></form></body></html>""",
    "/about": '<!doctype html><html lang="en"><head><title>About Acme</title></head><body><h1>About</h1><p>Since 1999.</p></body></html>',
}
BAD = {
    "/": """<!doctype html><html><head><title>Home</title><meta name="generator" content="WordPress 4.9.8">
      <script>window.jQuery = {fn: {jquery: "1.12.4"}};</script></head><body>
      <h2>Welcome</h2><div style="width:1200px">Wide fixed layout</div><img src="/pic.jpg">
      <table width="100%"><tr><td><table><tr><td>nested layout</td></tr></table></td></tr></table>
      <a href="/page2">More</a> <footer>Copyright 2014 Old Site</footer></body></html>""",
    "/page2": """<!doctype html><html><head><title>Home</title></head><body><h1>A</h1><h1>B</h1>
      <form><input name="email" placeholder="email"></form></body></html>""",
}
GOOD_EXPECT = {"https": False, "no_mixed_content": None, "viewport_meta": True, "no_horizontal_scroll_on_phone": True,
               "titles_present": True, "titles_unique": True, "meta_description": True, "single_h1": True,
               "business_structured_data": True, "click_to_call": True, "contact_form_or_email": True,
               "enough_text": True, "image_alt_text": True, "page_language": True, "form_labels": True,
               "jquery_current": None, "cms_current": None, "modern_layout": True, "recent_copyright_year": True,
               "visual_design": None, "page_weight": True, "largest_content_paint": True, "load_time": True,
               "working_links": True, "accessibility_rules": True, "more_than_one_page": True}
BAD_EXPECT = {"https": False, "no_mixed_content": None, "viewport_meta": False, "no_horizontal_scroll_on_phone": False,
              "titles_present": True, "titles_unique": False, "meta_description": False, "single_h1": False,
              "business_structured_data": False, "click_to_call": False, "contact_form_or_email": True,
              "enough_text": False, "image_alt_text": False, "page_language": False, "form_labels": False,
              "jquery_current": False, "cms_current": False, "modern_layout": False, "recent_copyright_year": False,
              "visual_design": None, "page_weight": True, "largest_content_paint": True, "load_time": True,
              "working_links": True, "accessibility_rules": False, "more_than_one_page": True}


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=BrowserEgressProxy.chromium_args())
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium available: {exc}")
        yield b
        b.close()


@pytest.mark.parametrize("pages,expect", [(GOOD, GOOD_EXPECT), (BAD, BAD_EXPECT)], ids=["good-site", "bad-site"])
def test_fixture_sites_report_exactly_the_planted_defects(tmp_path, browser, pages, expect):
    s = Site("127.0.0.2", pages, robots=(404, ""))
    crawler, proxy = make_crawler(tmp_path, browser, {s.port}, screenshots=False)
    try:
        res = crawler.crawl(f"http://127.0.0.2:{s.port}/", "fixture")
    finally:
        proxy.stop(), s.close()
    assert res.state == "crawled" and len(res.pages) == len(pages)
    got = {c.key: c.passed for c in res.criteria(today=TODAY)}
    assert got == expect, {k: (got.get(k), v) for k, v in expect.items() if got.get(k) != v}
    for c in res.criteria(today=TODAY):
        assert c.evidence and c.kind in ("measured", "heuristic")


def test_a_site_that_could_not_be_crawled_has_no_criteria(tmp_path, browser):
    s = Site("127.0.0.2", {"/": html("x", "y")}, robots=(500, "down"))
    crawler, proxy = make_crawler(tmp_path, browser, {s.port}, screenshots=False)
    try:
        res = crawler.crawl(f"http://127.0.0.2:{s.port}/", "x")
    finally:
        proxy.stop(), s.close()
    assert res.state == "robots_unavailable" and res.criteria() == []        # unknown, never "poor"
