"""Niche comparison (PHASE1.md 2.5) and prospect export (2.6).

No automatic winner and no ranking by size. Every count carries its coverage state; values that
are not measured yet (e.g. outdated websites before Phase 2) are reported as "not measured",
never as zero. Exports never infer missing contact data - a blank field states why it is blank.
"""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

NICHE_SQL = """
WITH cur AS (
  SELECT l.id, coalesce(l.industry_profile, 'unclassified') AS niche, l.county_fips, l.operating_status,
    q.status AS qual, b.value AS bos, p.value AS lps, b.evidence_points,
    EXISTS (SELECT 1 FROM online_profile o WHERE o.location_id = l.id AND o.platform_kind = 'independent') AS indep,
    EXISTS (SELECT 1 FROM online_profile o WHERE o.location_id = l.id AND o.platform_kind IN ('social','link_hub')) AS social,
    EXISTS (SELECT 1 FROM location_contact lc JOIN contact_point c ON c.id = lc.contact_point_id
            WHERE lc.location_id = l.id AND lc.active AND c.kind = 'phone') AS phone,
    EXISTS (SELECT 1 FROM location_contact lc JOIN contact_point c ON c.id = lc.contact_point_id
            WHERE lc.location_id = l.id AND lc.active AND c.kind = 'email') AS email,
    (SELECT count(DISTINCT source_key) FROM location_source_link k WHERE k.location_id = l.id AND k.active) AS n_sources
  FROM location l
  LEFT JOIN qualification q ON q.location_id = l.id AND q.superseded_at IS NULL
  LEFT JOIN score b ON b.location_id = l.id AND b.kind = 'bos' AND b.superseded_at IS NULL
  LEFT JOIN score p ON p.location_id = l.id AND p.kind = 'lps' AND p.superseded_at IS NULL
  WHERE l.merged_into_id IS NULL AND l.county_fips = ANY(:fips) AND (CAST(:niches AS text[]) IS NULL OR l.industry_profile = ANY(CAST(:niches AS text[])))
)
SELECT niche, county_fips,
  count(*) FILTER (WHERE operating_status <> 'permanently_closed') AS discovered_open_or_unknown,
  count(*) FILTER (WHERE operating_status = 'permanently_closed') AS closed,
  count(*) FILTER (WHERE operating_status <> 'permanently_closed' AND NOT indep) AS no_independent_website_listed,
  count(*) FILTER (WHERE operating_status <> 'permanently_closed' AND NOT indep AND social) AS social_only_listed,
  count(*) FILTER (WHERE qual = 'provisionally_qualified') AS provisionally_qualified,
  count(*) FILTER (WHERE qual = 'needs_research') AS needs_research,
  round(avg(bos) FILTER (WHERE qual IN ('provisionally_qualified','needs_research'))::numeric, 1) AS avg_bos_candidates,
  round(avg(lps) FILTER (WHERE qual IN ('provisionally_qualified','needs_research'))::numeric, 1) AS avg_lps_candidates,
  count(*) FILTER (WHERE phone) AS with_phone, count(*) FILTER (WHERE email) AS with_email,
  count(*) FILTER (WHERE NOT phone AND NOT email) AS mail_or_none_only,
  round(avg(evidence_points)::numeric, 2) AS avg_evidence_points,
  round((count(*) FILTER (WHERE n_sources >= 2))::numeric / nullif(count(*), 0), 3) AS share_multi_source
FROM cur GROUP BY niche, county_fips ORDER BY niche, county_fips
"""


def niche_comparison(s: Session, fips: list[str], niches: list[str] | None = None) -> dict:
    rows = [dict(r) for r in s.execute(text(NICHE_SQL), {"fips": fips, "niches": niches}).mappings()]
    names = dict(s.execute(text("SELECT spec->>'geoid', key FROM area WHERE kind='county'")).all())
    cov = s.execute(text("""SELECT DISTINCT ON (c.source_key, a.key) c.source_key, a.key, c.state, c.reason, r.id
                            FROM coverage c JOIN area a ON a.id = c.area_id JOIN discovery_run r ON r.id = c.run_id
                            ORDER BY c.source_key, a.key, r.id DESC""")).all()
    coverage = {}
    for src, area, state, reason, run in cov:
        coverage.setdefault(area, {})[src] = {"state": state, "reason": reason, "run_id": run}
    for r in rows:
        r["county"] = names.get(r.pop("county_fips"), "?")
        r["coverage"] = coverage.get(r["county"], {})
        r["outdated_websites"] = "not measured (website analysis is Phase 2)"
        r["complete"] = all(v["state"] == "complete" for v in r["coverage"].values()) and bool(r["coverage"])
    return {"rows": rows,
            "notes": ["No niche is ranked or chosen automatically; size alone is not a criterion.",
                      "All presence counts come from listing data; website discovery (Phase 2) confirms them.",
                      "Scores are provisional (stage=provisional) until Phase 2-3 inputs exist.",
                      "Rows whose coverage is not 'complete' must not be compared as if complete."]}


