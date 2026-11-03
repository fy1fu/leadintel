"""Generic manual CSV import of county business tax receipts (PHASE1.md item 3.2; decision D11).

Works for any county: a small mapping says which CSV column holds which field. The import
validates first and reports problems instead of guessing:
  * missing mapped columns -> the whole file is rejected with the list of missing columns;
  * rows lacking a required value or with an unparseable date -> rejected rows (reported);
  * expired receipts are kept as records but do not count as current evidence.
Current receipts attach to a matching location (name/alias trigram >= 0.85, same ZIP) as `btr`
evidence; unmatched current receipts create a registry location (the county is known from the file).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Address, Fact, Location, LocationAlias, LocationSourceLink, Source, SourceFile, SourceRecord
from app.normalize.core import address_key, name_norm, zip5
from app.sources.common import payload_hash

REQUIRED = ("business_name", "address", "zip", "receipt_number")
OPTIONAL = ("dba_name", "city", "issue_date", "expiry_date", "occupation", "phone")
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")


class MappingError(ValueError):
    pass


@dataclass
class BtrStats:
    rows: int = 0
    rejected: list[dict] = field(default_factory=list)
    current: int = 0
    expired: int = 0
    linked: int = 0
    created: int = 0


def _date(v: str | None) -> date | None:
    if not v or not v.strip():
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised date {v!r}")


def validate_mapping(header: list[str], mapping: dict[str, str]) -> None:
    missing_keys = [k for k in REQUIRED if k not in mapping]
    if missing_keys:
        raise MappingError(f"mapping lacks required fields: {missing_keys}")
    absent = [c for k, c in mapping.items() if c not in header]
    if absent:
        raise MappingError(f"columns not found in CSV header: {absent} (header: {header})")


def import_btr_csv(s: Session, src: Source, sf: SourceFile, blob: bytes, mapping: dict[str, str], county_fips: str,
                   *, today: date | None = None, min_similarity: float = 0.85) -> BtrStats:
    today = today or date.today()
    reader = csv.DictReader(io.StringIO(blob.decode("utf-8-sig", errors="replace")))
    validate_mapping(reader.fieldnames or [], mapping)
    st = BtrStats()
    for line_no, raw in enumerate(reader, start=2):
        st.rows += 1
        d = {k: (raw.get(c) or "").strip() for k, c in mapping.items()}
        miss = [k for k in REQUIRED if not d.get(k)]
        try:
            issued, expires = _date(d.get("issue_date")), _date(d.get("expiry_date"))
        except ValueError as exc:
            st.rejected.append({"line": line_no, "reason": str(exc)})
            continue
        if miss:
            st.rejected.append({"line": line_no, "reason": f"missing required values: {miss}"})
            continue
        payload = {**d, "county_fips": county_fips}
        s.execute(insert(SourceRecord).on_conflict_do_nothing(index_elements=["source_id", "record_kind", "external_id", "payload_hash"]),
                  [{"source_id": src.id, "record_kind": "btr", "external_id": d["receipt_number"], "source_file_id": sf.id,
                    "line_no": line_no, "payload": payload, "payload_hash": payload_hash(payload), "licence_tag": "public-record"}])
        rid = s.execute(text("SELECT id FROM source_record WHERE source_id=:s AND record_kind='btr' AND external_id=:e AND payload_hash=:h"),
                        {"s": src.id, "e": d["receipt_number"], "h": payload_hash(payload)}).scalar()
        if expires and expires < today:
            st.expired += 1
            continue
        st.current += 1
        z = zip5(d["zip"])
        names = [n for n in {name_norm(d.get("dba_name")), name_norm(d["business_name"])} if n]
        cand = s.execute(text("""
            SELECT l.id, max(greatest(similarity(l.name_norm, n), coalesce(similarity(al.alias_norm, n), 0))) sim
            FROM location l LEFT JOIN location_alias al ON al.location_id = l.id
            JOIN address a ON a.owner_type='location' AND a.owner_id=l.id AND a.zip5 = :z
            CROSS JOIN unnest(CAST(:names AS text[])) n
            WHERE l.merged_into_id IS NULL GROUP BY l.id
            HAVING max(greatest(similarity(l.name_norm, n), coalesce(similarity(al.alias_norm, n), 0))) >= :m
            ORDER BY sim DESC LIMIT 1"""), {"z": z, "names": names, "m": min_similarity}).first()
        if cand:
            lid = cand.id
            st.linked += 1
        else:
            display = d.get("dba_name") or d["business_name"]
            loc = Location(display_name=display, name_norm=name_norm(display), county_fips=county_fips, operating_status="registered")
            s.add(loc)
            s.flush()
            lid = loc.id
            s.add(Address(owner_type="location", owner_id=lid, role="principal", line1=d["address"], city=d.get("city") or None,
                          state="FL", zip5=z, country="US", norm_key=address_key(d["address"], None, d["zip"]) or f"raw:{d['address']}|{z}",
                          county_fips=county_fips, geocode_method="source_county", is_operating_location=True, source_record_id=rid))
            for nm, kind in ((d["business_name"], "legal"), (d.get("dba_name"), "dba")):
                if nm:
                    s.execute(insert(LocationAlias).on_conflict_do_nothing(index_elements=["location_id", "alias_norm", "kind"]),
                              [{"location_id": lid, "alias": nm, "alias_norm": name_norm(nm), "kind": kind, "source_record_id": rid}])
            st.created += 1
        s.execute(insert(Fact).on_conflict_do_nothing(index_elements=["subject_type", "subject_id", "field", "value_norm", "source_record_id"]),
                  [{"subject_type": "location", "subject_id": lid, "field": "btr",
                    "value": {"licence_number": d["receipt_number"], "issued": d.get("issue_date"), "expires": d.get("expiry_date"),
                              "occupation": d.get("occupation")},
                    "value_norm": d["receipt_number"], "source_record_id": rid, "source_key": src.key, "provenance": "registry",
                    "verification_state": "ingested", "confidence": 0.85}])
        if not s.execute(text("SELECT 1 FROM location_source_link WHERE location_id=:l AND source_key=:k AND external_id=:e"),
                         {"l": lid, "k": src.key, "e": d["receipt_number"]}).first():
            s.add(LocationSourceLink(location_id=lid, source_key=src.key, external_id=d["receipt_number"],
                                     match_method="btr_name_zip" if cand else "btr_new", match_score=float(cand.sim) if cand else 1.0))
    sf.records, sf.quarantined, sf.status = st.rows - len(st.rejected), len(st.rejected), "imported"
    return st
