from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.costs.guard import CostGuard
from app.db.models import Address, Organization, OrganizationLink, QuarantineRecord
from app.net.http import InfraClient
from app.net.registry import load_registry
from app.sources.common import Downloaded, ensure_source, register_file, sha256_file
from app.sources.sunbiz.client import MissingCredentials, SunbizClient, credentials, parse_propfind
from app.sources.sunbiz.importer import import_file
from app.sources.sunbiz.layouts import LAYOUTS
from app.sources.sunbiz.ledger import classify_days, florida_state_holidays
from app.sources.sunbiz.parser import Parsed, Rejected, parse_bytes
from helpers import make_settings
from sunbiz_fixtures import cor, fic, record


# ----------------------------------------------------------------------------- parser
def test_valid_record_parses_with_dates_and_nul_padding_stripped():
    [r] = list(parse_bytes(cor("L26000000001", "ACME LAWN CARE LLC") + b"\n", LAYOUTS["cor"]))
    assert isinstance(r, Parsed)
    assert r.data["name"] == "ACME LAWN CARE LLC" and r.data["file_date"] == date(2026, 10, 1)
    assert r.data["filing_type"] == "FLAL" and "filler" not in r.data


def test_malformed_lines_are_quarantined_and_rest_imports():
    good = cor("L26000000001", "GOOD LLC")
    short = good[:-10]
    bad_date = record("cor", doc="L26000000002", name="BAD DATE LLC", file_date="13452026")
    ctrl = record("cor", doc="L26000000003", name="BAD\x07NAME")
    bad_doc = record("cor", doc="??", name="NO DOC")
    out = list(parse_bytes(b"\n".join([good, short, bad_date, ctrl, bad_doc, good]) + b"\n", LAYOUTS["cor"]))
    kinds = [type(x).__name__ for x in out]
    assert kinds == ["Parsed", "Rejected", "Rejected", "Rejected", "Rejected", "Parsed"]
    reasons = [x.reason for x in out if isinstance(x, Rejected)]
    assert "record length" in reasons[0] and "invalid date" in reasons[1] and "control" in reasons[2] and "document" in reasons[3]


def test_non_utf8_field_decoded_with_warning_not_dropped():
    raw = bytearray(cor("L26000000004", "CAFE"))
    raw[12:16] = b"CAF\xe9"  # cp1252 e-acute, invalid UTF-8
    [r] = list(parse_bytes(bytes(raw), LAYOUTS["cor"]))
    assert isinstance(r, Parsed) and r.data["name"].startswith("CAFé") and r.warnings


def test_truncated_final_line_is_rejected():
    blob = cor("L26000000001", "A LLC") + b"\n" + cor("L26000000002", "B LLC")[:700]
    out = list(parse_bytes(blob, LAYOUTS["cor"]))
    assert isinstance(out[0], Parsed) and isinstance(out[1], Rejected)


# ----------------------------------------------------------------------------- ledger
def test_ledger_distinguishes_holiday_weekend_unexplained_and_unpublished():
    present = {date(2026, 7, 1), date(2026, 7, 7)}
    r = classify_days(date(2026, 7, 1), date(2026, 7, 9), present, failed={date(2026, 7, 8)} - present)
    assert r[date(2026, 7, 2)] == "absent_unexplained" and r[date(2026, 7, 3)] == "absent_holiday"
    assert r[date(2026, 7, 4)] == "absent_nonworkday" and r[date(2026, 7, 8)] == "failed"
    assert r[date(2026, 7, 9)] == "not_yet_published"
    assert date(2026, 11, 27) in florida_state_holidays(2026)  # Friday after Thanksgiving


# ----------------------------------------------------------------------------- client
PROPFIND = b"""<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">
<D:response><D:href>/Public/doc/fic/</D:href><D:propstat><D:prop></D:prop></D:propstat></D:response>
<D:response><D:href>/Public/doc/fic/20261101f.txt</D:href><D:propstat><D:prop><D:getcontentlength>4196</D:getcontentlength>
<D:getlastmodified>Mon, 02 Nov 2026 03:03:15 GMT</D:getlastmodified></D:prop></D:propstat></D:response>
<D:response><D:href>/Public/doc/fic/README.TXT</D:href><D:propstat><D:prop><D:getcontentlength>72</D:getcontentlength></D:prop></D:propstat></D:response>
</D:multistatus>"""


def test_propfind_parsing_and_missing_credentials(monkeypatch):
    rows = parse_propfind(PROPFIND)
    assert ("/Public/doc/fic/20261101f.txt", 4196) in [(h, n) for h, n, _ in rows]
    monkeypatch.delenv("SUNBIZ_SFTP_USER", raising=False)
    with pytest.raises(MissingCredentials):
        credentials()


