"""Measured and heuristic website checks (M3 step 4, first slice; research/M3_PREP.md §3).

``FACTS_JS`` runs inside each crawled page (read-only DOM inspection; it changes nothing and sends nothing) and
returns raw facts. ``evaluate_site`` turns the facts of the crawled pages into criteria for the WQS dimensions.

Every criterion is ``measured`` (an objective property of the page) or ``heuristic`` (a weaker indicator), carries
its evidence, and is ``None`` when it could not be assessed - which never counts as a failure ("unknown is not
poor"). WQS itself (weights, confidence, calibration) is a later step; this module only produces the inputs.

Not yet included: axe-core accessibility rules (to be vendored, MPL-2.0), performance timings, Wayback/RDAP history.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass

FACTS_JS = r"""() => {
  const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  const meta = n => (q(`meta[name="${n}" i]`) || {}).content || null;
  const text = (document.body && document.body.innerText) || "";
  const ld = [];
  for (const s of qa('script[type="application/ld+json"]')) {
    try { const j = JSON.parse(s.textContent); for (const o of [].concat(j['@graph'] || j)) ld.push([].concat(o['@type'] || []).join(',')); }
    catch (e) { ld.push('invalid'); }
  }
  const years = [...text.matchAll(/(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})/gi)].map(m => +m[1]);
  const forms = qa('form');
  const fields = qa('form input:not([type=hidden]):not([type=submit]):not([type=button]), form textarea, form select');
  const unlabeled = fields.filter(f => !(f.labels && f.labels.length) && !f.getAttribute('aria-label') && !f.getAttribute('aria-labelledby') && !f.getAttribute('title'));
  const insecure = performance.getEntriesByType('resource').map(e => e.name).filter(u => u.startsWith('http:'));
  const tables = qa('table');
  const layoutTables = tables.filter(t => !t.querySelector('th') && (t.querySelector('table') || t.offsetWidth > 0.8 * window.innerWidth));
  return {
    url: location.href, protocol: location.protocol, lang: document.documentElement.getAttribute('lang'),
    title: document.title || null, meta_description: meta('description'), viewport: meta('viewport'),
    generator: meta('generator'), h1: qa('h1').length, canonical: (q('link[rel=canonical]') || {}).href || null,
    ld_types: ld, tel_links: qa('a[href^="tel:" i]').length, mailto_links: qa('a[href^="mailto:" i]').length,
    forms: forms.length, form_fields: fields.length, unlabeled_fields: unlabeled.length,
    images: qa('img').length, images_without_alt: qa('img:not([alt])').length,
    copyright_year: years.length ? Math.max(...years) : null,
    jquery: (window.jQuery && window.jQuery.fn && window.jQuery.fn.jquery) || null,
    insecure_resources: insecure.slice(0, 20), layout_tables: layoutTables.length,
    flash: qa('object[type*="flash" i], embed[src$=".swf" i], object[data$=".swf" i]').length,
    words: (text.match(/\S+/g) || []).length,
    overflow_x: document.documentElement.scrollWidth > window.innerWidth + 1,
    viewport_width: window.innerWidth,
  };
}"""

DIMENSIONS = ("technical", "mobile", "seo", "effectiveness", "content", "accessibility", "modernity")


@dataclass
class Criterion:
    key: str
    dimension: str
    kind: str                 # measured | heuristic
    passed: bool | None       # None = could not be assessed (never counts as poor)
    evidence: str


def _c(key, dim, kind, passed, evidence) -> Criterion:
    return Criterion(key, dim, kind, passed, evidence)


def _version_lt(v: str, major: int) -> bool | None:
    m = re.match(r"(\d+)", v or "")
    return None if not m else int(m.group(1)) < major


def evaluate_site(pages: list[dict], mobile_home: dict | None, *, today: _dt.date | None = None) -> list[Criterion]:
    """pages: FACTS_JS results of the crawled pages (home first); mobile_home: FACTS_JS of the home page at phone width."""
    today = today or _dt.date.today()
    out: list[Criterion] = []
    if not pages:
        return out
    home = pages[0]
    # technical
    https = home.get("protocol") == "https:"
    out.append(_c("https", "technical", "measured", https, f"home page served over {home.get('protocol', '?')[:-1]}"))
    insecure = sorted({u for p in pages for u in (p.get("insecure_resources") or [])})
    out.append(_c("no_mixed_content", "technical", "measured", (not insecure) if https else None,
                  f"{len(insecure)} resources loaded over http" + (f": {insecure[0]}" if insecure else "")
                  if https else "not applicable without https"))
    # mobile
    vp = home.get("viewport") or ""
    out.append(_c("viewport_meta", "mobile", "measured", "width=device-width" in vp.replace(" ", ""),
                  f"viewport meta: {vp or 'missing'}"))
    if mobile_home is None:
        out.append(_c("no_horizontal_scroll_on_phone", "mobile", "measured", None, "no phone-width check ran"))
    else:
        out.append(_c("no_horizontal_scroll_on_phone", "mobile", "measured", not mobile_home.get("overflow_x"),
                      f"page {'is wider than' if mobile_home.get('overflow_x') else 'fits'} a {mobile_home.get('viewport_width')} px screen"))
    # seo
    titles = [p.get("title") for p in pages if p.get("title")]
    out.append(_c("titles_present", "seo", "measured", len(titles) == len(pages),
                  f"{len(titles)} of {len(pages)} pages have a title"))
    out.append(_c("titles_unique", "seo", "measured", len(set(titles)) == len(titles) if len(titles) > 1 else None,
                  f"{len(set(titles))} different titles on {len(titles)} pages"))
    md = home.get("meta_description") or ""
    out.append(_c("meta_description", "seo", "measured", 50 <= len(md) <= 300, f"home meta description: {len(md)} characters"))
    out.append(_c("single_h1", "seo", "measured", home.get("h1") == 1, f"home page has {home.get('h1')} h1 headings"))
    types = {t for p in pages for t in p.get("ld_types") or []}
    local = any(re.search(r"LocalBusiness|Organization|HomeAndConstructionBusiness|Service|Store", t) for t in types)
    out.append(_c("business_structured_data", "seo", "measured", local,
                  "structured data: " + (", ".join(sorted(types)) if types else "none")))
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
    # accessibility (basic, until axe-core is added)
    imgs = sum(p.get("images") or 0 for p in pages)
    no_alt = sum(p.get("images_without_alt") or 0 for p in pages)
    out.append(_c("image_alt_text", "accessibility", "measured", (no_alt == 0) if imgs else None,
                  f"{no_alt} of {imgs} images without alt text" if imgs else "no images"))
    out.append(_c("page_language", "accessibility", "measured", bool(home.get("lang")), f"lang: {home.get('lang') or 'missing'}"))
    fields = sum(p.get("form_fields") or 0 for p in pages)
    unl = sum(p.get("unlabeled_fields") or 0 for p in pages)
    out.append(_c("form_labels", "accessibility", "measured", (unl == 0) if fields else None,
                  f"{unl} of {fields} form fields without a label" if fields else "no form fields"))
    # modernity / maintenance
    jq = next((p.get("jquery") for p in pages if p.get("jquery")), None)
    out.append(_c("jquery_current", "modernity", "measured", (not _version_lt(jq, 3)) if jq else None,
                  f"jQuery {jq}" if jq else "jQuery not used"))
    gen = home.get("generator") or ""
    wp = re.search(r"WordPress\s+(\d+(?:\.\d+)*)", gen)
    out.append(_c("cms_current", "modernity", "heuristic", (not _version_lt(wp.group(1), 6)) if wp else None,
                  f"generator: {gen}" if gen else "no generator tag"))
    layout = sum(p.get("layout_tables") or 0 for p in pages)
    flash = sum(p.get("flash") or 0 for p in pages)
    out.append(_c("modern_layout", "modernity", "heuristic", layout == 0 and flash == 0,
                  f"{layout} layout tables, {flash} Flash objects"))
    year = home.get("copyright_year")
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
