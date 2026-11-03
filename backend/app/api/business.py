"""Business-data API (PHASE1.md M4.1). Read-only in Phase 1.

If the business database is unavailable these endpoints answer 503 with the reason; network
diagnostics (other router) keep working because they never use this database.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db.session import DatabaseUnavailable, check_database, make_engine

router = APIRouter(prefix="/api")


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return make_engine(get_settings())


def engine() -> Engine:
    try:
        e = _engine()
        check_database(e)
        return e
    except (DatabaseUnavailable, RuntimeError) as exc:
        raise HTTPException(503, detail=f"business database unavailable: {exc}") from exc


def _rows(sql: str, **params) -> list[dict]:
    with engine().connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


LIST_SQL = """
SELECT l.id, l.display_name AS name, coalesce(l.industry_profile, 'unclassified') AS industry, a.key AS county,
  l.operating_status, ST_X(l.geom) AS lon, ST_Y(l.geom) AS lat, b.value AS bos, p.value AS lps, b.stage,
  b.evidence_points, q.status AS qualification,
  EXISTS (SELECT 1 FROM online_profile o WHERE o.location_id=l.id AND o.platform_kind='independent') AS website_listed,
  EXISTS (SELECT 1 FROM location_contact lc JOIN contact_point c ON c.id=lc.contact_point_id
          WHERE lc.location_id=l.id AND lc.active AND c.kind='phone') AS has_phone
FROM location l
JOIN area a ON a.kind='county' AND a.is_target AND a.spec->>'geoid' = l.county_fips
LEFT JOIN score b ON b.location_id=l.id AND b.kind='bos' AND b.superseded_at IS NULL
LEFT JOIN score p ON p.location_id=l.id AND p.kind='lps' AND p.superseded_at IS NULL
LEFT JOIN qualification q ON q.location_id=l.id AND q.superseded_at IS NULL
WHERE l.merged_into_id IS NULL
  AND (CAST(:county AS text) IS NULL OR a.key = CAST(:county AS text))
  AND (CAST(:niche AS text) IS NULL OR l.industry_profile = CAST(:niche AS text))
  AND (CAST(:quals AS text[]) IS NULL OR q.status = ANY(CAST(:quals AS text[])))
  AND (CAST(:q AS text) IS NULL OR l.display_name ILIKE '%' || CAST(:q AS text) || '%')
ORDER BY p.value DESC NULLS LAST, l.id
LIMIT :limit
"""


@router.get("/prospects")
def prospects(county: str | None = None, niche: str | None = None, qualification: str | None = None,
              q: str | None = None, limit: int = Query(500, le=5000)) -> dict:
    quals = qualification.split(",") if qualification else None
    rows = _rows(LIST_SQL, county=county, niche=niche, quals=quals, q=q, limit=limit)
    return {"count": len(rows), "items": rows,
            "note": "Scores are provisional until website research (Phase 2) and competitor analysis (Phase 3)."}


@router.get("/prospects/{location_id}")
def profile(location_id: int) -> dict:
    loc = _rows("SELECT id, display_name, name_norm, category, industry_profile, county_fips, operating_status, chain_flag, "
                "brand_wikidata, merged_into_id, ST_X(geom) lon, ST_Y(geom) lat, first_seen_at, last_seen_at "
                "FROM location WHERE id=:i", i=location_id)
    if not loc:
        raise HTTPException(404, "unknown location")
    i = location_id
    score_rows = _rows("SELECT id, kind, stage, value, pre_cap_value, caps_applied, evidence_points, confidence, config_versions, "
                       "computed_at FROM score WHERE location_id=:i AND superseded_at IS NULL", i=i)
    for sc in score_rows:
        sc["factors"] = _rows("SELECT component, factor, effect, evidence, provenance FROM score_factor WHERE score_id=:s "
                              "ORDER BY component, abs(effect) DESC", s=sc["id"])
    return {
        "location": loc[0],
        "aliases": _rows("SELECT alias, kind FROM location_alias WHERE location_id=:i ORDER BY kind", i=i),
        "addresses": _rows("SELECT role, line1, line2, city, state, zip5, county_fips, geocode_method, is_operating_location, "
                           "not_operating_reason FROM address WHERE owner_type='location' AND owner_id=:i", i=i),
        "contacts": _rows("SELECT c.kind, c.display, lc.verification_state, lc.source_key FROM location_contact lc "
                          "JOIN contact_point c ON c.id=lc.contact_point_id WHERE lc.location_id=:i AND lc.active", i=i),
        "online_profiles": _rows("SELECT url, platform, platform_kind, verification_state, source_key FROM online_profile "
                                 "WHERE location_id=:i ORDER BY platform_kind", i=i),
        "organizations": _rows("""SELECT o.document_number, o.name, o.filing_kind, o.filing_type_code, o.status, o.registration_date,
                                         lo.role, lo.confidence, lo.evidence,
                                         (SELECT json_agg(json_build_object('owner_document', ol.to_document_number, 'kind', ol.kind))
                                          FROM organization_link ol WHERE ol.from_org_id = o.id) AS links
                                  FROM location_organization lo JOIN organization o ON o.id=lo.organization_id
                                  WHERE lo.location_id=:i AND lo.active""", i=i),
        "facts": _rows("SELECT field, value, source_key, provenance, verification_state, confidence, observed_at, fetched_at "
                       "FROM fact WHERE subject_type='location' AND subject_id=:i AND superseded_at IS NULL ORDER BY field", i=i),
        "scores": score_rows,
        "qualification": (_rows("SELECT status, reasons, stage, computed_at FROM qualification WHERE location_id=:i "
                                "AND superseded_at IS NULL", i=i) or [None])[0],
        "keyword_hits": _rows("SELECT \"group\", polarity, term, matched_text, field, effect, neutralized FROM keyword_hit "
                              "WHERE location_id=:i AND superseded_at IS NULL", i=i),
        "sources": _rows("""SELECT k.source_key, k.external_id, k.match_method, k.match_score, sf.remote_path AS file, sr.line_no
                            FROM location_source_link k
                            LEFT JOIN LATERAL (SELECT * FROM source_record r WHERE r.external_id = k.external_id
                                               ORDER BY r.id DESC LIMIT 1) sr ON true
                            LEFT JOIN source_file sf ON sf.id = sr.source_file_id
                            WHERE k.location_id=:i AND k.active""", i=i),
        "merges": _rows("SELECT id, action, survivor_id, merged_id, method, evidence, at FROM merge_event "
                        "WHERE survivor_id=:i OR merged_id=:i ORDER BY at", i=i),
    }


@router.get("/coverage/latest")
def coverage() -> dict:
    run = _rows("SELECT id, status, area_keys, params, started_at, finished_at FROM discovery_run ORDER BY id DESC LIMIT 1")
    if not run:
        return {"run": None, "coverage": []}
    cov = _rows("SELECT c.source_key AS source, a.key AS area, c.state, c.reason, c.record_count FROM coverage c "
                "JOIN area a ON a.id=c.area_id WHERE c.run_id=:r ORDER BY 1, 2", r=run[0]["id"])
    return {"run": run[0], "coverage": cov}


@router.get("/niches")
def niches() -> dict:
    from sqlalchemy.orm import Session

    from app.reports.prospects import niche_comparison
    e = engine()
    with Session(e) as s:
        fips = [r[0] for r in s.execute(text("SELECT spec->>'geoid' FROM area WHERE kind='county' AND is_target AND enabled"))]
        return niche_comparison(s, fips)
