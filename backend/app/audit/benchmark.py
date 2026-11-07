"""Industry peer benchmark for a website audit (M3; research/M3_PREP.md §5).

Peers: the latest scored audit of other businesses in the SAME niche, first in the same county, then across the
target region. At least MIN_PEERS are required; otherwise the answer is "not enough comparable data" - unrelated
industries are never mixed in to produce a number.
"""
from __future__ import annotations

import datetime as _dt
import statistics

from sqlalchemy import text

MIN_PEERS = 8

_PEERS_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (a.location_id) a.id, a.location_id, a.wqs, a.started_at
  FROM website_audit a WHERE a.wqs IS NOT NULL AND a.status IN ('completed', 'partial') AND a.location_id <> :loc
  ORDER BY a.location_id, a.started_at DESC, a.id DESC)
SELECT latest.id, latest.wqs, latest.started_at FROM latest JOIN location l ON l.id = latest.location_id
WHERE l.merged_into_id IS NULL AND l.industry_profile = :niche AND {where}
"""


def compute(conn, location_id: int, wqs_value: float | None) -> dict:
    loc = conn.execute(text("SELECT l.industry_profile, l.county_fips, a.name FROM location l "
                            "LEFT JOIN area a ON a.kind = 'county' AND a.spec->>'geoid' = l.county_fips WHERE l.id = :i"),
                       {"i": location_id}).first()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    if loc is None or not loc[0]:
        return {"scope": "none", "niche": None, "area": None, "peers": 0, "computed_at": now,
                "description": "no industry recorded for this business",
                "message": "Not enough comparable data: the business has no recorded industry."}
    niche, fips, county = loc
    attempts = [("county", f"{county or fips} County", "l.county_fips = :fips"),
                ("region", "the target counties",
                 "l.county_fips IN (SELECT spec->>'geoid' FROM area WHERE kind = 'county' AND is_target)")]
    peers, scope, area = [], "none", None
    for sc, label, where in attempts:
        rows = conn.execute(text(_PEERS_SQL.format(where=where)), {"loc": location_id, "niche": niche, "fips": fips}).all()
        if len(rows) >= MIN_PEERS:
            peers, scope, area = rows, sc, label
            break
        peers = rows
    niche_label = niche.replace("_", " ")
    if scope == "none":
        return {"scope": "none", "niche": niche, "area": None, "peers": len(peers), "computed_at": now,
                "description": f"{niche_label} businesses in the target counties",
                "message": f"Not enough comparable data: {len(peers)} audited {niche_label} businesses so far "
                           f"(at least {MIN_PEERS} are needed)."}
    values = sorted(r[1] for r in peers)
    ids = [r[0] for r in peers]
    crit = {k: {"passed": int(p), "assessed": int(a)} for k, p, a in conn.execute(text(
        "SELECT criterion, count(*) FILTER (WHERE passed), count(*) FILTER (WHERE passed IS NOT NULL) "
        "FROM website_audit_finding WHERE audit_id = ANY(:ids) GROUP BY criterion"), {"ids": ids}).all()}
    q = statistics.quantiles(values, n=4) if len(values) >= 2 else [values[0]] * 3
    pct = None if wqs_value is None else round(100 * sum(1 for v in values if v <= wqs_value) / len(values))
    desc = f"{niche_label} businesses in {area}"
    return {"scope": scope, "niche": niche, "area": area, "peers": len(values), "computed_at": now, "description": desc,
            "median": round(statistics.median(values), 1), "p75": round(q[2], 1), "percentile": pct,
            "audited_from": min(r[2] for r in peers).date().isoformat(), "audited_to": max(r[2] for r in peers).date().isoformat(),
            "criteria": crit,
            "message": f"Compared with {len(values)} {desc} (audits {min(r[2] for r in peers).date()} to "
                       f"{max(r[2] for r in peers).date()}): median website quality {statistics.median(values):.0f}"
                       + (f"; this business is at the {pct}th percentile." if pct is not None else ".")}
