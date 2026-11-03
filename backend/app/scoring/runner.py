"""Compute and persist provisional scores, keyword hits, industry and qualification (PHASE1.md M2).

Append-only: a location's previous current rows are superseded only when its inputs (features,
keyword hits, config versions) changed - identical inputs produce no new rows.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import KeywordHit, Qualification, Score, ScoreFactor
from app.scoring.model import Features, score
from app.targeting.keywords import hard_excluded, match, tokens

FEATURE_SQL = """
SELECT l.id, l.display_name, l.category, l.operating_status, l.chain_flag, l.county_fips, l.last_seen_at::date AS last_seen,
  (SELECT (sr.payload->>'confidence')::float FROM location_source_link k JOIN source_record sr
      ON sr.external_id = k.external_id AND sr.record_kind = 'place'
    WHERE k.location_id = l.id AND k.source_key = 'overture' AND k.active ORDER BY sr.id DESC LIMIT 1) AS overture_confidence,
  COALESCE((SELECT array_agg(DISTINCT alias) FROM location_alias WHERE location_id = l.id), '{}') AS aliases,
  COALESCE((SELECT json_agg(DISTINCT jsonb_build_array(platform, platform_kind)) FROM online_profile WHERE location_id = l.id), '[]') AS platforms,
  COALESCE((SELECT array_agg(lc.verification_state) FROM location_contact lc JOIN contact_point cp ON cp.id = lc.contact_point_id
            WHERE lc.location_id = l.id AND lc.active AND cp.kind = 'phone'), '{}') AS phones,
  COALESCE((SELECT array_agg(lc.verification_state) FROM location_contact lc JOIN contact_point cp ON cp.id = lc.contact_point_id
            WHERE lc.location_id = l.id AND lc.active AND cp.kind = 'email'), '{}') AS emails,
  EXISTS (SELECT 1 FROM address a WHERE a.owner_type = 'location' AND a.owner_id = l.id AND a.is_operating_location
          AND a.zip5 IS NOT NULL AND a.line1 IS NOT NULL AND a.line1 !~* '^\\s*p\\.?\\s*o\\.?\\s*box') AS has_mail_address,
  COALESCE((SELECT array_agg(DISTINCT source_key) FROM location_source_link WHERE location_id = l.id AND active), '{}') AS sources,
  COALESCE((SELECT json_agg(json_build_object('filing_kind', o.filing_kind, 'registration_date', o.registration_date,
            'status', o.status, 'owner_registration_date',
            (SELECT min(ow.registration_date) FROM organization_link ol JOIN organization ow ON ow.id = ol.to_org_id
             WHERE ol.from_org_id = o.id AND ol.kind = 'dba_owned_by')))
            FROM location_organization lo JOIN organization o ON o.id = lo.organization_id
            WHERE lo.location_id = l.id AND lo.active), '[]') AS orgs,
  EXISTS (SELECT 1 FROM area a WHERE a.kind = 'county' AND a.is_target AND a.enabled AND a.spec->>'geoid' = l.county_fips) AS in_target,
  COALESCE((SELECT array_agg(DISTINCT f.value->>'licence_number') FROM fact f WHERE f.subject_type='location' AND f.subject_id=l.id
            AND f.field IN ('licence', 'btr') AND f.superseded_at IS NULL), '{}') AS licences
