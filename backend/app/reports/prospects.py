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

from app.api.filters import FROM_SQL, ProspectFilter, count_query

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


EXPORT_COLUMNS = """
SELECT l.id, l.display_name, coalesce(l.industry_profile, 'unclassified') AS industry, a.key AS county, l.operating_status,
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
"""
MAX_EXPORT = 100_000


def export_query(f: ProspectFilter, limit: int = MAX_EXPORT) -> tuple[str, dict]:
    """The export uses the same FROM, WHERE and ORDER BY as the list, summary and map (app.api.filters)."""
    where, params = f.where()
    return (f"{EXPORT_COLUMNS}{FROM_SQL}WHERE {where}\nORDER BY {f.order_by()}\nLIMIT :limit",
            {**params, "limit": int(limit)})


def export_row(r) -> dict:
    """Adds the 'missing' explanation: a blank field states why it is blank; nothing is inferred."""
    d = dict(r)
    missing = [f"{k}: not published in the sources checked" for k in ("phones", "emails") if not d.get(k)]
    if not d.get("websites"):
        missing.append("websites: none listed; website discovery (Phase 2) not run yet")
    d["missing"] = "; ".join(missing)
    return d


EXPORT_FIELDS = ["id", "display_name", "industry", "county", "operating_status", "address", "phones", "emails",
                 "websites", "profiles", "registry", "bos", "lps", "stage", "evidence_points", "caps",
                 "qualification", "qualification_reasons", "bos_factors", "sources", "last_seen", "missing"]


def export_counts(s, f: ProspectFilter, limit: int = MAX_EXPORT) -> dict:
    sql, params = count_query(f)
    total = int(s.execute(text(sql), params).scalar() or 0)
    return {"total": total, "exported": min(total, limit), "limit": limit, "truncated": total > limit}


def export_csv(s: Session, path: Path, fips: list[str] | None = None, *, niche: str | None = None,
               quals: list[str] | None = None, limit: int = MAX_EXPORT, flt: ProspectFilter | None = None) -> int:
    """Writes the prospects matching ``flt`` (or the legacy niche/quals arguments) to ``path``; returns rows written.

    ``fips`` (legacy) restricts to those county codes on top of the target-county join every view uses.
    """
    f = flt or ProspectFilter(niches=[niche] if niche else [], qualifications=list(quals or []))
    if fips:
        f.fips = list(fips)
    sql, params = export_query(f, limit)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EXPORT_FIELDS)
        w.writeheader()
        for r in s.execute(text(sql), params).mappings():
            w.writerow(export_row(r))
            n += 1
    return n
