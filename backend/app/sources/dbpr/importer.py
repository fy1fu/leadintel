"""DBPR licensee extracts -> licence evidence (PHASE1.md item 3.1).

Column meanings are INFERRED from the files (no layout is published on the public-records pages);
open verification item V13. Each row is validated; the county code is cross-checked against the
ZIP (ZCTA -> county) and disagreements are counted, not guessed away.

Licences never create prospects on their own (many licensees are individuals/employees). An active
licence becomes evidence for an existing location when the licensee or DBA name matches a location
name/alias (trigram >= 0.85) in the same ZIP. Ambiguous matches (two candidates) are skipped and counted.

Validity rules (Fix 2; no official layout or rules are published - V13):
* Only status `A` with a VALID, non-past expiry date and a licence number is active evidence.
* A blank expiry is not active evidence: no published DBPR rule says a blank expiry means "active".
  (Real data: blank expiry with status A occurs on `QB` rows, which also lack licence numbers.)
* A malformed or impossible non-blank expiry date, an unrecognised status code, or a row with the
  wrong number of columns is QUARANTINED with its raw line and reason - never evidence.
* Blank status (very common for QB/FRO/CRS1/PVDR rows) is counted, not quarantined.
* Geography (Fix 3, `resolve_geo`): the county code and the ZIP's ZCTA counties must not conflict.
  agree -> that county; code known + ZIP absent/unknown -> code's county (flagged); code unknown + ZIP in
  exactly one county -> that county (flagged); conflict, ambiguous ZIP, out-of-state or nothing usable ->
  NO county, so the row can never count toward or attach evidence in Duval / St. Johns / Clay. Matching is
  restricted to locations in the resolved county. Raw values and the decision are kept in the payload.
* Evidence created by earlier imports of the same file whose row is no longer accepted is superseded
  (history kept) and its source link deactivated, so it cannot keep inflating scores.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Fact, LocationSourceLink, QuarantineRecord, Source, SourceFile, SourceRecord
from app.normalize.core import name_norm, zip5
from app.sources.common import payload_hash

SOURCE_KEY = "dbpr"
# bump when the acceptance rules change: files imported under another version are re-imported once
PARSER_VERSION = "dbpr-3"   # 3 = validity rules (Fix 2) + geographic policy (Fix 3)
# V13 - INFERRED, NOT OFFICIALLY VERIFIED. No authoritative DBPR code list has been found. Until one is, every
# county derived from a code alone is flagged inferred, and a code is only trusted when the ZIP agrees.
COUNTY_CODE_MAP_STATUS = "inferred_v13"
# Inferred rule: Florida counties in alphabetical order
# (Dade under D) numbered from 11. Census FIPS uses the same order in odd numbers, except Miami-Dade = 086.
# Checked against the 2026-11 extracts: for all 67 codes, 99.2% of 626k rows have a ZIP whose ZCTA lies
# in the predicted county (lowest per code 88.8%, small counties with border ZIPs). 7xx codes are
# out-of-state addresses (GA, TX, NC, ...); blank codes are mostly out-of-state as well.
_FL_COUNTIES = ("Alachua Baker Bay Bradford Brevard Broward Calhoun Charlotte Citrus Clay Collier Columbia Dade DeSoto "
                "Dixie Duval Escambia Flagler Franklin Gadsden Gilchrist Glades Gulf Hamilton Hardee Hendry Hernando "
                "Highlands Hillsborough Holmes IndianRiver Jackson Jefferson Lafayette Lake Lee Leon Levy Liberty Madison "
                "Manatee Marion Martin Monroe Nassau Okaloosa Okeechobee Orange Osceola PalmBeach Pasco Pinellas Polk "
                "Putnam StJohns StLucie SantaRosa Sarasota Seminole Sumter Suwannee Taylor Union Volusia Wakulla Walton "
                "Washington").split()
DBPR_COUNTY_TO_FIPS = {str(11 + i): ("12086" if n == "Dade" else f"12{2 * i + 1:03d}") for i, n in enumerate(_FL_COUNTIES)}
assert len(DBPR_COUNTY_TO_FIPS) == 67 and DBPR_COUNTY_TO_FIPS["26"] == "12031" and DBPR_COUNTY_TO_FIPS["20"] == "12019" \
    and DBPR_COUNTY_TO_FIPS["65"] == "12109" and DBPR_COUNTY_TO_FIPS["77"] == "12133"
COLUMNS = ["board", "licence_type", "licensee_name", "dba_name", "unknown_4", "addr1", "addr2", "addr3", "city", "state",
           "zip", "county_code", "licence_number_short", "rank", "status", "original_licence_date", "status_date",
           "expiry_date", "unknown_18", "unknown_19", "licence_number", "ce_status"]


@dataclass
class DbprStats:
    rows: int = 0
    malformed: int = 0              # wrong column count (quarantined)
    quarantined: int = 0            # all quarantined rows (malformed columns, bad dates, unrecognised status)
    status_blank: int = 0
    status_inactive: int = 0
    no_licence_number: int = 0
    expiry_blank: int = 0
    expired: int = 0
    evidence_superseded: int = 0
    in_target: int = 0
    active: int = 0
    county_zip_mismatch: int = 0    # code vs ZIP conflicts touching a target county (excluded)
    geo: Counter = field(default_factory=Counter)   # resolve_geo status counts for target-relevant rows
    linked: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    by_type: Counter = field(default_factory=Counter)


KNOWN_STATUS = {"A": "active", "I": "inactive", "": "blank"}


def parse_date(v: str | None) -> tuple[str, date | None]:
    """('blank', None) | ('valid', date) | ('malformed', None). Never turns bad input into a date."""
    v = (v or "").strip()
    if not v:
        return "blank", None
    try:
        return "valid", datetime.strptime(v, "%m/%d/%Y").date()
    except ValueError:  # wrong format AND impossible dates such as 02/30/2027
        return "malformed", None


def _quarantine(s: Session, sf: SourceFile, line_no: int, raw: bytes, reason: str, fld: str | None) -> None:
    s.execute(insert(QuarantineRecord).on_conflict_do_nothing(index_elements=["source_file_id", "line_no"]),
              [{"source_file_id": sf.id, "line_no": line_no, "raw_line": raw, "reason": reason, "field": fld}])


@dataclass
class GeoDecision:
    status: str                  # agree | code_only | zip_only | conflict | ambiguous | out_of_state | unresolved
    county_fips: str | None      # only set when the evidence does not conflict
    code_fips: str | None
    zip_fips: list[str]
    flags: list[str]
    reason: str = ""

    def as_payload(self, d: dict) -> dict:
        return {"status": self.status, "county_fips": self.county_fips, "code_fips": self.code_fips,
                "zip_fips": self.zip_fips, "flags": self.flags, "reason": self.reason,
                "code_map": COUNTY_CODE_MAP_STATUS, "raw_county_code": d.get("county_code"),
                "raw_zip": d.get("zip"), "raw_state": d.get("state")}


def resolve_geo(d: dict, zip_counties: dict[str, set[str]]) -> GeoDecision:
    """Never assigns a county on conflicting or ambiguous evidence; source values are left untouched."""
    code, state, z = d.get("county_code", ""), d.get("state", "").upper(), zip5(d.get("zip"))
    code_fips = DBPR_COUNTY_TO_FIPS.get(code)
    zf = sorted(zip_counties.get(z, set())) if z else []   # a missing ZIP never looks anything up
    flags = []
    if not d.get("addr1") or not d.get("city"):
        flags.append("address_incomplete")
    if not z:
        flags.append("zip_missing")
    elif not zf:
        flags.append("zip_not_in_florida_zcta")
    if not code:
        flags.append("county_code_blank")
    elif code_fips is None:
        flags.append(f"county_code_unknown:{code}")
    if (state and state != "FL") or (len(code) == 3 and code.startswith("7")):
        return GeoDecision("out_of_state", None, code_fips, zf, flags,
                           f"state {state or '-'} / code {code or '-'}: outside Florida, no county assigned")
    if code_fips:
        if not zf:
            return GeoDecision("code_only", code_fips, code_fips, zf, flags + ["county_from_inferred_code_map"],
                               f"code {code} -> {code_fips} by the inferred (V13) code map; ZIP gives no county")
        if code_fips in zf:
            return GeoDecision("agree", code_fips, code_fips, zf, flags + (["zip_spans_counties"] if len(zf) > 1 else []),
                               f"code {code} -> {code_fips} corroborated by ZIP {z} counties {zf}")
        return GeoDecision("conflict", None, code_fips, zf, flags + ["county_zip_conflict"],
                           f"code {code} -> {code_fips} but ZIP {z} lies in {zf}: no county assigned")
    if len(zf) == 1:   # unknown/blank code: county from the ZIP only, never silently (status + flag)
        return GeoDecision("zip_only", zf[0], None, zf, flags + ["county_from_zip_only"],
                           f"code {code or '-'} unusable; ZIP {z} lies only in {zf[0]}")
    if len(zf) > 1:
        return GeoDecision("ambiguous", None, None, zf, flags,
                           f"code {code or '-'} unusable; ZIP {z} spans {zf}: no county assigned")
    return GeoDecision("unresolved", None, None, zf, flags, f"code {code or '-'} unusable and no ZIP county: no county assigned")


def import_extract(s: Session, src: Source, sf: SourceFile, blob: bytes, zip_counties: dict[str, set[str]],
                   target_fips: set[str], *, min_similarity: float = 0.85, today: date | None = None) -> DbprStats:
    st = DbprStats()
    today = today or date.today()
    keep = []
    accepted: list[tuple[str, int, int]] = []   # (licence number, location id, source_record id) kept as evidence
    # line-based so the exact raw bytes of a rejected row can be quarantined (no multi-line rows in real files)
    for line_no, raw in enumerate(blob.split(b"\n"), start=1):
        raw = raw.rstrip(b"\r")
        if not raw.strip():
            continue
        st.rows += 1
        row = next(csv.reader([raw.decode("utf-8", errors="replace")]), [])
        if len(row) != len(COLUMNS):
            st.malformed += 1
            st.quarantined += 1
            _quarantine(s, sf, line_no, raw, f"wrong column count {len(row)} != {len(COLUMNS)}", None)
            continue
        d = dict(zip(COLUMNS, (c.strip() for c in row)))
        exp_kind, exp = parse_date(d["expiry_date"])
        if exp_kind == "malformed":
            st.quarantined += 1
            _quarantine(s, sf, line_no, raw, f"malformed expiry date {d['expiry_date']!r}", "expiry_date")
            continue
        if d["status"] not in KNOWN_STATUS:
            st.quarantined += 1
            _quarantine(s, sf, line_no, raw, f"unrecognised status {d['status']!r}", "status")
            continue
        z = zip5(d["zip"])
        g = resolve_geo(d, zip_counties)
        touches_target = g.county_fips in target_fips or g.code_fips in target_fips or bool(set(g.zip_fips) & target_fips)
        if touches_target:
            st.geo[g.status] += 1
            if g.status == "conflict":
                st.county_zip_mismatch += 1
        if g.county_fips not in target_fips:   # conflicting/ambiguous/out-of-state rows never reach a target county
            continue
        st.in_target += 1
        if d["status"] == "":
            st.status_blank += 1
            continue
        if d["status"] == "I":
            st.status_inactive += 1
            continue
        if not d["licence_number"]:
            st.no_licence_number += 1
            continue
        if exp_kind == "blank":
            st.expiry_blank += 1
            continue
        if exp < today:
            st.expired += 1
            continue
        st.active += 1
        st.by_type[d["licence_type"]] += 1
        d["zip5"], d["line_no"], d["geo"] = z, line_no, g.as_payload(d)
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
            WHERE l.merged_into_id IS NULL AND l.county_fips = :c
            GROUP BY l.id HAVING max(greatest(similarity(l.name_norm, n), coalesce(similarity(al.alias_norm, n), 0))) >= :m
            ORDER BY sim DESC LIMIT 2"""), {"z": d["zip5"], "names": names, "m": min_similarity,
                                                "c": d["geo"]["county_fips"]}).all()
        if not cands:
            st.unmatched += 1
            continue
        if len(cands) == 2 and cands[0].sim - cands[1].sim < 0.05:
            st.ambiguous += 1
            continue
        lid = cands[0].id
        fact_ins = insert(Fact).values([{"subject_type": "location", "subject_id": lid, "field": "licence",
                    "value": {"licence_number": d["licence_number"], "type": d["licence_type"], "status": "active",
                              "original": d["original_licence_date"], "expires": d["expiry_date"], "name_similarity": round(cands[0].sim, 3),
                              "geo_status": d["geo"]["status"], "geo_flags": d["geo"]["flags"],
                              "geo_reason": d["geo"]["reason"], "county_code_map": COUNTY_CODE_MAP_STATUS},
                    "value_norm": d["licence_number"], "source_record_id": rid, "source_key": SOURCE_KEY, "provenance": "registry",
                    "verification_state": "ingested",
                    # flagged geography (code-only, ZIP-only, incomplete address) lowers confidence; never raises it
                    "confidence": round(0.9 * cands[0].sim * (0.8 if d["geo"]["flags"] or d["geo"]["status"] != "agree" else 1), 3)}])
        # the same record version accepted again after an earlier withdrawal becomes current again
        s.execute(fact_ins.on_conflict_do_update(
            index_elements=["subject_type", "subject_id", "field", "value_norm", "source_record_id"],
            set_={"superseded_at": None}, where=Fact.superseded_at.is_not(None)))
        exists = s.execute(text("SELECT active FROM location_source_link WHERE location_id=:l AND source_key='dbpr' AND external_id=:e"),
                           {"l": lid, "e": d["licence_number"]}).first()
        if exists is not None and not exists.active:
            s.execute(text("UPDATE location_source_link SET active = true WHERE location_id=:l AND source_key='dbpr' "
                           "AND external_id=:e"), {"l": lid, "e": d["licence_number"]})
        if exists is None:
            s.add(LocationSourceLink(location_id=lid, source_key=SOURCE_KEY, external_id=d["licence_number"],
                                     match_method="licence_name_zip", match_score=float(cands[0].sim)))
        st.linked += 1
        accepted.append((d["licence_number"], lid, rid))
    # Withdraw evidence from earlier imports of this same remote file unless this import re-accepted the SAME
    # (licence, location, record version); history is kept (superseded_at), nothing is deleted.
    res = s.execute(text("""
        UPDATE fact f SET superseded_at = now()
        FROM source_record r JOIN source_file sfx ON sfx.id = r.source_file_id
        WHERE f.source_record_id = r.id AND f.source_key = :k AND f.field = 'licence' AND f.superseded_at IS NULL
          AND sfx.remote_path = :p
          AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:lics AS text[]), CAST(:lids AS bigint[]), CAST(:rids AS bigint[])) a(l, i, r2)
                          WHERE a.l = f.value_norm AND a.i = f.subject_id AND a.r2 = f.source_record_id)
        RETURNING f.subject_id, f.value_norm"""),
        {"k": SOURCE_KEY, "p": sf.remote_path, "lics": [a[0] for a in accepted], "lids": [a[1] for a in accepted],
         "rids": [a[2] for a in accepted]}).all()
    still = {(a[0], a[1]) for a in accepted}
    st.evidence_superseded = sum(1 for lid, lic in res if (lic, lid) not in still)
    for lid, lic in res:
        if (lic, lid) in still:  # same licence still evidence here via its current record version
            continue
        s.execute(text("UPDATE location_source_link SET active = false WHERE location_id = :l AND source_key = :k "
                       "AND external_id = :e"), {"l": lid, "k": SOURCE_KEY, "e": lic})
    sf.records, sf.quarantined, sf.status, sf.parser_version = st.in_target, st.quarantined, "imported", PARSER_VERSION
    return st
