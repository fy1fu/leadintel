"""Places for choosing a search-area center (M3 Search Area, PLAN.md §14.2): ZIP codes and cities.

Free, keyless Census Gazetteer files, downloaded during Load Data (which holds the maintenance lock) through the
same safe download helper as every other source. They are reduced to one small JSON file for the target state
(`data/geo/gazetteer-<state>.json`) that the web app only reads.

* ZIP codes: ZCTA internal points ("ZIP code center"). A ZCTA approximates a USPS ZIP code; the point is a
  representative interior point, not a mailing address - results are labelled approximate.
* Cities: incorporated places and census-designated places of the state, internal point ("city center").
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import threading
import zipfile
from pathlib import Path

from app.db.locks import ensure_write_allowed
from app.net.http import InfraClient
from app.sources.common import download_checked

GAZETTEER_YEAR = "2024"
GAZ_BASE = f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{GAZETTEER_YEAR}_Gazetteer"
ZCTA_URL = f"{GAZ_BASE}/{GAZETTEER_YEAR}_Gaz_zcta_national.zip"
PLACE_URL = GAZ_BASE + "/{year}_gaz_place_{fips}.txt"
FORMAT = 1
# Legal/statistical area descriptions at the end of Gazetteer place names ("Jacksonville city", "Fruit Cove CDP").
_LSAD_SUFFIX = re.compile(r"\s+(city|town|village|CDP|municipality|borough)$", re.I)
log = logging.getLogger(__name__)


def gazetteer_path(data_dir: Path, state_usps: str = "FL") -> Path:
    return Path(data_dir) / "geo" / f"gazetteer-{state_usps.lower()}.json"


def _point(lat: str, lon: str) -> tuple[float, float] | None:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (la, lo) if math.isfinite(la) and math.isfinite(lo) and abs(la) <= 90 and abs(lo) <= 180 else None


def _rows(text: str):
    # Gazetteer files are tab-separated; the last header carries trailing spaces.
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    for row in reader:
        yield dict(zip(header, (c.strip() for c in row)))


def parse_zcta(text: str, keep: set[str] | None = None) -> dict[str, list[float]]:
    out = {}
    for r in _rows(text):
        z = r.get("GEOID", "")
        if len(z) == 5 and z.isdigit() and (keep is None or z in keep):
            p = _point(r.get("INTPTLAT"), r.get("INTPTLONG"))
            if p:
                out[z] = [round(p[0], 6), round(p[1], 6)]
    return out


def parse_places(text: str, state_usps: str) -> list[list]:
    out = []
    for r in _rows(text):
        if r.get("USPS") != state_usps:
            continue
        p = _point(r.get("INTPTLAT"), r.get("INTPTLONG"))
        name = _LSAD_SUFFIX.sub("", r.get("NAME", "")).strip()
        if p and name:
            out.append([name, round(p[0], 6), round(p[1], 6)])
    return sorted(out, key=lambda x: x[0].lower())


def refresh_gazetteer(client: InfraClient, data_dir: Path, zcta_keep: set[str] | None, *, state_fips: str = "12",
                      state_usps: str = "FL") -> dict:
    """Downloads (or reuses) the Gazetteer files and writes the reduced JSON. Needs the maintenance lock."""
    base = Path(data_dir) / "geo"
    zdl = download_checked(client, ZCTA_URL, base / Path(ZCTA_URL).name)
    pdl = download_checked(client, PLACE_URL.format(year=GAZETTEER_YEAR, fips=state_fips),
                           base / f"{GAZETTEER_YEAR}_gaz_place_{state_fips}.txt")
    with zipfile.ZipFile(zdl.path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".txt")]
        if len(names) != 1:
            raise ValueError(f"unexpected ZCTA archive contents: {names}")
        zcta = parse_zcta(zf.read(names[0]).decode("utf-8-sig"), zcta_keep)
    places = parse_places(pdl.path.read_text(encoding="utf-8-sig"), state_usps)
    if not zcta or not places:
        raise ValueError(f"gazetteer looks empty (ZIP codes: {len(zcta)}, places: {len(places)}); kept the previous file")
    doc = {"format": FORMAT, "year": GAZETTEER_YEAR, "state": state_usps, "sources": [ZCTA_URL, pdl.path.name],
           "zcta_sha256": zdl.sha256, "place_sha256": pdl.sha256, "zcta": zcta, "places": places}
    dest = gazetteer_path(data_dir, state_usps)
    ensure_write_allowed(dest)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, dest)
    return {"zip_codes": len(zcta), "places": len(places), "year": GAZETTEER_YEAR}


_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def load_gazetteer(data_dir: Path, state_usps: str = "FL") -> dict | None:
    """The reduced gazetteer (read-only, cached by modification time), or None before the first Load Data."""
    p = gazetteer_path(data_dir, state_usps)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    with _cache_lock:
        hit = _cache.get(str(p))
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if doc.get("format") != FORMAT:
            return None
        doc["_places_lc"] = [(n.lower(), n, la, lo) for n, la, lo in doc.get("places", [])]
        _cache[str(p)] = (mtime, doc)
        return doc


def find_zip(doc: dict | None, q: str) -> list[dict]:
    if not doc or not (len(q) == 5 and q.isdigit()) or q not in doc["zcta"]:
        return []
    lat, lon = doc["zcta"][q]
    return [{"kind": "zip", "label": f"ZIP {q}", "lat": lat, "lon": lon, "precision": "ZIP code center (approximate)",
             "source": f"Census Gazetteer {doc['year']}"}]


def find_places(doc: dict | None, q: str, limit: int = 5) -> list[dict]:
    if not doc or len(q) < 2:
        return []
    ql = re.sub(r",?\s+(fl|florida)$", "", q.strip().lower())
    starts = [p for p in doc["_places_lc"] if p[0].startswith(ql)]
    contains = [p for p in doc["_places_lc"] if ql in p[0] and not p[0].startswith(ql)]
    out = []
    for _, name, lat, lon in (starts + contains)[:limit]:
        out.append({"kind": "city", "label": f"{name}, {doc['state']}", "lat": lat, "lon": lon,
                    "precision": "city center (approximate)", "source": f"Census Gazetteer {doc['year']}"})
    return out
