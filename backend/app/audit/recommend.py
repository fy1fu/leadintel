"""Recommendations from actual audit findings (M3). Only failed checks produce recommendations - never unknown ones.
No conversion or revenue promises: each item says what was found, the evidence, why it matters and a practical fix."""
from __future__ import annotations

from app.audit.benchmark import MIN_PEERS
from app.scoring.wqs import DIMENSION_WEIGHTS

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
# criterion -> (severity, title, why it matters, practical suggestion)
TEMPLATES = {
    "https": ("high", "Serve the website securely (https)",
              "Browsers label plain http pages \"Not secure\", which can make visitors hesitate to call or fill in a form.",
              "Turn on a TLS certificate (free from most hosts or Let's Encrypt) and redirect every http address to https."),
    "viewport_meta": ("high", "Make the website work on phones",
                      "Many local customers look up businesses on a phone; without a mobile viewport the page is shown shrunk and hard to use.",
                      "Use a responsive layout and add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">."),
    "no_horizontal_scroll_on_phone": ("high", "Stop the page from overflowing on phones",
                                      "Content wider than the screen forces sideways scrolling and hides parts of the page.",
                                      "Replace fixed-width sections with flexible widths so the page fits a phone screen."),
    "click_to_call": ("high", "Add tap-to-call phone links",
                      "On a phone, a tappable number is the quickest way for a customer to reach the business.",
                      "Link the phone number with tel: (for example <a href=\"tel:+19045550100\">) on every page."),
    "contact_form_or_email": ("medium", "Offer a simple way to get in touch online",
                              "Some customers prefer to write rather than call, especially outside business hours.",
                              "Add a short contact or quote-request form, or at least a clickable email address."),
    "accessibility_rules": ("medium", "Fix serious accessibility problems",
                            "Serious WCAG issues make the site hard or impossible to use for some visitors, including screen-reader users.",
                            "Fix the listed issues (for example missing image descriptions, page language, form labels)."),
    "no_mixed_content": ("medium", "Load every file securely",
                         "Files loaded over http on an https page can be blocked by browsers and weaken the secure connection.",
                         "Change the listed http:// resource addresses to https://."),
    "page_weight": ("medium", "Make the home page lighter",
                    "Heavy pages load slowly on mobile connections.",
                    "Compress and resize images and remove unused scripts."),
    "largest_content_paint": ("medium", "Show the main content sooner",
                              "When the main content appears late, visitors may leave before seeing it.",
                              "Optimise the largest image or text block at the top and defer non-essential scripts."),
    "load_time": ("medium", "Shorten the page load time",
                  "Slow loading frustrates visitors, especially on phones.",
                  "Reduce large files and third-party scripts; check the hosting speed."),
    "jquery_current": ("medium", "Update outdated website software",
                       "Old JavaScript libraries have known security problems and are no longer maintained.",
                       "Update jQuery (and plugins depending on it) to a supported 3.x version."),
    "cms_current": ("medium", "Update the website platform",
                    "An outdated content-management system misses security fixes.",
                    "Update WordPress and its plugins to supported versions, or move to maintained hosting."),
    "modern_layout": ("medium", "Modernise the page layout",
                      "Table-based or Flash layouts are outdated, do not adapt to phones and look old.",
                      "Rebuild the layout with modern responsive techniques."),
    "business_structured_data": ("medium", "Describe the business for search engines",
                                 "Structured business data helps search engines show the name, address, phone and hours correctly.",
                                 "Add schema.org LocalBusiness data (name, address, phone, opening hours, service area)."),
    "meta_description": ("medium", "Write a search-result description",
                         "Without a description, search engines pick random text for the result snippet.",
                         "Add a 50-160 character description of the business and its area to the home page."),
    "enough_text": ("medium", "Explain the services in more detail",
                    "Very little text gives customers and search engines little to go on.",
                    "Add a short page per main service with the area served and what is included."),
    "more_than_one_page": ("medium", "Add pages for services and contact",
                           "A single page cannot cover services, service area and contact details well.",
                           "Add separate service, about and contact pages linked from the home page."),
    "image_alt_text": ("low", "Describe images with alt text", "Screen readers and search engines cannot see images.",
                       "Give every meaningful image a short alt text."),
    "page_language": ("low", "Declare the page language", "Screen readers need it to pronounce the text correctly.",
                      "Add lang=\"en\" to the <html> element."),
    "form_labels": ("low", "Label the form fields", "Unlabelled fields are hard to use with assistive technology.",
                    "Give every form field a visible label."),
    "titles_unique": ("low", "Give each page its own title", "Identical titles make pages hard to tell apart in search results and tabs.",
                      "Write a distinct title per page, e.g. \"Lawn mowing in Jacksonville | Business name\"."),
    "titles_present": ("low", "Add page titles", "Pages without a title show the address in search results and browser tabs.",
                       "Add a descriptive <title> to every page."),
    "single_h1": ("low", "Use one main heading per page", "A single clear main heading helps visitors and search engines.",
                  "Use exactly one h1 that says what the page is about."),
    "working_links": ("low", "Fix empty links", "Links that go nowhere confuse visitors.", "Give each link a real destination or remove it."),
    "recent_copyright_year": ("low", "Show that the site is maintained", "An old copyright year suggests the site is not looked after.",
                              "Update the footer year and review the content for anything out of date."),
}


def build(criteria, benchmark: dict | None = None, limit: int = 5) -> list[dict]:
    peer = (benchmark or {}).get("criteria") or {}
    failed = [c for c in criteria if c.passed is False and c.key in TEMPLATES]
    failed.sort(key=lambda c: (SEVERITY_RANK[TEMPLATES[c.key][0]], -DIMENSION_WEIGHTS.get(c.dimension, 0), c.key))
    out = []
    for i, c in enumerate(failed[:limit], 1):
        severity, title, why, suggestion = TEMPLATES[c.key]
        bench = None
        p = peer.get(c.key)
        if p and p.get("assessed", 0) >= MIN_PEERS:          # the same minimum as the benchmark itself
            bench = (f"{p['passed']} of {p['assessed']} comparable businesses ({benchmark['description']}) pass this check.")
        out.append({"criterion": c.key, "severity": severity, "priority": i, "title": title,
                    "finding": f"Check \"{c.key.replace('_', ' ')}\" did not pass.", "evidence": c.evidence,
                    "why": why, "suggestion": suggestion, "benchmark": bench})
    return out
