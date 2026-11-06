"""Business-data API (PHASE1.md M4.1). Read-only in Phase 1.

If the business database is unavailable these endpoints answer 503 with the reason; network
diagnostics (other router) keep working because they never use this database.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api.filters import (MAX_LEGACY_LIMIT, MAX_PAGE_SIZE, MAX_POINTS, OPERATING_STATUSES, QUALIFICATIONS,
                             CLAIM_FILTERS, FOLLOW_UP_FILTERS, SALES_STATUSES, SORTS, WEBSITE_STATUSES, FilterError, ProspectFilter, count_query, list_query,
                             points_query, split_list, summary_query)
from app.api.security import safe_link
from app.config import get_settings
from app.reports.prospects import MAX_EXPORT
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


def _scalar(sql: str, **params):
    with engine().connect() as c:
        return c.execute(text(sql), params).scalar()


def crm_context():
    """(CRM tables present, local identity or None), read from the business database (M2)."""
    from app.crm import service as crm
    with engine().connect() as c:
        available = crm.crm_available(c)
        return available, (crm.identity(c) if available else None)


def prospect_filter(county: str | None = None, niche: str | None = None, qualification: str | None = None,
                    q: str | None = None, website: str | None = None, operating: str | None = None,
                    phone: bool | None = None, email: bool | None = None,
                    bos_min: float | None = None, bos_max: float | None = None,
                    lps_min: float | None = None, lps_max: float | None = None, sort: str = "lps",
                    sales: str | None = None, claim: str | None = None, follow_up: str | None = None,
                    list_id: int | None = None) -> ProspectFilter:
    """Query parameters -> ProspectFilter. Lists are comma-separated; a single value works as before (S2a).
    The CRM context (local organization and user) comes from the database, never from the request (M2)."""
    available, me = crm_context()
    try:
        return ProspectFilter(counties=split_list(county), niches=split_list(niche),
                              qualifications=split_list(qualification), q=q, website=split_list(website),
                              operating=split_list(operating), phone=phone, email=email, bos_min=bos_min,
                              bos_max=bos_max, lps_min=lps_min, lps_max=lps_max, sort=sort,
                              sales=split_list(sales), claim=claim or None, follow_up=follow_up or None,
                              list_id=list_id, crm=available, crm_org=me.organization_id if me else None,
                              crm_user=me.user_id if me else None)
    except FilterError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/prospects")
def prospects(f: ProspectFilter = Depends(prospect_filter),
              page: int | None = Query(None, ge=1), page_size: int | None = Query(None, ge=1, le=MAX_PAGE_SIZE),
              limit: int = Query(500, ge=0, le=MAX_LEGACY_LIMIT)) -> dict:
    """Prospects matching the filters, server-side.

    Paged mode (page and/or page_size given): ``page_size`` rows (default 50, at most 200) of page ``page``.
    Legacy mode (neither given): the first ``limit`` rows (default 500, at most 5000), as before S2a;
    ``limit=0`` still returns no rows (now with ``total``, i.e. a count-only request). Negative limits are 422
    (before S2a they reached the database and failed with 500).
    ``count`` is the number of rows returned (unchanged meaning); ``total`` is every matching business.
    """
    paged = page is not None or page_size is not None
    size = (page_size or 50) if paged else limit
    pg = page or 1
    sql, params = list_query(f, size, (pg - 1) * size if paged else 0)
    rows = _rows(sql, **params)
    csql, cparams = count_query(f)
    total = int(_scalar(csql, **cparams) or 0)
    out = {"count": len(rows), "total": total, "items": rows,
           "note": "Scores are provisional until website research (Phase 2) and competitor analysis (Phase 3)."}
    if paged:
        out.update(page=pg, page_size=size, pages=(total + size - 1) // size, has_more=pg * size < total)
    else:
        out.update(limit=limit, truncated=total > len(rows))
    return out


@router.get("/prospects/summary")
def prospects_summary(f: ProspectFilter = Depends(prospect_filter)) -> dict:
    """How many businesses match, how many of them can be mapped, and how they split by qualification."""
    sql, params = summary_query(f)
    rows = _rows(sql, **params)
    total = sum(int(r["n"]) for r in rows)
    with_coords = sum(int(r["with_coordinates"]) for r in rows)
    return {"total": total, "with_coordinates": with_coords, "without_coordinates": total - with_coords,
            "by_qualification": {r["qualification"]: int(r["n"]) for r in rows}}


@router.get("/prospects/points")
def prospect_points(f: ProspectFilter = Depends(prospect_filter)) -> dict:
    """Map points for the matching businesses that have coordinates (compact [id, lon, lat, qualification])."""
    sql, params = points_query(f, MAX_POINTS)
    rows = _rows(sql, **params)
    truncated = len(rows) > MAX_POINTS
    rows = rows[:MAX_POINTS]
    ssql, sparams = summary_query(f)
    srows = _rows(ssql, **sparams)
    total = sum(int(r["n"]) for r in srows)
    with_coords = sum(int(r["with_coordinates"]) for r in srows)
    return {"points": [[r["id"], r["lon"], r["lat"], r["qualification"]] for r in rows],
            "returned": len(rows), "total": total, "with_coordinates": with_coords,
            "without_coordinates": total - with_coords, "limit": MAX_POINTS, "truncated": truncated}


def _with_links(contacts: list[dict]) -> list[dict]:
    """Adds a vetted link target (tel:/mailto:) to each contact, or None when the value must stay plain text."""
    kinds = {"phone": "tel", "email": "mailto"}
    for c in contacts:
        c["href"] = safe_link(kinds[c["kind"]], c.get("value_norm") or c.get("display")) if c["kind"] in kinds else None
    return contacts


@router.get("/filters/options")
def filter_options() -> dict:
    """Values for the filter panel: target counties and the niches present in the data."""
    counties = _rows("SELECT key, name FROM area WHERE kind = 'county' AND is_target ORDER BY name")
    niches = [r["niche"] for r in _rows("SELECT DISTINCT industry_profile AS niche FROM location "
                                        "WHERE industry_profile IS NOT NULL AND merged_into_id IS NULL ORDER BY 1")]
    return {"counties": counties, "niches": niches, "website": list(WEBSITE_STATUSES),
            "operating": list(OPERATING_STATUSES), "qualifications": list(QUALIFICATIONS), "sorts": list(SORTS),
            "sales": list(SALES_STATUSES), "claim": list(CLAIM_FILTERS), "follow_up": list(FOLLOW_UP_FILTERS)}


EXPORT_CHUNK_ROWS = 500   # rows per streamed chunk


@router.get("/prospects/export.csv")
def prospects_export(f: ProspectFilter = Depends(prospect_filter),
                     limit: int = Query(MAX_EXPORT, ge=1, le=MAX_EXPORT)) -> StreamingResponse:
    """CSV of the prospects matching the filters - the same filter as the list, summary and map.

    The file is a clean CSV table. How many businesses matched, how many rows were exported and whether the
    export was cut off are sent as X-LeadIntel-* headers (the page shows the counts before exporting). Rows are
    streamed from the database; nothing is written on the server.
    """
    import csv
    import io
    from datetime import datetime

    from app.reports.prospects import export_fields, export_query, export_row
    e = engine()
    csql, cparams = count_query(f)
    with e.connect() as c:
        total = int(c.execute(text(csql), cparams).scalar() or 0)
    sql, params = export_query(f, limit)

    def rows():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=export_fields(f))
        w.writeheader()
        with e.connect().execution_options(stream_results=True, yield_per=EXPORT_CHUNK_ROWS) as c:
            for i, r in enumerate(c.execute(text(sql), params).mappings(), 1):
                w.writerow(export_row(r))
                if i % EXPORT_CHUNK_ROWS == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
        yield buf.getvalue()

    name = f"leadintel-prospects-{datetime.now():%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(rows(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "X-LeadIntel-Total": str(total), "X-LeadIntel-Exported": str(min(total, limit)),
        "X-LeadIntel-Limit": str(limit), "X-LeadIntel-Truncated": "true" if total > limit else "false"})


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
        "contacts": _with_links(_rows("SELECT c.kind, c.display, c.value_norm, lc.verification_state, lc.source_key "
                                      "FROM location_contact lc JOIN contact_point c ON c.id=lc.contact_point_id "
                                      "WHERE lc.location_id=:i AND lc.active", i=i)),
        "online_profiles": [dict(r, href=safe_link("web", r["url"]))
                            for r in _rows("SELECT url, platform, platform_kind, verification_state, source_key "
                                           "FROM online_profile WHERE location_id=:i ORDER BY platform_kind", i=i)],
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
