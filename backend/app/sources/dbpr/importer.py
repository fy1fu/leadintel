"""DBPR licensee extracts -> licence evidence (PHASE1.md item 3.1).

Column meanings are INFERRED from the files (no layout is published on the public-records pages);
open verification item V13. Each row is validated; the county code is cross-checked against the
ZIP (ZCTA -> county) and disagreements are counted, not guessed away.

Licences never create prospects on their own (many licensees are individuals/employees). An active
licence becomes evidence for an existing location when the licensee or DBA name matches a location
name/alias (trigram >= 0.85) in the same ZIP. Ambiguous matches (two candidates) are skipped and counted.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Fact, LocationSourceLink, Source, SourceFile, SourceRecord
from app.normalize.core import name_norm, zip5
from app.sources.common import payload_hash

SOURCE_KEY = "dbpr"
# DBPR county codes appear to be alphabetical county order + 10 (Alachua=11 ... Duval=26 ... Clay=20, St. Johns=65)
DBPR_COUNTY_TO_FIPS = {"20": "12019", "26": "12031", "65": "12109"}
COLUMNS = ["board", "licence_type", "licensee_name", "dba_name", "unknown_4", "addr1", "addr2", "addr3", "city", "state",
           "zip", "county_code", "licence_number_short", "rank", "status", "original_licence_date", "status_date",
           "expiry_date", "unknown_18", "unknown_19", "licence_number", "ce_status"]


@dataclass
class DbprStats:
    rows: int = 0
    malformed: int = 0
    in_target: int = 0
    active: int = 0
    county_zip_mismatch: int = 0
    linked: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    by_type: Counter = field(default_factory=Counter)


def _date(v: str) -> date | None:
    try:
        return datetime.strptime(v.strip(), "%m/%d/%Y").date() if v and v.strip() else None
    except ValueError:
        return None


def import_extract(s: Session, src: Source, sf: SourceFile, blob: bytes, zip_counties: dict[str, set[str]],
                   target_fips: set[str], *, min_similarity: float = 0.85) -> DbprStats:
    st = DbprStats()
    reader = csv.reader(io.TextIOWrapper(io.BytesIO(blob), encoding="utf-8", errors="replace", newline=""))
    keep = []
    for line_no, row in enumerate(reader, start=1):
        st.rows += 1
        if len(row) != len(COLUMNS):
            st.malformed += 1
            continue
        d = dict(zip(COLUMNS, (c.strip() for c in row)))
        z = zip5(d["zip"])
        fips = DBPR_COUNTY_TO_FIPS.get(d["county_code"])
        zip_fips = zip_counties.get(z or "", set())
        if fips is None and not (zip_fips & target_fips):
            continue
        if fips and zip_fips and fips not in zip_fips:
            st.county_zip_mismatch += 1
        st.in_target += 1
        if d["status"] != "A" or not d["licence_number"]:
            continue
        exp = _date(d["expiry_date"])
        if exp and exp < date.today():
            continue
        st.active += 1
        st.by_type[d["licence_type"]] += 1
        d["zip5"], d["line_no"] = z, line_no
        keep.append(d)
    for d in keep:
        payload = {k: v for k, v in d.items() if k != "line_no"}
        s.execute(insert(SourceRecord).on_conflict_do_nothing(index_elements=["source_id", "record_kind", "external_id", "payload_hash"]),
                  [{"source_id": src.id, "record_kind": "licence", "external_id": d["licence_number"], "source_file_id": sf.id,
                    "line_no": d["line_no"], "payload": payload, "payload_hash": payload_hash(payload), "licence_tag": "public-record"}])
        rid = s.execute(text("SELECT id FROM source_record WHERE source_id=:s AND record_kind='licence' AND external_id=:e "
                             "AND payload_hash=:h"), {"s": src.id, "e": d["licence_number"], "h": payload_hash(payload)}).scalar()
        names = [n for n in {name_norm(d["dba_name"]), name_norm(d["licensee_name"])} if n]
        if not names or not d["zip5"]:
            st.unmatched += 1
            continue
        cands = s.execute(text("""
            SELECT l.id, max(greatest(similarity(l.name_norm, n), coalesce(similarity(al.alias_norm, n), 0))) sim
            FROM location l LEFT JOIN location_alias al ON al.location_id = l.id
            JOIN address a ON a.owner_type = 'location' AND a.owner_id = l.id AND a.zip5 = :z
            CROSS JOIN unnest(CAST(:names AS text[])) n
            WHERE l.merged_into_id IS NULL
            GROUP BY l.id HAVING max(greatest(similarity(l.name_norm, n), coalesce(similarity(al.alias_norm, n), 0))) >= :m
            ORDER BY sim DESC LIMIT 2"""), {"z": d["zip5"], "names": names, "m": min_similarity}).all()
        if not cands:
            st.unmatched += 1
            continue
        if len(cands) == 2 and cands[0].sim - cands[1].sim < 0.05:
            st.ambiguous += 1
            continue
        lid = cands[0].id
        s.execute(insert(Fact).on_conflict_do_nothing(index_elements=["subject_type", "subject_id", "field", "value_norm", "source_record_id"]),
                  [{"subject_type": "location", "subject_id": lid, "field": "licence",
                    "value": {"licence_number": d["licence_number"], "type": d["licence_type"], "status": "active",
                              "original": d["original_licence_date"], "expires": d["expiry_date"], "name_similarity": round(cands[0].sim, 3)},
                    "value_norm": d["licence_number"], "source_record_id": rid, "source_key": SOURCE_KEY, "provenance": "registry",
                    "verification_state": "ingested", "confidence": round(0.9 * cands[0].sim, 3)}])
        exists = s.execute(text("SELECT 1 FROM location_source_link WHERE location_id=:l AND source_key='dbpr' AND external_id=:e"),
                           {"l": lid, "e": d["licence_number"]}).first()
        if not exists:
            s.add(LocationSourceLink(location_id=lid, source_key=SOURCE_KEY, external_id=d["licence_number"],
                                     match_method="licence_name_zip", match_score=float(cands[0].sim)))
        st.linked += 1
    sf.records, sf.status = st.in_target, "imported"
    return st
