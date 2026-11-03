"""Normalization of names, phones, emails, URLs and addresses. Pure functions; no I/O."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers
import usaddress

# ----------------------------------------------------------------------------- names
_LEGAL_SUFFIXES = {"llc", "l l c", "inc", "incorporated", "corp", "corporation", "co", "company", "pa", "p a", "pllc",
                   "ltd", "limited", "lp", "llp", "lllp", "pc", "dba", "the"}


def name_norm(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b([a-z])\.", r"\1", s)            # l.l.c. -> llc, p.a. -> pa
    s = s.replace("&", " and ").replace("@", " at ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t]
    # strip legal suffixes at the end (repeatedly), keep them elsewhere
    while toks and (toks[-1] in _LEGAL_SUFFIXES or " ".join(toks[-2:]) in _LEGAL_SUFFIXES):
        toks = toks[:-2] if " ".join(toks[-2:]) in _LEGAL_SUFFIXES and len(toks) >= 2 else toks[:-1]
    if toks and toks[0] == "the":
        toks = toks[1:]
    return " ".join(toks)


# ----------------------------------------------------------------------------- phones & emails
def phone_e164(raw: str | None, region: str = "US") -> str | None:
    if not raw or not str(raw).strip():
        return None
    try:
        n = phonenumbers.parse(str(raw), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(n):
        return None
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def email_norm(raw: str | None) -> str | None:
    if not raw:
        return None
    e = raw.strip().lower().removeprefix("mailto:")
    return e if _EMAIL.match(e) else None


# ----------------------------------------------------------------------------- URLs
SOCIAL = {"facebook.com": "facebook", "fb.com": "facebook", "fb.me": "facebook", "instagram.com": "instagram",
          "tiktok.com": "tiktok", "linkedin.com": "linkedin", "twitter.com": "x", "x.com": "x",
          "youtube.com": "youtube", "youtu.be": "youtube", "pinterest.com": "pinterest", "nextdoor.com": "nextdoor",
          "threads.net": "threads", "snapchat.com": "snapchat"}
LINK_HUB = {"linktr.ee": "linktree", "beacons.ai": "link_hub", "lnk.bio": "link_hub", "bio.link": "link_hub",
            "linkin.bio": "link_hub", "campsite.bio": "link_hub", "taplink.cc": "link_hub"}
LISTING = {"yelp.com": "yelp", "google.com": "google_listing", "g.page": "google_listing", "goo.gl": "google_listing",
           "maps.app.goo.gl": "google_listing", "business.site": "google_listing", "bbb.org": "bbb",
           "angi.com": "angi", "angieslist.com": "angi", "homeadvisor.com": "homeadvisor", "thumbtack.com": "thumbtack",
           "yellowpages.com": "yellowpages", "mapquest.com": "mapquest", "manta.com": "manta", "porch.com": "porch",
           "houzz.com": "houzz", "nextdoor.co": "nextdoor", "tripadvisor.com": "tripadvisor", "opentable.com": "opentable",
           "doordash.com": "doordash", "ubereats.com": "ubereats", "grubhub.com": "grubhub", "booksy.com": "booksy",
           "vagaro.com": "vagaro", "styleseat.com": "styleseat", "square.site": None}
BUILDER_SUBDOMAINS = ("wixsite.com", "squarespace.com", "godaddysites.com", "weebly.com", "wordpress.com",
                      "square.site", "webflow.io", "carrd.co", "site123.me", "jimdosite.com", "business.site")
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|y_source)")


@dataclass(frozen=True)
class UrlClass:
    url_norm: str
    platform: str
    platform_kind: str          # independent | social | link_hub | listing
    unbranded_domain: bool = False


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def classify_url(raw: str | None) -> UrlClass | None:
    """Classify a URL. Social, link-in-bio and listing URLs are never independent websites."""
    if not raw or not str(raw).strip():
        return None
    u = str(raw).strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "http://" + u
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host or parts.scheme.lower() not in ("http", "https"):
        return None
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and _host_matches(host, "facebook.com"):
        host = host[2:]
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/")
    norm = urlunsplit(("https", host, path, query, ""))
    for table, kind in ((SOCIAL, "social"), (LINK_HUB, "link_hub")):
        for dom, plat in table.items():
            if _host_matches(host, dom):
                return UrlClass(norm, plat, kind)
    for dom, plat in LISTING.items():
        if _host_matches(host, dom) and plat is not None:
            if dom == "google.com" and not (path.startswith("/maps") or path.startswith("/search") or "cid=" in query):
                break
            return UrlClass(norm, plat, "listing")
    unbranded = any(_host_matches(host, d) for d in BUILDER_SUBDOMAINS)
    return UrlClass(norm, "website", "independent", unbranded)


# ----------------------------------------------------------------------------- addresses
_ABBR = {"STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN",
         "COURT": "CT", "CIRCLE": "CIR", "PLACE": "PL", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TERRACE": "TER",
         "TRAIL": "TRL", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW",
         "SOUTHEAST": "SE", "SOUTHWEST": "SW", "SUITE": "STE", "APARTMENT": "APT", "UNIT": "UNIT"}


def zip5(raw: str | None) -> str | None:
    m = re.match(r"\s*(\d{5})", raw or "")
    return m.group(1) if m else None


def address_key(line1: str | None, line2: str | None, zip_code: str | None) -> str | None:
    """Comparable key: street number + street name + unit + ZIP5 (abbreviations unified)."""
    z = zip5(zip_code)
    text = " ".join(p for p in (line1, line2) if p and p.strip()).upper()
    if not text or not z:
        return None
    text = re.sub(r"[.,#]", " ", text)
    try:
        tagged, _ = usaddress.tag(text)
    except usaddress.RepeatedLabelError:
        tagged = {}
    if tagged.get("AddressNumber") and tagged.get("StreetName"):
        parts = [tagged.get("AddressNumber"), tagged.get("StreetNamePreDirectional"), tagged.get("StreetName"),
                 tagged.get("StreetNamePostType"), tagged.get("StreetNamePostDirectional"),
                 tagged.get("OccupancyIdentifier")]
        toks = " ".join(p for p in parts if p).split()
    elif tagged.get("USPSBoxID"):
        toks = ["PO", "BOX", tagged["USPSBoxID"]]
    else:
        toks = text.split()
    toks = [_ABBR.get(t, t) for t in toks]
    return " ".join(toks) + "|" + z


def is_po_box(line1: str | None) -> bool:
    return bool(re.search(r"\bP\.?\s*O\.?\s*BOX\b|\bPOST OFFICE BOX\b", (line1 or "").upper()))