EXPORT_SQL = """
SELECT l.id, l.display_name, coalesce(l.industry_profile, 'unclassified') AS industry, a2.key AS county, l.operating_status,
  (SELECT concat_ws(', ', ad.line1, ad.city, ad.state, ad.zip5) FROM address ad WHERE ad.owner_type='location' AND ad.owner_id=l.id
     ORDER BY ad.role = 'principal' DESC, ad.id LIMIT 1) AS address,
  (SELECT string_agg(c.display || ' [' || lc.verification_state || ', ' || lc.source_key || ']', '; ')
     FROM location_contact lc JOIN contact_point c ON c.id = lc.contact_point_id WHERE lc.location_id = l.id AND lc.active AND c.kind='phone') AS phones,
  (SELECT string_agg(c.display || ' [' || lc.verification_state || ', ' || lc.source_key || ']', '; ')
     FROM location_contact lc JOIN contact_point c ON c.id = lc.contact_point_id WHERE lc.location_id = l.id AND lc.active AND c.kind='email') AS emails,
  (SELECT string_agg(o.url || ' [' || o.verification_state || ']', '; ') FROM online_profile o WHERE o.location_id=l.id AND o.platform_kind='independent') AS websites,
  (SELECT string_agg(o.platform || ': ' || o.url, '; ') FROM online_profile o WHERE o.location_id=l.id AND o.platform_kind<>'independent') AS profiles,
  (SELECT string_agg(o2.filing_kind || ' ' || o2.document_number || ' filed ' || coalesce(o2.registration_date::text, '?') || ' (' || lo.role || ')', '; ')
     FROM location_organization lo JOIN organization o2 ON o2.id = lo.organization_id WHERE lo.location_id = l.id AND lo.active) AS registry,
  b.value AS bos, p.value AS lps, b.stage, b.evidence_points, b.caps_applied::text AS caps, q.status AS qualification, q.reasons::text AS qualification_reasons,
  (SELECT string_agg(f.factor || ' ' || to_char(f.effect, 'FM990.0'), '; ' ORDER BY abs(f.effect) DESC)
     FROM score_factor f WHERE f.score_id = b.id AND f.effect <> 0) AS bos_factors,
  (SELECT string_agg(DISTINCT k.source_key || ':' || k.external_id, '; ') FROM location_source_link k WHERE k.location_id=l.id AND k.active) AS sources,
  l.last_seen_at::date AS last_seen
FROM location l
JOIN area a2 ON a2.kind='county' AND a2.spec->>'geoid' = l.county_fips
LEFT JOIN score b ON b.location_id = l.id AND b.kind='bos' AND b.superseded_at IS NULL
LEFT JOIN score p ON p.location_id = l.id AND p.kind='lps' AND p.superseded_at IS NULL
LEFT JOIN qualification q ON q.location_id = l.id AND q.superseded_at IS NULL
WHERE l.merged_into_id IS NULL AND l.county_fips = ANY(:fips)
  AND (CAST(:niche AS text) IS NULL OR l.industry_profile = CAST(:niche AS text))
  AND (CAST(:quals AS text[]) IS NULL OR q.status = ANY(CAST(:quals AS text[])))
ORDER BY p.value DESC NULLS LAST, l.id
LIMIT :limit
"""


def export_csv(s: Session, path: Path, fips: list[str], *, niche: str | None = None, quals: list[str] | None = None,
               limit: int = 100000) -> int:
    rows = s.execute(text(EXPORT_SQL), {"fips": fips, "niche": niche, "quals": quals, "limit": limit}).mappings().all()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        cols = list(rows[0].keys()) + ["missing"] if rows else ["id"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            d = dict(r)
            missing = [f"{k}: not published in the sources checked" for k in ("phones", "emails") if not d.get(k)]
            if not d.get("websites"):
                missing.append("websites: none listed; website discovery (Phase 2) not run yet")
            d["missing"] = "; ".join(missing)
            w.writerow(d)
    return len(rows)
