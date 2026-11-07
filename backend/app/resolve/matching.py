"""Entity resolution v1 (docs/PHASE1.md "Relationship rules").

* Duplicate listings: same normalized phone within 150 m AND near-identical names (trigram >= 0.8)
  -> automatic merge. Same phone with partly similar names (0.5-0.8) -> review queue. Same phone
  with dissimilar names is co-location (a practice's practitioners, a counter inside a store), not a
  duplicate, and is left alone. Name similarity >= 0.9 within 75 m without a shared phone -> review
  queue (never automatic).  Validated on real Overture data (see docs/PHASE1_PROGRESS.md).
  Shared addresses or similar names alone never merge.
* Registry placement: organizations with an operating principal address in a target county are
  geocoded; a strong unique match to an existing listing links the organization to that location,
  an ambiguous one creates a registry location plus a review-queue merge proposal, no match creates
  a registry-only location. DBA locations also link to the DBA's owning entity.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (Address, Fact, Location, LocationAlias, LocationOrganization, LocationSourceLink, Organization,
                           ReviewQueue)
from app.geo.geocode import CensusGeocoder
from app.resolve.merge import merge

PHONE_RADIUS_M = 150
AUTO_MERGE_SIM = 0.8
REVIEW_MIN_SIM = 0.5
DEG_PER_M = 1 / 90_000  # generous degree pre-filter (index-assisted), exact metres checked after
NAME_RADIUS_M = 75
LINK_RADIUS_M = 250


def _queue(s: Session, kind: str, a: int, b: int, score: float, evidence: dict) -> None:
    s.execute(insert(ReviewQueue).on_conflict_do_nothing(index_elements=["kind", "subject_a", "subject_b"]),
              [{"kind": kind, "subject_a": a, "subject_b": b, "score": score, "evidence": evidence}])


@dataclass
class DedupeStats:
    auto_merged: int = 0
    queued: int = 0
    colocated: int = 0


def dedupe_locations(s: Session, county_fips: list[str] | None = None) -> DedupeStats:
    st = DedupeStats()
    pairs = s.execute(text(f"""
        SELECT l1.id, l2.id, similarity(l1.name_norm, l2.name_norm) sim, cp.value_norm,
               ST_Distance(l1.geom::geography, l2.geom::geography) dist
        FROM location_contact c1 JOIN location_contact c2 ON c1.contact_point_id = c2.contact_point_id AND c1.location_id < c2.location_id
        JOIN contact_point cp ON cp.id = c1.contact_point_id AND cp.kind = 'phone'
        JOIN location l1 ON l1.id = c1.location_id AND l1.merged_into_id IS NULL
        JOIN location l2 ON l2.id = c2.location_id AND l2.merged_into_id IS NULL
        WHERE c1.active AND c2.active AND ST_DWithin(l1.geom, l2.geom, {PHONE_RADIUS_M * DEG_PER_M * 1.5})
          AND ST_DWithin(l1.geom::geography, l2.geom::geography, {PHONE_RADIUS_M})
          {"AND l1.county_fips = ANY(:c)" if county_fips else ""}
        ORDER BY l1.id, l2.id"""), {"c": county_fips}).all()
    gone: set[int] = set()
    for a, b, sim, phone, dist in pairs:
        if a in gone or b in gone:
            continue
        ev = {"phone": phone, "distance_m": round(dist, 1), "name_similarity": round(sim, 3)}
        if sim >= AUTO_MERGE_SIM:
            merge(s, a, b, method="same_phone_nearby", evidence=ev)
            gone.add(b)
            st.auto_merged += 1
        elif sim >= REVIEW_MIN_SIM:
            _queue(s, "location_merge", a, b, float(sim), ev | {"reason": "same phone, partly similar names"})
            st.queued += 1
        else:
            st.colocated += 1
    names = s.execute(text(f"""
        SELECT l1.id, l2.id, similarity(l1.name_norm, l2.name_norm) sim, ST_Distance(l1.geom::geography, l2.geom::geography)
        FROM location l1 JOIN location l2 ON l1.id < l2.id AND l2.merged_into_id IS NULL
          AND ST_DWithin(l1.geom, l2.geom, {NAME_RADIUS_M * DEG_PER_M * 1.5})
          AND ST_DWithin(l1.geom::geography, l2.geom::geography, {NAME_RADIUS_M})
        WHERE l1.merged_into_id IS NULL AND l1.name_norm <> '' {"AND l1.county_fips = ANY(:c)" if county_fips else ""}
          AND similarity(l1.name_norm, l2.name_norm) >= 0.9"""), {"c": county_fips}).all()
    for a, b, sim, dist in names:
        if a in gone or b in gone:
            continue
        _queue(s, "location_merge", a, b, float(sim), {"name_similarity": round(sim, 3), "distance_m": round(dist, 1),
                                                       "reason": "very similar names nearby, no shared phone"})
        st.queued += 1
    return st


@dataclass
class PlacementStats:
    candidates: int = 0
    out_of_area: int = 0
    unplaceable: int = 0
    linked_to_listing: int = 0
    registry_only: int = 0
    queued: int = 0
    owner_links: int = 0
    methods: Counter = field(default_factory=Counter)


def place_registry_orgs(s: Session, geocoder: CensusGeocoder, zip_counties: dict[str, set[str]], target_fips: set[str],
                        reliability: dict) -> PlacementStats:
    st = PlacementStats()
    target_zips = {z for z, cs in zip_counties.items() if cs & target_fips}
    rows = s.execute(select(Organization, Address).join(Address, (Address.owner_type == "organization") &
                                                        (Address.owner_id == Organization.id) & (Address.role == "principal"))
                     .where(Address.is_operating_location.is_(True), Organization.status == "active",
                            Address.zip5.in_(target_zips),
                            ~select(LocationSourceLink.id).where(LocationSourceLink.source_key == "sunbiz",
                                                                 LocationSourceLink.external_id == Organization.document_number).exists())
                     ).all()
    st.candidates = len(rows)
    items = {a.norm_key + "|" + (a.city or "").upper(): (" ".join(p for p in (a.line1, a.line2) if p), a.city, a.state or "FL", a.zip5)
             for _, a in rows}
    geo = geocoder.geocode(s, items) if items else {}
    for org, a in rows:
        g = geo[a.norm_key + "|" + (a.city or "").upper()]
        if g.matched:
            county, method = g.county_fips, "census_match"
            a.geom, a.county_fips, a.geocode_method = f"SRID=4326;POINT({g.lon} {g.lat})", county, method
        else:
            cs = zip_counties.get(a.zip5 or "", set())
            if len(cs) == 1 and cs <= target_fips:
                county, method = next(iter(cs)), "zip_county"
                a.county_fips, a.geocode_method = county, method
            else:
                st.unplaceable += 1
                continue
        if county not in target_fips:
            st.out_of_area += 1
            continue
        st.methods[method] += 1
        _place_one(s, org, a, g if g.matched else None, county, method, reliability, st)
    # DBA locations also belong to the DBA's owning entity
    res = s.execute(text("""INSERT INTO location_organization (location_id, organization_id, role, evidence, confidence, active, created_at)
        SELECT lo.location_id, l.to_org_id, 'legal_owner', jsonb_build_object('via', 'dba_owned_by', 'link_id', l.id), l.confidence, true, now()
        FROM location_organization lo JOIN organization_link l ON l.from_org_id = lo.organization_id AND l.kind='dba_owned_by'
        WHERE lo.role='trade_name' AND lo.active AND l.to_org_id IS NOT NULL
        ON CONFLICT (location_id, organization_id, role) DO NOTHING"""))
    st.owner_links = res.rowcount or 0
    return st


def _place_one(s, org: Organization, a: Address, g, county: str, method: str, rel: dict, st: PlacementStats) -> None:
    role = "trade_name" if org.filing_kind == "fictitious_name" else "legal_owner"
    best = []
    if g is not None:
        best = s.execute(text(f"""
            SELECT l.id, greatest(similarity(l.name_norm, :n), coalesce(max(similarity(al.alias_norm, :n)), 0)) sim,
                   ST_Distance(l.geom::geography, ST_SetSRID(ST_MakePoint(:x, :y), 4326)::geography) d
            FROM location l LEFT JOIN location_alias al ON al.location_id = l.id
            WHERE l.merged_into_id IS NULL AND l.geom IS NOT NULL
              AND ST_DWithin(l.geom, ST_SetSRID(ST_MakePoint(:x, :y), 4326), {LINK_RADIUS_M * DEG_PER_M * 1.5})
              AND ST_DWithin(l.geom::geography, ST_SetSRID(ST_MakePoint(:x, :y), 4326)::geography, {LINK_RADIUS_M})
            GROUP BY l.id ORDER BY sim DESC LIMIT 3"""), {"n": org.name_norm, "x": g.lon, "y": g.lat}).all()
    top = best[0] if best else None
    second = best[1] if len(best) > 1 else None
    evidence = {"document_number": org.document_number, "geocode_method": method}
    if top and top.sim >= 0.85 and (second is None or top.sim - second.sim >= 0.1):
        _link(s, top.id, org, role, evidence | {"name_similarity": round(top.sim, 3), "distance_m": round(top.d, 1)}, top.sim)
        st.linked_to_listing += 1
        return
    loc = Location(display_name=org.name, name_norm=org.name_norm, county_fips=county, operating_status="registered",
                   geom=f"SRID=4326;POINT({g.lon} {g.lat})" if g is not None else None)
    s.add(loc)
    s.flush()
    _link(s, loc.id, org, role, evidence, 1.0)
    s.add(Address(owner_type="location", owner_id=loc.id, role="principal", line1=a.line1, line2=a.line2, city=a.city,
                  state=a.state or "FL", zip5=a.zip5, country="US", norm_key=a.norm_key, geom=a.geom, county_fips=county,
                  geocode_method=method, is_operating_location=True, source_record_id=a.source_record_id))
    s.add(Fact(subject_type="location", subject_id=loc.id, field="name", value={"v": org.name}, value_norm=org.name_norm,
               source_record_id=org.latest_source_record_id, source_key="sunbiz", provenance="registry",
               verification_state="ingested", confidence=float(rel.get("name", 0.9))))
    st.registry_only += 1
    if top and top.sim >= 0.6:
        _queue(s, "location_merge", top.id, loc.id, float(top.sim),
               evidence | {"name_similarity": round(top.sim, 3), "distance_m": round(top.d, 1),
                           "reason": "registry filing near a similar listing"})
        st.queued += 1


def _link(s: Session, location_id: int, org: Organization, role: str, evidence: dict, conf: float) -> None:
    s.execute(insert(LocationOrganization).on_conflict_do_nothing(index_elements=["location_id", "organization_id", "role"]),
              [{"location_id": location_id, "organization_id": org.id, "role": role, "evidence": evidence, "confidence": conf}])
    s.execute(insert(LocationSourceLink), [{"location_id": location_id, "source_key": "sunbiz", "external_id": org.document_number,
                                            "match_method": "registry_placement", "match_score": conf}])
    s.execute(insert(LocationAlias).on_conflict_do_nothing(index_elements=["location_id", "alias_norm", "kind"]),
              [{"location_id": location_id, "alias": org.name, "alias_norm": org.name_norm,
                "kind": "dba" if role == "trade_name" else "legal", "source_record_id": org.latest_source_record_id}])
