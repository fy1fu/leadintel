from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import Address, Location, LocationAlias
from app.sources.btr.csv_import import MappingError, import_btr_csv
from app.sources.common import Downloaded, ensure_source, register_file, sha256_file
from app.sources.dbpr.importer import import_extract


def _sf(s, key, tmp_path, blob, name):
    src = ensure_source(s, key, {"name": key})
    p = tmp_path / name
    p.write_bytes(blob)
    return src, register_file(s, src, kind=key, remote_path=f"/t/{name}", dl=Downloaded(p, sha256_file(p), len(blob), True))


def _loc(s, name, zip_):
    from app.normalize.core import name_norm
    l = Location(display_name=name, name_norm=name_norm(name), county_fips="12031", operating_status="open")
    s.add(l)
    s.flush()
    s.add(Address(owner_type="location", owner_id=l.id, role="listing", line1="1 X St", zip5=zip_, norm_key=f"k{l.id}"))
    s.add(LocationAlias(location_id=l.id, alias=name, alias_norm=name_norm(name), kind="listing"))
    s.flush()
    return l


def dbpr_row(name, dba, zip_, county, status="A", exp="08/31/2028", lic="CBC1234567"):
    return (f'"06","CBC","{name}","{dba}","","1 MAIN ST","","","JACKSONVILLE","FL","{zip_}","{county}","1234567","C","{status}",'
            f'"01/01/2015","01/01/2025","{exp}","","","{lic}",""')


def test_dbpr_licence_becomes_evidence_only_for_matching_location(db, tmp_path):
    with Session(db) as s:
        l = _loc(s, "Ace Roofing", "32207")
        rows = [dbpr_row("SMITH, JOHN", "ACE ROOFING LLC", "32207", "26"),
                dbpr_row("DOE, JANE", "NOBODY CONTRACTING", "32207", "26", lic="CBC7654321"),
                dbpr_row("OLD, ONE", "ACE ROOFING", "32207", "26", status="I", lic="CBC1111111"),
                dbpr_row("FAR, AWAY", "ACE ROOFING", "33101", "23", lic="CBC2222222"),
                '"too","few","columns"']
        src, sf = _sf(s, "dbpr", tmp_path, "\n".join(rows).encode(), "c.csv")
        st = import_extract(s, src, sf, "\n".join(rows).encode(), {"32207": {"12031"}, "33101": {"12086"}}, {"12031"})
        assert (st.malformed, st.in_target, st.active, st.linked, st.unmatched) == (1, 3, 2, 1, 1)
        facts = s.execute(text("select subject_id, verification_state from fact where field='licence'")).all()
        assert facts == [(l.id, "ingested")]
        assert s.execute(text("select count(*) from location")).scalar() == 1  # licences never create prospects
        s.commit()


MAPPING = {"business_name": "Business Name", "dba_name": "DBA", "address": "Location Address", "city": "City",
           "zip": "Zip", "receipt_number": "Receipt #", "issue_date": "Issued", "expiry_date": "Expires", "occupation": "Occupation"}


def test_btr_csv_validates_links_and_creates(db, tmp_path):
    csv_text = ("Business Name,DBA,Location Address,City,Zip,Receipt #,Issued,Expires,Occupation\n"
                "Ace Roofing LLC,,1 X St,Jacksonville,32207,R-1,10/01/2026,09/30/2027,ROOFING\n"
                "New Shine LLC,Jax Shine Mobile Detail,9 Y Rd,Jacksonville,32210,R-2,10/15/2026,09/30/2027,DETAILING\n"
                "Old Co,,5 Z Ave,Jacksonville,32210,R-3,10/01/2020,09/30/2021,OTHER\n"
                ",,7 W St,Jacksonville,32210,R-4,10/01/2026,09/30/2027,MISSING NAME\n"
                "Bad Date,,8 V St,Jacksonville,32210,R-5,2026-13-45,09/30/2027,X\n")
    with Session(db) as s:
        l = _loc(s, "Ace Roofing", "32207")
        src, sf = _sf(s, "btr_duval", tmp_path, csv_text.encode(), "btr.csv")
        st = import_btr_csv(s, src, sf, csv_text.encode(), MAPPING, "12031", today=date(2026, 11, 3))
        assert (st.rows, st.current, st.expired, st.linked, st.created) == (5, 2, 1, 1, 1)
        assert [r["line"] for r in st.rejected] == [5, 6]
        new = s.execute(text("select display_name, operating_status from location where id <> :i"), {"i": l.id}).one()
        assert new == ("Jax Shine Mobile Detail", "registered")
        with pytest.raises(MappingError):
            import_btr_csv(s, src, sf, b"Wrong,Header\n1,2\n", MAPPING, "12031")
        s.commit()
