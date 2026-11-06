"""Geocoding for registry addresses (PHASE1.md item 1.6).

* ZIP pre-filter: Census 2020 ZCTA-to-county relationship file, so only addresses whose ZIP can
  fall inside a target county are geocoded (not the whole state).
* US Census batch geocoder (free, no key) returns point + county FIPS; results are cached by
  normalized address so nothing is geocoded twice.
* Every result records its method: `census_match` (rooftop/range match) or `zip_county`
  (ZIP lies entirely in one target county; no point). Unmatched addresses stay unmatched.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import GeocodeCache
from app.net.http import InfraClient
from app.net.mirrors import RetryableServiceError, raise_for_retryable
from app.sources.common import download_checked

ZCTA_COUNTY_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
ONELINE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"   # M3 Search Area center search
log = logging.getLogger(__name__)


def load_zip_counties(client: InfraClient, data_dir: Path, state_fips: str = "12") -> dict[str, set[str]]:
    dl = download_checked(client, ZCTA_COUNTY_URL, data_dir / "boundaries" / "tab20_zcta520_county20_natl.txt")
    out: dict[str, set[str]] = defaultdict(set)
    with open(dl.path, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh, delimiter="|"):
            z, c = row.get("GEOID_ZCTA5_20"), row.get("GEOID_COUNTY_20")
            if z and c and c.startswith(state_fips):
                out[z].add(c)
    return dict(out)


@dataclass
class GeoResult:
    key: str
    matched: bool
    lon: float | None
    lat: float | None
    county_fips: str | None
    quality: str | None


def _parse_batch(text: str) -> dict[str, GeoResult]:
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        rid = row[0]
        if len(row) >= 10 and row[2] == "Match":
            lon, lat = (float(x) for x in row[5].split(","))
            out[rid] = GeoResult(rid, True, lon, lat, row[8] + row[9], row[3].lower())
        else:
            out[rid] = GeoResult(rid, False, None, None, None, (row[2] if len(row) > 2 else "no_match").lower())
    return out


class CensusGeocoder:
    # The Census batch service has short outages (HTTP 502/503 seen live). One failed batch used to fail the
    # whole Sunbiz placement step, so each batch is retried after these waits before giving up.
    RETRY_DELAYS = (10.0, 30.0)

    def __init__(self, client: InfraClient, batch_size: int = 1000, *, retry_delays: tuple[float, ...] | None = None,
                 sleep=time.sleep):
        self.client = client
        self.batch_size = batch_size
        self.retry_delays = self.RETRY_DELAYS if retry_delays is None else tuple(retry_delays)
        self._sleep = sleep

    def _post_batch(self, payload: bytes) -> httpx.Response:
        """POST one batch; retry on 5xx/429, timeouts and dropped connections (not on other 4xx)."""
        for attempt in range(len(self.retry_delays) + 1):
            try:
                resp = self.client.request("POST", BATCH_URL, data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
                                           files={"addressFile": ("batch.csv", payload, "text/csv")}, timeout=600)
                return raise_for_retryable(resp, retry_404=False)
            except (RetryableServiceError, httpx.TransportError) as exc:
                if attempt == len(self.retry_delays):
                    raise
                log.warning("census geocoder batch attempt %d failed (%s); retrying in %.0fs", attempt + 1, exc,
                            self.retry_delays[attempt])
                self._sleep(self.retry_delays[attempt])
        raise AssertionError("unreachable")

    def geocode(self, s: Session, items: dict[str, tuple[str, str, str, str]]) -> dict[str, GeoResult]:
        """items: cache_key -> (street, city, state, zip). Returns results for all keys (cached or new)."""
        keys = list(items)
        cached = {r.address_key: r for r in s.execute(select(GeocodeCache).where(GeocodeCache.address_key.in_(keys))).scalars()}
        out = {k: GeoResult(k, c.matched, c.lon, c.lat, c.county_fips, c.quality) for k, c in cached.items()}
        todo = [k for k in keys if k not in cached]
        for i in range(0, len(todo), self.batch_size):
            chunk = todo[i:i + self.batch_size]
            buf = io.StringIO()
            w = csv.writer(buf)
            for n, k in enumerate(chunk):
                street, city, state, zp = items[k]
                w.writerow([n, street or "", city or "", state or "FL", zp or ""])
            resp = self._post_batch(buf.getvalue().encode())
            parsed = _parse_batch(resp.text)
            rows = []
            for n, k in enumerate(chunk):
                r = parsed.get(str(n), GeoResult(k, False, None, None, None, "missing_in_response"))
                r.key = k
                out[k] = r
                rows.append({"address_key": k, "matched": r.matched, "lon": r.lon, "lat": r.lat,
                             "county_fips": r.county_fips, "quality": r.quality, "response": {}})
            s.execute(insert(GeocodeCache).on_conflict_do_nothing(index_elements=["address_key"]), rows)
            s.flush()
        return out


def geocode_one_line(client: InfraClient, address: str, limit: int = 5, timeout: float = 15.0) -> list[dict]:
    """One free-text street address -> candidate points (Census one-line geocoder; free, no key).

    Used only when the user asks for an address lookup in the Search Area. Nothing is stored. Every returned
    coordinate is checked; a malformed answer yields no candidates rather than a wrong point.
    """
    import math
    resp = client.get(ONELINE_URL, params={"address": address[:200], "benchmark": "Public_AR_Current", "format": "json"},
                      timeout=timeout)
    raise_for_retryable(resp, retry_404=False)
    resp.raise_for_status()
    try:
        matches = resp.json()["result"]["addressMatches"]
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for m in matches if isinstance(matches, list) else []:
        c = m.get("coordinates") if isinstance(m, dict) else None
        x, y = (c or {}).get("x"), (c or {}).get("y")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y)):
            continue
        if abs(y) > 85 or abs(x) > 180:
            continue
        out.append({"label": str(m.get("matchedAddress") or address)[:200], "lat": float(y), "lon": float(x)})
        if len(out) >= limit:
            break
    return out