FROM location l
WHERE l.merged_into_id IS NULL AND l.county_fips = ANY(:fips)
ORDER BY l.id
"""


def _d(v):
    return date.fromisoformat(v) if isinstance(v, str) else v


def classify(category: str | None, hit_groups: set[str], industries: dict) -> str | None:
    cat_toks = set(tokens(category))
    best = None
    for key, p in industries["profiles"].items():
        if not p.get("enabled", True):
            continue
        tax = set(p.get("overture_taxonomy", []))
        rank = 0 if category in tax else 1 if cat_toks & {t for x in tax for t in tokens(x)} else \
            2 if hit_groups & set(p.get("keyword_groups", [])) else None
        if rank is None:
            continue
        # deterministic tie-break: more matching keyword groups first, then profile key (never user priority, F9)
        cand = (rank, -len(hit_groups & set(p.get("keyword_groups", []))), key)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


def score_all(s: Session, cfg, target_fips: list[str], *, today: date | None = None, batch: int = 5000) -> dict:
    today = today or date.today()
    now = datetime.now(timezone.utc)
    versions = {k: cfg.versions.get(k) for k in ("scoring", "keywords", "industries")}
    st = Counter()
    rows = s.execute(text(FEATURE_SQL), {"fips": target_fips}).mappings().all()
    current = dict(s.execute(text("SELECT location_id, inputs_hash FROM score WHERE kind='bos' AND superseded_at IS NULL "
                                  "AND location_id = ANY(:ids)"), {"ids": [r["id"] for r in rows]}).all())
    for i in range(0, len(rows), batch):
        _score_batch(s, rows[i:i + batch], cfg, versions, current, today, now, st)
        s.flush()
    return dict(st)


def _score_batch(s, rows, cfg, versions, current, today, now, st):
    scores, factors_by_key, hits_rows, quals, industry_updates, changed = [], {}, [], [], [], []
    for r in rows:
        fields = {"name": r["display_name"], "category": r["category"]}
        for i, a in enumerate(sorted(r["aliases"] or [])):
            fields[f"alias{i}"] = a
        hits, _ = match(fields, cfg["keywords"])
        excl = hard_excluded(fields, cfg["keywords"])
        industry = classify(r["category"], {h.group for h in hits if not h.neutralized}, cfg["industries"])
        f = Features(location_id=r["id"], name=r["display_name"], aliases=list(r["aliases"] or []), category=r["category"],
                     operating_status=r["operating_status"], chain_flag=r["chain_flag"], overture_confidence=r["overture_confidence"],
                     platforms=[tuple(p) for p in r["platforms"]], phones=list(r["phones"]), emails=list(r["emails"]),
                     has_mail_address=r["has_mail_address"], sources=set(r["sources"]),
                     orgs=[{k: _d(v) for k, v in o.items()} for o in r["orgs"]], in_target=r["in_target"],
                     last_seen=r["last_seen"], industry=industry, licences=list(r["licences"]))
        h = hashlib.sha256(json.dumps([asdict(f), [h.__dict__ for h in hits], excl, versions], default=str, sort_keys=True)
                           .encode()).hexdigest()
        if current.get(r["id"]) == h:
            st["unchanged"] += 1
            continue
        res = score(f, hits, cfg["scoring"], cfg["industries"], today)
        if excl:
            res.qualification, res.reasons = "not_qualified", [f"hard_excluded: {excl}"]
        changed.append(r["id"])
        industry_updates.append({"id": r["id"], "industry_profile": industry})
        for kind, value, pre, caps, fl in (("bos", res.bos, res.pre_cap, res.caps, res.factors),
                                           ("lps", res.lps, None, [], res.lps_factors)):
            key = (r["id"], kind)
            scores.append({"location_id": r["id"], "kind": kind, "stage": "provisional", "value": value, "pre_cap_value": pre,
                           "caps_applied": caps, "evidence_points": res.evidence_points, "confidence": res.confidence,
                           "config_versions": versions, "inputs_hash": h if kind == "bos" else None, "computed_at": now})
            factors_by_key[key] = fl
        for hh in hits:
            hits_rows.append({"location_id": r["id"], "group": hh.group, "polarity": hh.polarity, "term": hh.term,
                              "matched_text": hh.matched_text, "field": hh.field, "effect": hh.effect, "neutralized": hh.neutralized,
                              "config_version_id": versions.get("keywords"), "computed_at": now})
        quals.append({"location_id": r["id"], "status": res.qualification, "reasons": res.reasons, "stage": "provisional",
                      "computed_at": now})
        st[res.qualification] += 1
        st["scored"] += 1
    if not changed:
        return
    for table in ("score", "keyword_hit", "qualification"):
        s.execute(text(f"UPDATE {table} SET superseded_at = :now WHERE superseded_at IS NULL AND location_id = ANY(:ids)"),
                  {"now": now, "ids": changed})
    ids = s.execute(insert(Score).returning(Score.id, Score.location_id, Score.kind, sort_by_parameter_order=True), scores).all()
    frows = []
    for sid, lid, kind in ids:
        for x in factors_by_key[(lid, kind)]:
            frows.append({"score_id": sid, "component": x.component, "factor": x.factor, "effect": float(x.effect),
                          "evidence": json.loads(json.dumps(x.evidence, default=str)), "provenance": x.provenance})
    if frows:
        s.execute(insert(ScoreFactor), frows)
    if hits_rows:
        s.execute(insert(KeywordHit), hits_rows)
    s.execute(insert(Qualification), quals)
    s.execute(text("UPDATE location l SET industry_profile = u.ind FROM (SELECT unnest(CAST(:ids AS bigint[])) id, "
                   "unnest(CAST(:inds AS text[])) ind) u WHERE l.id = u.id"),
              {"ids": [u["id"] for u in industry_updates], "inds": [u["industry_profile"] for u in industry_updates]})