def test_client_lists_and_downloads_with_size_check(tmp_path, monkeypatch):
    monkeypatch.setenv("SUNBIZ_SFTP_USER", "Public")
    monkeypatch.setenv("SUNBIZ_SFTP_PASSWORD", "not-the-real-one")
    body = fic("G26000000001", "JAX SHINE MOBILE DETAIL") + b"\n" + fic("G26000000002", "OTHER")
    body = body + b"\n"
    seen_auth = []

    def handler(req: httpx.Request):
        seen_auth.append(req.headers.get("authorization", ""))
        if req.method == "PROPFIND":
            return httpx.Response(207, content=PROPFIND.replace(b"4196", str(len(body)).encode()))
        return httpx.Response(200, content=body)
    reg = load_registry(REPO_ROOT / "config" / "network.yaml")
    st = make_settings(tmp_path)
    with InfraClient(reg, st, CostGuard(reg, st), transport=httpx.MockTransport(handler)) as c:
        sc = SunbizClient(c, tmp_path)
        [rf] = sc.list_kind("fic")
        assert rf.day == date(2026, 11, 1) and rf.size == len(body)
        dl = sc.download(rf)
        assert dl.path.read_bytes() == body and dl.size == len(body)
    assert all(a.startswith("Basic ") for a in seen_auth)


# ----------------------------------------------------------------------------- importer (DB)
def _import(s, kind, lines, name):
    src = ensure_source(s, "sunbiz", {"name": "Sunbiz"})
    import tempfile
    p = Path(tempfile.mkdtemp(prefix="sunbiz-test-")) / name
    p.write_bytes(b"\n".join(lines) + b"\n")
    sf = register_file(s, src, kind=kind, remote_path=f"/test/{name}", dl=Downloaded(p, sha256_file(p), p.stat().st_size, True))
    return import_file(s, src, sf, kind, p.read_bytes(), mass_filer_threshold=3)


def test_company_with_several_dbas_links_even_when_owner_arrives_later(db):
    with Session(db) as s:
        st = _import(s, "fic", [fic("G26000000001", "ACME PRESSURE WASHING", owner_charter="L26000000009"),
                                fic("G26000000002", "ACME LAWN", owner_charter="L26000000009")], "f1.txt")
        assert st.dba_links == 2
        assert s.execute(select(OrganizationLink.to_org_id)).scalars().all() == [None, None]  # owner not yet known
        _import(s, "cor", [cor("L26000000009", "ACME SERVICES LLC")], "c1.txt")
        owner = s.execute(select(Organization).where(Organization.document_number == "L26000000009")).scalar_one()
        assert set(s.execute(select(OrganizationLink.to_org_id)).scalars()) == {owner.id}
        s.commit()


def test_registered_agent_and_mass_filer_addresses_are_not_operating(db):
    with Session(db) as s:
        lines = [cor("L26000000101", "SAME AS AGENT LLC", addr="200 AGENT WAY", ra_addr="200 AGENT WAY")]
        lines += [cor(f"L2600000020{i}", f"SHELL {i} LLC", addr="1 FILER PLAZA", ra_addr="9 OTHER RD") for i in range(5)]
        lines += [cor("L26000000301", "REAL SHOP LLC", addr="55 BEACH BLVD", ra_addr="200 AGENT WAY")]
        _import(s, "cor", lines, "c2.txt")
        rows = {(a.owner_id, a.role): a for a in s.execute(select(Address).where(Address.role == "principal")).scalars()}
        by_doc = dict(s.execute(select(Organization.document_number, Organization.id)).all())
        assert not rows[(by_doc["L26000000101"], "principal")].is_operating_location
        assert not rows[(by_doc["L26000000201"], "principal")].is_operating_location  # shared by 5 > threshold 3
        assert rows[(by_doc["L26000000301"], "principal")].is_operating_location
        assert all(not a.is_operating_location for a in s.execute(select(Address).where(Address.role == "registered_agent")).scalars())
        s.commit()


def test_reimport_is_idempotent_and_quarantine_is_recorded(db):
    with Session(db) as s:
        lines = [cor("L26000000401", "ONE LLC"), b"garbage line"]
        a = _import(s, "cor", lines, "c3.txt")
        n = s.execute(text("select count(*) from source_record")).scalar()
        b = _import(s, "cor", lines, "c3.txt")
        assert (a.parsed, a.quarantined) == (b.parsed, b.quarantined) == (1, 1)
        assert s.execute(text("select count(*) from source_record")).scalar() == n
        assert s.execute(select(QuarantineRecord)).scalars().one().reason.startswith("record length")
        s.commit()


def test_dissolution_event_updates_status(db):
    with Session(db) as s:
        _import(s, "cor", [cor("L26000000501", "CLOSING LLC")], "c4.txt")
        ev = record("cor_event", doc="L26000000501", seq="00001", code="CORAPVOLDS", desc="VOLUNTARY DISSOLUTION",
                    effective_date=date(2026, 10, 20), filed_date=date(2026, 10, 20))
        st = _import(s, "cor_event", [ev], "ce4.txt")
        assert st.status_updates == 1
        assert s.execute(select(Organization.status).where(Organization.document_number == "L26000000501")).scalar() == "dissolved"
        s.commit()
