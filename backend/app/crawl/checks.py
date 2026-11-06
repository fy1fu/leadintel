"""Measured and heuristic website checks (M3 step 4; research/M3_PREP.md §3, SCORING.md §5).

Inputs per crawled page (all read-only - nothing on the page is clicked, typed or submitted):
* ``FACTS_JS``  - DOM facts (titles, headings, structured data, links, forms, images, versions, layout, ...);
* ``PERF_JS``   - the browser's own timing APIs for this load (navigation timing, resources, LCP, CLS). This is a
  LAB measurement on the user's PC and network, not Lighthouse: it feeds coarse bands only;
* axe-core (vendored, MPL-2.0) accessibility rules, WCAG 2 A/AA, on up to ``AXE_MAX_PAGES`` pages.

``evaluate_site`` turns them into criteria for the seven WQS dimensions of SCORING.md §5. Each criterion is
``measured`` or ``heuristic``, has evidence and a source, and is ``None`` when it could not be assessed - which
never counts as a failure ("unknown is not poor"). ``visual`` is subjective: with the ``none`` analysis provider
it is always "not assessed".
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass
from pathlib import Path

AXE_VERSION = "4.14.0"
AXE_PATH = Path(__file__).parent / "vendor" / "axe-core" / "axe.min.js"
AXE_MAX_PAGES = 5
DIMENSIONS = ("visual", "ux", "effectiveness", "technical", "content", "seo", "modernity")
WEIGHT = {"measured": 1.0, "heuristic": 0.5}

FACTS_JS = r"""() => {
  const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  const meta = n => (q(`meta[name="${n}" i]`) || {}).content || null;
  const text = (document.body && document.body.innerText) || "";
  const ld = [];
  for (const s of qa('script[type="application/ld+json"]')) {
    try { const j = JSON.parse(s.textContent); for (const o of [].concat(j['@graph'] || j)) ld.push([].concat((o || {})['@type'] || []).join(',')); }
    catch (e) { ld.push('invalid'); }
  }
  const years = [...text.matchAll(/(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})/gi)].map(m => +m[1]).filter(y => y > 1990 && y < 2100);
  const fields = qa('form input:not([type=hidden]):not([type=submit]):not([type=button]), form textarea, form select');
  const unlabeled = fields.filter(f => !(f.labels && f.labels.length) && !f.getAttribute('aria-label') && !f.getAttribute('aria-labelledby') && !f.getAttribute('title'));
  const insecure = performance.getEntriesByType('resource').map(e => e.name).filter(u => u.startsWith('http:'));
  const layoutTables = qa('table').filter(t => !t.querySelector('th') && (t.querySelector('table') || t.offsetWidth > 0.8 * window.innerWidth));
  const hrefs = qa('a[href]').map(a => a.getAttribute('href') || '');
  return {
    url: location.href, protocol: location.protocol, lang: document.documentElement.getAttribute('lang'),
    title: (document.title || '').slice(0, 400) || null, meta_description: (meta('description') || '').slice(0, 600) || null,
    viewport: meta('viewport'), generator: (meta('generator') || '').slice(0, 120) || null, h1: qa('h1').length,
    ld_types: ld.slice(0, 20), tel_links: qa('a[href^="tel:" i]').length, mailto_links: qa('a[href^="mailto:" i]').length,
    forms: qa('form').length, form_fields: fields.length, unlabeled_fields: unlabeled.length,
    images: qa('img').length, images_without_alt: qa('img:not([alt])').length,
    copyright_year: years.length ? Math.max(...years) : null,
    jquery: (window.jQuery && window.jQuery.fn && typeof window.jQuery.fn.jquery === 'string' && window.jQuery.fn.jquery.slice(0, 20)) || null,
    insecure_resources: insecure.slice(0, 20), layout_tables: layoutTables.length,
    flash: qa('object[type*="flash" i], embed[src$=".swf" i], object[data$=".swf" i]').length,
    words: (text.match(/\S+/g) || []).length, empty_links: hrefs.filter(h => !h.trim() || h.trim() === '#').length,
    script_links: hrefs.filter(h => /^\s*javascript:/i.test(h)).length,
    overflow_x: document.documentElement.scrollWidth > window.innerWidth + 1, viewport_width: window.innerWidth,
  };
}"""

PERF_JS = r"""() => new Promise(resolve => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const res = performance.getEntriesByType('resource');
  const out = {dom_content_loaded_ms: nav.domContentLoadedEventEnd ? Math.round(nav.domContentLoadedEventEnd) : null,
               load_ms: nav.loadEventEnd ? Math.round(nav.loadEventEnd) : null,
               requests: res.length + 1,
               bytes: Math.round((nav.transferSize || 0) + res.reduce((s, r) => s + (r.transferSize || 0), 0)),
               lcp_ms: null, cls: null};
  let pending = 2;
  const done = () => { if (--pending === 0) resolve(out); };
  try { new PerformanceObserver(l => { const e = l.getEntries(); if (e.length) out.lcp_ms = Math.round(e[e.length - 1].startTime); })
          .observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { out.cls = (out.cls || 0) + l.getEntries().filter(x => !x.hadRecentInput).reduce((s, x) => s + x.value, 0); })
          .observe({type: 'layout-shift', buffered: true}); out.cls = out.cls || 0; } catch (e) {}
  setTimeout(done, 50); setTimeout(done, 50);
})"""

AXE_RUN_JS = r"""async () => {
  const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']}, resultTypes: ['violations']});
  return r.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.length, help: (v.help || '').slice(0, 200)}));
}"""


@dataclass
class Criterion:
    key: str
    dimension: str
    kind: str                 # measured | heuristic
    passed: bool | None       # None = could not be assessed (never counts as poor)
    evidence: str
    source: str = "page DOM"
    weight: float = 1.0


def _c(key, dim, kind, passed, evidence, source="page DOM") -> Criterion:
    return Criterion(key, dim, kind, passed, evidence, source, WEIGHT[kind])


def _version_lt(v: str, major: int) -> bool | None:
    m = re.match(r"(\d+)", v or "")
    return None if not m else int(m.group(1)) < major


def evaluate_site(pages: list[dict], mobile_home: dict | None, *, today: _dt.date | None = None,
                  perf_home: dict | None = None, axe: list[list[dict]] | None = None,
                  broken_pages: list[str] | None = None) -> list[Criterion]:
    """pages: FACTS_JS of the crawled pages (home first); mobile_home: FACTS_JS of the home page at phone width;
    perf_home: PERF_JS of the home page; axe: axe-core violations per audited page (None = axe did not run)."""
    today = today or _dt.date.today()
    out: list[Criterion] = []
    if not pages:
        return out
    home = pages[0]
    # visual: subjective; the none provider does not judge it
    out.append(_c("visual_design", "visual", "heuristic", None,
                  "visual design is a subjective judgement; not assessed without an analysis provider", "none provider"))
    # technical
    https = home.get("protocol") == "https:"
    out.append(_c("https", "technical", "measured", https, f"home page served over {(home.get('protocol') or '?:')[:-1]}"))
    insecure = sorted({u for p in pages for u in (p.get("insecure_resources") or [])})
    out.append(_c("no_mixed_content", "technical", "measured", (not insecure) if https else None,
                  (f"{len(insecure)} resources loaded over http" + (f": {insecure[0]}" if insecure else ""))
                  if https else "not applicable without https"))
    if perf_home:
        b, n, lcp, load = perf_home.get("bytes"), perf_home.get("requests"), perf_home.get("lcp_ms"), perf_home.get("load_ms")
        lab = "browser timing on this PC (lab, not Lighthouse)"
        out.append(_c("page_weight", "technical", "measured", (b <= 3_000_000) if b else None,
                      f"home page transferred {b / 1_000_000:.1f} MB in {n} requests" if b else "transfer size not reported", lab))
        out.append(_c("largest_content_paint", "technical", "heuristic", (lcp <= 4000) if lcp else None,
                      f"largest content painted after {lcp / 1000:.1f} s" if lcp else "not reported by the browser", lab))
        out.append(_c("load_time", "technical", "heuristic", (load <= 8000) if load else None,
                      f"page load finished after {load / 1000:.1f} s" if load else "not reported by the browser", lab))
    # ux (mobile + accessibility)
    vp = home.get("viewport") or ""
    out.append(_c("viewport_meta", "ux", "measured", "width=device-width" in vp.replace(" ", ""), f"viewport meta: {vp or 'missing'}"))
    if mobile_home is None:
        out.append(_c("no_horizontal_scroll_on_phone", "ux", "measured", None, "no phone-width check ran"))
    else:
        out.append(_c("no_horizontal_scroll_on_phone", "ux", "measured", not mobile_home.get("overflow_x"),
                      f"page {'is wider than' if mobile_home.get('overflow_x') else 'fits'} a {mobile_home.get('viewport_width')} px screen"))
    imgs = sum(p.get("images") or 0 for p in pages)
    no_alt = sum(p.get("images_without_alt") or 0 for p in pages)
    out.append(_c("image_alt_text", "ux", "measured", (no_alt == 0) if imgs else None,
                  f"{no_alt} of {imgs} images without alt text" if imgs else "no images"))
    out.append(_c("page_language", "ux", "measured", bool(home.get("lang")), f"lang: {home.get('lang') or 'missing'}"))
    fields = sum(p.get("form_fields") or 0 for p in pages)
    unl = sum(p.get("unlabeled_fields") or 0 for p in pages)
    out.append(_c("form_labels", "ux", "measured", (unl == 0) if fields else None,
                  f"{unl} of {fields} form fields without a label" if fields else "no form fields"))
    broken = sum((p.get("empty_links") or 0) + (p.get("script_links") or 0) for p in pages)
    dead = broken_pages or []
    out.append(_c("working_links", "ux", "heuristic", broken == 0 and not dead,
                  f"{broken} empty or javascript: links; {len(dead)} linked pages return an error"
                  + (f" (e.g. {dead[0][:200]})" if dead else "")))
    if axe is None or not any(a is not None for a in axe):
        out.append(_c("accessibility_rules", "ux", "measured", None, "accessibility rules did not run", f"axe-core {AXE_VERSION}"))
    else:
        ran = [a for a in axe if a is not None]
        severe = sorted({v["id"] for a in ran for v in a if v.get("impact") in ("critical", "serious")})
        minor = sorted({v["id"] for a in ran for v in a if v.get("impact") not in ("critical", "serious")})
        out.append(_c("accessibility_rules", "ux", "measured", not severe,
                      (f"{len(severe)} serious or critical WCAG issues ({', '.join(severe[:5])})" if severe else
                       "no serious or critical WCAG issues") + (f"; {len(minor)} minor" if minor else "") +
                      f" on {len(ran)} pages", f"axe-core {AXE_VERSION}"))
    # seo
    titles = [p.get("title") for p in pages if p.get("title")]
    out.append(_c("titles_present", "seo", "measured", len(titles) == len(pages), f"{len(titles)} of {len(pages)} pages have a title"))
    out.append(_c("titles_unique", "seo", "measured", len(set(titles)) == len(titles) if len(titles) > 1 else None,
                  f"{len(set(titles))} different titles on {len(titles)} pages"))
    md = home.get("meta_description") or ""
    out.append(_c("meta_description", "seo", "measured", 50 <= len(md) <= 300, f"home meta description: {len(md)} characters"))
    out.append(_c("single_h1", "seo", "measured", home.get("h1") == 1, f"home page has {home.get('h1')} h1 headings"))
    types = {t for p in pages for t in p.get("ld_types") or []}
    local = any(re.search(r"LocalBusiness|Organization|HomeAndConstructionBusiness|Service|Store", t) for t in types)
    out.append(_c("business_structured_data", "seo", "measured", local, "structured data: " + (", ".join(sorted(types)) if types else "none")))
    # effectiveness
    out.append(_c("click_to_call", "effectiveness", "measured", any((p.get("tel_links") or 0) > 0 for p in pages),
                  f"tel: links on {sum(1 for p in pages if (p.get('tel_links') or 0) > 0)} pages"))
    out.append(_c("contact_form_or_email", "effectiveness", "measured",
                  any((p.get("forms") or 0) > 0 or (p.get("mailto_links") or 0) > 0 for p in pages),
                  f"forms on {sum(1 for p in pages if (p.get('forms') or 0) > 0)} pages, mailto links on "
                  f"{sum(1 for p in pages if (p.get('mailto_links') or 0) > 0)}"))
    # content
    words = sum(p.get("words") or 0 for p in pages)
    out.append(_c("enough_text", "content", "heuristic", words >= 300, f"{words} words across {len(pages)} pages"))
    out.append(_c("more_than_one_page", "content", "heuristic", len(pages) > 1, f"{len(pages)} pages reachable from the home page"))
    # modernity
    jq = next((p.get("jquery") for p in pages if p.get("jquery")), None)
    out.append(_c("jquery_current", "modernity", "measured", (not _version_lt(jq, 3)) if jq else None,
                  f"jQuery {jq}" if jq else "jQuery not used"))
    gen = home.get("generator") or ""
    wp = re.search(r"WordPress\s+(\d+(?:\.\d+)*)", gen)
    out.append(_c("cms_current", "modernity", "heuristic", (not _version_lt(wp.group(1), 6)) if wp else None,
                  f"generator: {gen}" if gen else "no generator tag"))
    layout = sum(p.get("layout_tables") or 0 for p in pages)
    flash = sum(p.get("flash") or 0 for p in pages)
    out.append(_c("modern_layout", "modernity", "heuristic", layout == 0 and flash == 0, f"{layout} layout tables, {flash} Flash objects"))
    year = home.get("copyright_year")
    if year and year > today.year + 1:          # a year in the future says nothing about upkeep
        out.append(_c("recent_copyright_year", "modernity", "heuristic", None, f"copyright year {year} is in the future"))
    else:
        out.append(_c("recent_copyright_year", "modernity", "heuristic", (year >= today.year - 2) if year else None,
                      f"copyright year {year}" if year else "no copyright year shown"))
    return out


def summarize(criteria: list[Criterion]) -> dict:
    """Per dimension: passed / assessed / not assessed (inputs for WQS; not a score)."""
    dims = {d: {"passed": 0, "assessed": 0, "not_assessed": 0} for d in DIMENSIONS}
    for c in criteria:
        d = dims[c.dimension]
        if c.passed is None:
            d["not_assessed"] += 1
        else:
            d["assessed"] += 1
            d["passed"] += int(c.passed)
    return {k: v for k, v in dims.items() if v["assessed"] or v["not_assessed"]}


def as_dicts(criteria: list[Criterion]) -> list[dict]:
    return [asdict(c) for c in criteria]
