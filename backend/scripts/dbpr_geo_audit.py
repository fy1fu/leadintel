"""Audit the INFERRED (V13) DBPR county-code map against Census ZCTA->county relationships.

Usage (from backend/):  python scripts/dbpr_geo_audit.py <data_dir> <licensee.csv> [...]
Reads only local files: <data_dir>/boundaries/tab20_zcta520_county20_natl.txt and
cb_2025_us_county_500k.zip (both downloaded by the pipeline). Prints the numbers cited in
docs/DBPR_GEOGRAPHY.md. This is a consistency check, NOT an official verification.
"""
from __future__ import annotations

import csv
import io
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import shapefile
from shapely.geometry import shape

from app.sources.dbpr.importer import COLUMNS, DBPR_COUNTY_TO_FIPS, parse_date, resolve_geo

TARGET = {"12019", "12031", "12109"}


def main(data_dir: Path, files: list[Path], today: date) -> None:
    zc: dict[str, set[str]] = defaultdict(set)
    with open(data_dir / "boundaries" / "tab20_zcta520_county20_natl.txt", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter="|"):
            if r["GEOID_ZCTA5_20"] and r["GEOID_COUNTY_20"].startswith("12"):   # same filter as load_zip_counties
                zc[r["GEOID_ZCTA5_20"]].add(r["GEOID_COUNTY_20"])
    z = zipfile.ZipFile(data_dir / "boundaries" / "cb_2025_us_county_500k.zip")
    part = {n.rsplit(".", 1)[1]: io.BytesIO(z.read(n)) for n in z.namelist() if n.rsplit(".", 1)[1] in ("shp", "dbf", "shx")}
    shp = shapefile.Reader(**part)
    geom, name = {}, {}
    for sr in shp.iterShapeRecords():
        rec = sr.record.as_dict()
        if rec["STATEFP"] == "12":
            geom[rec["GEOID"]] = shape(sr.shape.__geo_interface__).buffer(0.001)   # ~100 m: touching counties count as adjacent
            name[rec["GEOID"]] = rec["NAME"]
    agree, dis, kinds, tgt, tgt_active, pairs, examples = Counter(), Counter(), Counter(), Counter(), Counter(), Counter(), []
    rows = 0
    status, status_active = Counter(), Counter()
    for f in files:
        with open(f, encoding="utf-8", errors="replace", newline="") as fh:
            for r in csv.reader(fh):
                if len(r) != len(COLUMNS):
                    continue
                rows += 1
                d = dict(zip(COLUMNS, (c.strip() for c in r)))
                g = resolve_geo(d, zc)
                if g.county_fips in TARGET or g.code_fips in TARGET or set(g.zip_fips) & TARGET:
                    status[g.status] += 1
                    k, e = parse_date(d["expiry_date"])
                    if d["status"] == "A" and d["licence_number"] and k == "valid" and e >= today:
                        status_active[g.status] += 1
                cf, zf = DBPR_COUNTY_TO_FIPS.get(d["county_code"]), zc.get(d["zip"][:5])
                if not cf or not zf:
                    continue
                if cf in zf:
                    agree[d["county_code"]] += 1
                    continue
                dis[d["county_code"]] += 1
                kind = "adjacent" if any(geom[cf].intersects(geom[c]) for c in zf) else "non_adjacent"
                kinds[kind] += 1
                if (zf | {cf}) & TARGET:
                    tgt[kind] += 1
                    k, e = parse_date(d["expiry_date"])
                    if d["status"] == "A" and d["licence_number"] and k == "valid" and e >= today:
                        tgt_active[kind] += 1
                    pairs[(name[cf], "/".join(sorted(name[c] for c in zf)))] += 1
                    if kind == "non_adjacent" and len(examples) < 8:
                        examples.append((d["county_code"], name[cf], d["zip"][:5], sorted(name[c] for c in zf), d["city"]))
    a, b = sum(agree.values()), sum(dis.values())
    print(f"rows read: {rows}; rows with a mapped code and a Florida ZCTA ZIP: {a + b}")
    print(f"agree: {a} ({a / (a + b):.2%}); disagree: {b} ({b / (a + b):.2%})")
    lo = min(((agree[c] / (agree[c] + dis[c])), c) for c in set(agree) | set(dis))
    print(f"codes seen: {len(set(agree) | set(dis))} of 67; lowest per-code agreement: code {lo[1]} at {lo[0]:.1%}")
    print("disagreements by kind:", dict(kinds))
    print("touching Duval/St. Johns/Clay:", dict(tgt), "- of which otherwise active evidence:", dict(tgt_active))
    print("top code-county vs ZIP-county pairs touching the targets:")
    for p, n in pairs.most_common(12):
        print(f"  {p[0]} code vs ZIP in {p[1]}: {n}")
    print("resolve_geo status, rows touching the targets:", dict(status))
    print("  of which otherwise active evidence:", dict(status_active))
    print("non-adjacent examples (code, code county, ZIP, ZIP counties, city):")
    for x in examples:
        print("  ", x)


if __name__ == "__main__":
    main(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]], date.today())
