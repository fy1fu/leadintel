"""Domain history observations for the website audit (M3 step 4).

Free sources already registered in config/network.yaml, contacted through the infrastructure client:
* Wayback Machine CDX API (``web.archive.org``): first and most recent capture of the site;
* RDAP (``rdap.verisign.com``) for .com/.net: registration and last-change dates of the domain.

These describe how *established* a website is, not how good it is: they are stored as dated, sourced observations
and are NOT WQS criteria (a young site is not a worse site). Any failure is recorded as "unavailable", never guessed.
"""
from __future__ import annotations

import datetime as _dt
import json
from urllib.parse import urlsplit

CDX_URL = "https://web.archive.org/cdx/search/cdx"
RDAP_URL = "https://rdap.verisign.com/{tld}/v1/domain/{domain}"


def registrable_host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _ts(s: str) -> str | None:
    try:
        return _dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S").date().isoformat()
    except (ValueError, TypeError):
        return None


def wayback(client, url: str) -> dict:
    host = registrable_host(url)
    obs = {"source": "Wayback Machine CDX", "fetched_at": _now(), "first_capture": None, "last_capture": None}
    try:
        for key, extra in (("first_capture", {"limit": "1"}), ("last_capture", {"limit": "-1", "fastLatest": "true"})):
            r = client.get(CDX_URL, params={"url": host, "output": "json", "fl": "timestamp", **extra}, timeout=20)
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            rows = json.loads(r.text or "[]")
            if isinstance(rows, list) and len(rows) > 1 and isinstance(rows[-1], list) and rows[-1]:
                obs[key] = _ts(str(rows[-1][0]))
        obs["status"] = "ok" if obs["first_capture"] else "no captures"
    except Exception as exc:  # noqa: BLE001 - optional observation
        obs["status"] = f"unavailable ({type(exc).__name__})"
    return obs


def rdap(client, url: str) -> dict:
    host = registrable_host(url)
    labels = host.split(".")
    obs = {"source": "RDAP (Verisign)", "fetched_at": _now(), "registered": None, "last_changed": None}
    if len(labels) < 2 or labels[-1] not in ("com", "net"):
        obs["status"] = "not assessed (only .com and .net are looked up)"
        return obs
    domain = ".".join(labels[-2:])
    try:
        r = client.get(RDAP_URL.format(tld=labels[-1], domain=domain), timeout=20)
        if r.status_code == 404:
            obs["status"] = "not found"
            return obs
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        for ev in (r.json().get("events") or []):
            action, date = ev.get("eventAction"), str(ev.get("eventDate") or "")[:10]
            if action == "registration":
                obs["registered"] = date or None
            elif action == "last changed":
                obs["last_changed"] = date or None
        obs["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        obs["status"] = f"unavailable ({type(exc).__name__})"
    return obs


def domain_history(client, url: str) -> dict:
    return {"domain": registrable_host(url), "wayback": wayback(client, url), "rdap": rdap(client, url)}
