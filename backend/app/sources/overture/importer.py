"""Import an Overture snapshot into the business DB (PHASE1.md item 1.2).

* Only places inside enabled target areas become locations; the rest are counted, not stored.
* One append-only `source_record` per place version; facts start as `ingested`.
* Idempotent: identity is `location_source_link(source_key='overture', external_id=<GERS id>)`.
* Closed places are kept with their status, never silently dropped.
* Empty-string websites are dropped; social / link-hub / listing URLs never count as websites.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import pyarrow.parquet as pq
from shapely import wkb
from shapely.geometry import Point
from shapely.prepared import prep
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (Address, Area, ContactPoint, Fact, Location, LocationAlias, LocationContact,
                           LocationSourceLink, OnlineProfile, Source, SourceRecord)
from app.normalize.core import address_key, classify_url, email_norm, name_norm, phone_e164, zip5
from app.sources.common import ensure_source, payload_hash
from app.sources.overture.snapshot import Snapshot

SOURCE_KEY = "overture"
BATCH = 2000


@dataclass
class ImportStats:
    rows_in_snapshot: int = 0
    in_target_areas: int = 0
    outside_targets: int = 0
    new_locations: int = 0
    updated_locations: int = 0
    unchanged: int = 0
    closed: int = 0
    per_area: Counter = field(default_factory=Counter)
    websites_dropped_empty: int = 0
    social_or_listing_urls: int = 0


def _area_index(s: Session) -> list[tuple[str, str, bool, object]]:
    rows = s.execute(text("SELECT key, spec->>'geoid', is_target AND enabled, ST_AsBinary(geom) FROM area WHERE kind='county'")).all()
    return [(k, g, t, prep(wkb.loads(bytes(b)))) for k, g, t, b in rows]


def _first(lst) -> str | None:
    for v in lst or []:
        if v and str(v).strip():
            return str(v).strip()
    return None


def normalize_place(r: dict) -> dict:
    """Pure normalization of one Overture row into a JSON-safe payload."""
    names = r.get("names") or {}
    tax = r.get("taxonomy") or {}
    addr = (r.get("addresses") or [{}])[0] or {}
    brand = r.get("brand") or {}
    point = wkb.loads(bytes(r["geometry"])) if r.get("geometry") else None
    urls, dropped = [], 0
    for u in (r.get("websites") or []) + (r.get("socials") or []):
        if u is None or not str(u).strip():
            dropped += 1
            continue
        c = classify_url(u)
        if c:
            urls.append({"url": str(u).strip(), "url_norm": c.url_norm, "platform": c.platform,
                         "platform_kind": c.platform_kind, "unbranded_domain": c.unbranded_domain})
    return {
        "id": r["id"],
        "name": names.get("primary") or "",
        "category": tax.get("primary") or r.get("basic_category"),
        "basic_category": r.get("basic_category"),
        "taxonomy_hierarchy": list(tax.get("hierarchy") or []),
        "confidence": r.get("confidence"),
        "operating_status": r.get("operating_status") or "unknown",
        "phones": sorted({p for p in (phone_e164(x) for x in r.get("phones") or []) if p}),
        "emails": sorted({e for e in (email_norm(x) for x in r.get("emails") or []) if e}),
        "urls": urls,
        "brand_name": ((brand.get("names") or {}).get("primary") or None),
        "brand_wikidata": brand.get("wikidata"),
        "address": {"freeform": addr.get("freeform"), "locality": addr.get("locality"),
                    "postcode": addr.get("postcode"), "region": addr.get("region"), "country": addr.get("country")},
        "lon": point.x if point is not None else None,
        "lat": point.y if point is not None else None,
        "contributors": sorted({sv.get("dataset") for sv in r.get("sources") or [] if sv.get("dataset")}),
        "licences": sorted({sv.get("license") for sv in r.get("sources") or [] if sv.get("license")}),
        "release_version": r.get("version"),
        "_dropped_empty_urls": dropped,
    }


def _batches(snapshot: Snapshot, size: int = BATCH) -> Iterable[list[dict]]:
    pf = pq.ParquetFile(snapshot.path)
    for b in pf.iter_batches(batch_size=size):
        yield b.to_pylist()


def import_snapshot(s: Session, snapshot: Snapshot, sources_cfg: dict, *, now: datetime | None = None) -> ImportStats:
    now = now or datetime.now(timezone.utc)
    src = ensure_source(s, SOURCE_KEY, sources_cfg["sources"][SOURCE_KEY])
    rel = src.reliability or {}
    areas = _area_index(s)
    st = ImportStats()
    for batch in _batches(snapshot):
        st.rows_in_snapshot += len(batch)
        keep = []
        for r in batch:
            p = normalize_place(r)
            if p["lon"] is None:
                st.outside_targets += 1
                continue
            pt = Point(p["lon"], p["lat"])
            hit = next(((k, g, t) for k, g, t, geom in areas if geom.contains(pt)), None)
            if hit is None or not hit[2]:
                st.outside_targets += 1
                continue
            p["county_fips"], p["area_key"] = hit[1], hit[0]
            st.websites_dropped_empty += p.pop("_dropped_empty_urls")
            st.social_or_listing_urls += sum(1 for u in p["urls"] if u["platform_kind"] != "independent")
            keep.append(p)
        if keep:
            _import_batch(s, src, rel, keep, snapshot, st, now)
    return st


def _import_batch(s: Session, src: Source, rel: dict, rows: list[dict], snapshot: Snapshot, st: ImportStats,
                  now: datetime) -> None:
    # 1. append-only source records
    recs = [{"source_id": src.id, "record_kind": "place", "external_id": p["id"], "payload": p,
             "payload_hash": payload_hash(p), "licence_tag": ";".join(p["licences"])[:60] or None} for p in rows]
    s.execute(insert(SourceRecord).on_conflict_do_nothing(
        index_elements=["source_id", "record_kind", "external_id", "payload_hash"]), recs)
    ids = {(e, h): i for i, e, h in s.execute(
        select(SourceRecord.id, SourceRecord.external_id, SourceRecord.payload_hash).where(
            SourceRecord.source_id == src.id, SourceRecord.record_kind == "place",
            SourceRecord.external_id.in_([p["id"] for p in rows])))}
    # 2. resolve existing locations (follow merges to the survivor)
    links = dict(s.execute(select(LocationSourceLink.external_id, LocationSourceLink.location_id).where(
        LocationSourceLink.source_key == SOURCE_KEY, LocationSourceLink.active.is_(True),
        LocationSourceLink.external_id.in_([p["id"] for p in rows]))).all())
    merged = dict(s.execute(select(Location.id, Location.merged_into_id).where(
        Location.id.in_(list(links.values())), Location.merged_into_id.is_not(None))).all()) if links else {}
    for k, v in list(links.items()):
        while v in merged:
            v = merged[v]
        links[k] = v
    new_rows = [p for p in rows if p["id"] not in links]
    if new_rows:
        created = s.execute(insert(Location).returning(Location.id, sort_by_parameter_order=True), [
            {"display_name": p["name"] or "(unnamed)", "name_norm": name_norm(p["name"]),
             "geom": f"SRID=4326;POINT({p['lon']} {p['lat']})", "county_fips": p["county_fips"],
             "category": p["category"], "chain_flag": bool(p["brand_wikidata"]), "brand_wikidata": p["brand_wikidata"],
             "operating_status": p["operating_status"], "first_seen_at": now, "last_seen_at": now}
            for p in new_rows]).scalars().all()
        for p, lid in zip(new_rows, created):
            links[p["id"]] = lid
        s.execute(insert(LocationSourceLink), [
            {"location_id": links[p["id"]], "source_key": SOURCE_KEY, "external_id": p["id"],
             "match_method": "source_identity", "match_score": 1.0} for p in new_rows])
        st.new_locations += len(new_rows)
    existing = [p for p in rows if p not in new_rows]
    if existing:  # keep current listing attributes on the location row (ORM bulk update by primary key)
        s.execute(update(Location), [
            {"id": links[p["id"]], "display_name": p["name"] or "(unnamed)", "category": p["category"],
             "operating_status": p["operating_status"], "brand_wikidata": p["brand_wikidata"],
             "chain_flag": bool(p["brand_wikidata"]), "last_seen_at": now} for p in existing])
    # 3. child rows (all idempotent)
    aliases, addrs, profiles, facts, cps, lcs = [], [], [], [], [], []
    for p in rows:
        lid, rid = links[p["id"]], ids[(p["id"], payload_hash(p))]
        st.per_area[p["area_key"]] += 1
        if p["operating_status"] == "permanently_closed":
            st.closed += 1
        if p["name"]:
            aliases.append({"location_id": lid, "alias": p["name"], "alias_norm": name_norm(p["name"]), "kind": "listing",
                            "source_record_id": rid})
        a = p["address"]
        nk = address_key(a.get("freeform"), None, a.get("postcode"))
        addrs.append({"owner_type": "location", "owner_id": lid, "role": "listing", "line1": a.get("freeform"),
                      "city": a.get("locality"), "state": (a.get("region") or "")[:2] or None, "zip5": zip5(a.get("postcode")),
                      "country": (a.get("country") or "")[:2] or None, "norm_key": nk or f"point:{p['lon']:.5f},{p['lat']:.5f}",
                      "geom": f"SRID=4326;POINT({p['lon']} {p['lat']})", "county_fips": p["county_fips"],
                      "geocode_method": "source_point", "is_operating_location": True, "source_record_id": rid})
        conf = float(p["confidence"] or 0.5)

        def fact(field_, value, norm, rel_key):
            facts.append({"subject_type": "location", "subject_id": lid, "field": field_, "value": {"v": value},
                          "value_norm": norm, "source_record_id": rid, "source_key": SOURCE_KEY,
                          "provenance": "directory", "verification_state": "ingested",
                          "confidence": round(conf * float(rel.get(rel_key, 0.5)), 3), "observed_at": now})
        fact("name", p["name"], name_norm(p["name"]), "name")
        if p["category"]:
            fact("category", p["category"], p["category"], "category")
        fact("operating_status", p["operating_status"], p["operating_status"], "operating_status")
        for ph in p["phones"]:
            fact("phone", ph, ph, "phone")
            cps.append({"kind": "phone", "value_norm": ph, "display": ph})
            lcs.append((lid, "phone", ph, rid))
        for em in p["emails"]:
            fact("email", em, em, "email")
            cps.append({"kind": "email", "value_norm": em, "display": em})
            lcs.append((lid, "email", em, rid))
        for u in p["urls"]:
            kind = "website" if u["platform_kind"] == "independent" else "online_profile"
            fact(kind, u["url"], u["url_norm"], "website" if kind == "website" else "social")
            profiles.append({"location_id": lid, "url": u["url"], "url_norm": u["url_norm"], "platform": u["platform"],
                             "platform_kind": u["platform_kind"], "source_key": SOURCE_KEY, "source_record_id": rid})
    # executemany form: SQLAlchemy pages the rows (Postgres allows at most 65,535 bind parameters per statement)
    if aliases:
        s.execute(insert(LocationAlias).on_conflict_do_nothing(index_elements=["location_id", "alias_norm", "kind"]), aliases)
    if addrs:
        s.execute(insert(Address).on_conflict_do_nothing(index_elements=["owner_type", "owner_id", "role", "norm_key"]), addrs)
    if profiles:
        s.execute(insert(OnlineProfile).on_conflict_do_nothing(index_elements=["location_id", "url_norm"]), profiles)
    if facts:
        s.execute(insert(Fact).on_conflict_do_nothing(
            index_elements=["subject_type", "subject_id", "field", "value_norm", "source_record_id"]), facts)
    if cps:
        s.execute(insert(ContactPoint).on_conflict_do_nothing(index_elements=["kind", "value_norm"]), cps)
        cp_ids = {(k, v): i for i, k, v in s.execute(select(ContactPoint.id, ContactPoint.kind, ContactPoint.value_norm).where(
            ContactPoint.value_norm.in_([c["value_norm"] for c in cps])))}
        s.execute(insert(LocationContact).on_conflict_do_nothing(index_elements=["location_id", "contact_point_id", "source_key"]), [
            {"location_id": lid, "contact_point_id": cp_ids[(k, v)], "source_key": SOURCE_KEY, "source_record_id": rid}
            for lid, k, v, rid in lcs])
    # 4. supersede facts from older versions of the same place (history kept, current set explicit)
    current = [{"lid": links[p["id"]], "rid": ids[(p["id"], payload_hash(p))]} for p in rows]
    s.execute(text("""UPDATE fact f SET superseded_at = :now FROM (SELECT unnest(CAST(:lids AS bigint[])) lid,
                      unnest(CAST(:rids AS bigint[])) rid) c
                      WHERE f.subject_type='location' AND f.subject_id=c.lid AND f.source_key=:sk
                        AND f.superseded_at IS NULL AND f.source_record_id <> c.rid"""),
              {"now": now, "sk": SOURCE_KEY, "lids": [c["lid"] for c in current], "rids": [c["rid"] for c in current]})
    st.in_target_areas += len(rows)
    st.updated_locations += len(existing)
