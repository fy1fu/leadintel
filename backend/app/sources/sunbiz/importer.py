"""Import parsed Sunbiz files (PHASE1.md item 1.5).

* `organization` per document number (current state); every file line is kept verbatim in an
  append-only `source_record` (raw bytes, file, line number), so each lead traces to its filing.
* Typed addresses: principal / mailing / registered_agent / owner. Registered-agent addresses,
  addresses equal to the entity's RA address, PO boxes and mass-filer addresses are never
  operating locations.
* DBA -> owning entity via `own_charter` (`organization_link dba_owned_by`); links resolve later
  when the owner arrives in a later file.
* Event files update status (dissolution / revocation / withdrawal / reinstatement / cancellation).
* Malformed lines are quarantined; the rest of the file imports.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Address, Organization, OrganizationLink, PersonRole, QuarantineRecord, Source, SourceFile, SourceRecord
from app.normalize.core import address_key, is_po_box, name_norm, zip5
from app.sources.common import payload_hash
from app.sources.sunbiz.layouts import FILING_KIND, LAYOUTS
from app.sources.sunbiz.parser import Parsed, parse_bytes

SOURCE_KEY = "sunbiz"


@dataclass
class FileStats:
    kind: str
    parsed: int = 0
    quarantined: int = 0
    warnings: int = 0
    organizations: int = 0
    status_updates: int = 0
    dba_links: int = 0
    reasons: Counter = field(default_factory=Counter)


def _json(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


def _status_from_event(text_: str | None) -> str | None:
    t = (text_ or "").lower()
    if "reinstat" in t or "renew" in t:
        return "active"
    if any(w in t for w in ("dissol", "revoc", "revok", "withdraw", "cancel", "expire")):
        return "dissolved" if "dissol" in t else "inactive"
    return None


def import_file(s: Session, src: Source, sf: SourceFile, kind: str, blob: bytes, *, mass_filer_threshold: int = 25) -> FileStats:
    st = FileStats(kind)
    good: list[Parsed] = []
    for r in parse_bytes(blob, LAYOUTS[kind]):
        if isinstance(r, Parsed):
            good.append(r)
            st.warnings += bool(r.warnings)
        else:
            st.quarantined += 1
            st.reasons[r.reason.split(":")[0]] += 1
            s.execute(insert(QuarantineRecord).on_conflict_do_nothing(index_elements=["source_file_id", "line_no"]),
                      [{"source_file_id": sf.id, "line_no": r.line_no, "raw_line": r.raw, "reason": r.reason, "field": r.field}])
    st.parsed = len(good)
    if good:
        recs = [{"source_id": src.id, "record_kind": kind, "external_id": r.data["doc"], "source_file_id": sf.id,
                 "line_no": r.line_no, "raw_line": r.raw, "payload": _json(r.data) | ({"_warnings": r.warnings} if r.warnings else {}),
                 "payload_hash": payload_hash(_json(r.data)), "licence_tag": "public-record"} for r in good]
        s.execute(insert(SourceRecord).on_conflict_do_nothing(
            index_elements=["source_id", "record_kind", "external_id", "payload_hash"]), recs)
        rid = {(e, h): i for i, e, h in s.execute(select(SourceRecord.id, SourceRecord.external_id, SourceRecord.payload_hash)
                                                  .where(SourceRecord.source_id == src.id, SourceRecord.record_kind == kind,
                                                         SourceRecord.external_id.in_([r.data["doc"] for r in good])))}
        handler = {"cor": _import_cor, "fic": _import_fic, "cor_event": _import_cor_events, "fic_event": _import_fic_events}[kind]
        handler(s, src, good, rid, st)
    sf.records, sf.quarantined, sf.status = st.parsed, st.quarantined, "imported"
    sf.imported_at = text("now()")
    _post_import(s, mass_filer_threshold)
    return st


def _org_rows(src: Source, good: list[Parsed], rid: dict, kind_fn) -> list[dict]:
    rows = {}
    for r in good:
        d = r.data
        fk, code = kind_fn(d)
        rows[d["doc"]] = {"source_id": src.id, "document_number": d["doc"], "name": d.get("name") or "",
                          "name_norm": name_norm(d.get("name")), "filing_kind": fk, "filing_type_code": code,
                          "status": {"A": "active", "I": "inactive"}.get(d.get("status") or "", "unknown"),
                          "registration_date": d.get("file_date"), "county_hint": d.get("county"),
                          "latest_source_record_id": rid[(d["doc"], payload_hash(_json(d)))]}
    return list(rows.values())


def _upsert_orgs(s: Session, rows: list[dict]) -> dict[str, int]:
    stmt = insert(Organization)
    stmt = stmt.on_conflict_do_update(index_elements=["source_id", "document_number"], set_={
        c: stmt.excluded[c] for c in ("name", "name_norm", "filing_kind", "filing_type_code", "status",
                                      "registration_date", "county_hint", "latest_source_record_id")} | {"updated_at": text("now()")})
    s.execute(stmt, rows)
    return dict(s.execute(select(Organization.document_number, Organization.id).where(
        Organization.document_number.in_([r["document_number"] for r in rows]))).all())


def _addr(owner_id: int, role: str, l1, l2, city, state, zp, rid: int, operating: bool, reason: str | None) -> dict | None:
    nk = address_key(l1, l2, zp)
    if not nk and not l1:
        return None
    return {"owner_type": "organization", "owner_id": owner_id, "role": role, "line1": l1, "line2": l2, "city": city,
            "state": (state or None), "zip5": zip5(zp), "country": "US", "norm_key": nk or f"raw:{(l1 or '')[:60]}|{zp}",
            "is_operating_location": operating, "not_operating_reason": reason, "source_record_id": rid}


def _import_cor(s: Session, src: Source, good: list[Parsed], rid: dict, st: FileStats) -> None:
    def kind_fn(d):
        code = (d.get("filing_type") or "").strip() or None
        return FILING_KIND.get(code or "", "other"), code
    ids = _upsert_orgs(s, _org_rows(src, good, rid, kind_fn))
    st.organizations = len(ids)
    addrs, people = [], []
    for r in good:
        d, oid = r.data, ids[r.data["doc"]]
        record_id = rid[(d["doc"], payload_hash(_json(d)))]
        ra_key = address_key(d.get("ra_addr"), None, d.get("ra_zip"))
        p_key = address_key(d.get("addr1"), d.get("addr2"), d.get("zip"))
        reason = ("PO box" if is_po_box(d.get("addr1")) else
                  "same as registered-agent address" if p_key and p_key == ra_key else None)
        for a in (_addr(oid, "principal", d.get("addr1"), d.get("addr2"), d.get("city"), d.get("state"), d.get("zip"),
                        record_id, reason is None, reason),
                  _addr(oid, "mailing", d.get("mail_addr1"), d.get("mail_addr2"), d.get("mail_city"), d.get("mail_state"),
                        d.get("mail_zip"), record_id, False, "mailing address"),
                  _addr(oid, "registered_agent", d.get("ra_addr"), None, d.get("ra_city"), d.get("ra_state"), d.get("ra_zip"),
                        record_id, False, "registered-agent address")):
            if a:
                addrs.append(a)
        if d.get("ra_name"):
            people.append({"organization_id": oid, "role": "registered_agent", "title": None, "name": d["ra_name"],
                           "is_entity": d.get("ra_name_type") == "C", "source_record_id": record_id})
        for i in range(1, 7):
            if d.get(f"off{i}_name"):
                people.append({"organization_id": oid, "role": "officer", "title": d.get(f"off{i}_title"),
                               "name": d[f"off{i}_name"], "is_entity": d.get(f"off{i}_type") == "C",
                               "source_record_id": record_id})
    if addrs:
        s.execute(insert(Address).on_conflict_do_nothing(index_elements=["owner_type", "owner_id", "role", "norm_key"]), addrs)
    if people:
        s.execute(insert(PersonRole).on_conflict_do_nothing(index_elements=["organization_id", "role", "name", "source_record_id"]), people)


def _import_fic(s: Session, src: Source, good: list[Parsed], rid: dict, st: FileStats) -> None:
    ids = _upsert_orgs(s, _org_rows(src, good, rid, lambda d: ("fictitious_name", "FIC")))
    st.organizations = len(ids)
    addrs, people, links = [], [], []
    for r in good:
        d, oid = r.data, ids[r.data["doc"]]
        record_id = rid[(d["doc"], payload_hash(_json(d)))]
        reason = "PO box" if is_po_box(d.get("addr1")) else None
        a = _addr(oid, "principal", d.get("addr1"), d.get("addr2"), d.get("city"), d.get("state"), d.get("zip"),
                  record_id, reason is None, reason)
        if a:
            addrs.append(a)
        for i in range(1, 11):
            nm = d.get(f"own{i}_name")
            if not nm:
                continue
            is_entity = d.get(f"own{i}_fmt") == "C"
            people.append({"organization_id": oid, "role": "owner", "title": None, "name": nm, "is_entity": is_entity,
                           "source_record_id": record_id})
            charter = d.get(f"own{i}_charter")
            if is_entity and charter:
                links.append({"from_org_id": oid, "to_document_number": charter, "kind": "dba_owned_by",
                              "evidence": {"field": f"own{i}_charter", "value": charter, "source_record_id": record_id},
                              "confidence": 1.0})
    if addrs:
        s.execute(insert(Address).on_conflict_do_nothing(index_elements=["owner_type", "owner_id", "role", "norm_key"]), addrs)
    if people:
        s.execute(insert(PersonRole).on_conflict_do_nothing(index_elements=["organization_id", "role", "name", "source_record_id"]), people)
    if links:
        s.execute(insert(OrganizationLink).on_conflict_do_nothing(index_elements=["from_org_id", "to_document_number", "kind"]), links)
        st.dba_links = len(links)


def _apply_status(s: Session, doc: str, status: str, when: date | None, st: FileStats) -> None:
    res = s.execute(text("UPDATE organization SET status=:st, status_changed_at=:w, updated_at=now() "
                         "WHERE document_number=:d AND (status_changed_at IS NULL OR :w IS NULL OR status_changed_at <= :w)"),
                    {"st": status, "w": when, "d": doc})
    st.status_updates += res.rowcount or 0


def _import_cor_events(s: Session, src: Source, good: list[Parsed], rid: dict, st: FileStats) -> None:
    for r in good:
        d = r.data
        status = _status_from_event(f"{d.get('code')} {d.get('desc')}")
        if status:
            _apply_status(s, d["doc"], status, d.get("effective_date") or d.get("filed_date"), st)


def _import_fic_events(s: Session, src: Source, good: list[Parsed], rid: dict, st: FileStats) -> None:
    for r in good:
        d = r.data
        status = _status_from_event(d.get("action_verbage"))
        if status:
            _apply_status(s, d.get("orig_doc") or d["doc"], status, d.get("event_date"), st)


def _post_import(s: Session, threshold: int) -> None:
    # resolve DBA links whose owner has arrived since
    s.execute(text("UPDATE organization_link l SET to_org_id = o.id FROM organization o "
                   "WHERE l.to_org_id IS NULL AND o.document_number = l.to_document_number"))
    # mass-filer / registered-agent-service addresses are not operating locations
    s.execute(text("""UPDATE address a SET is_operating_location = false,
                         not_operating_reason = 'address shared by ' || c.n || ' filings or used as a registered-agent address'
                      FROM (SELECT norm_key, count(DISTINCT owner_id) n FROM address
                            WHERE owner_type='organization' AND role IN ('principal','registered_agent')
                            GROUP BY norm_key HAVING count(DISTINCT owner_id) > :t) c
                      WHERE a.owner_type='organization' AND a.role='principal' AND a.norm_key=c.norm_key
                        AND a.is_operating_location"""), {"t": threshold})
