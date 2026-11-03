"""Fix 2: malformed DBPR expiration dates / statuses can never become active-licence evidence."""
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.appconfig.store import ConfigService
from app.config import REPO_ROOT, Settings
from app.db.models import Address, Area, Fact, Location, LocationAlias, LocationSourceLink, SourceRecord
from app.scoring.runner import score_all
from app.sources.common import Downloaded, ensure_source, register_file, sha256_file
from app.sources.dbpr.importer import import_extract, parse_date

TODAY = date(2026, 11, 3)
ZC = {"32207": {"12031"}}
TARGET = {"12031"}


@pytest.mark.parametrize("raw,kind,value", [
    ("", "blank", None), ("   ", "blank", None), ("08/31/2028", "valid", date(2028, 8, 31)),
    ("2028-08-31", "malformed", None), ("13/45/2028", "malformed", None), ("02/30/2027", "malformed", None),
    ("N/A", "malformed", None),
])
def test_parse_date_distinguishes_blank_valid_malformed(raw, kind, value):
    assert parse_date(raw) == (kind, value)


def row(lic, *, status="A", exp="08/31/2028", name="ACE ROOFING LLC", zip_="32207", county="26"):
    return (f'"06","CBC","SMITH, JOHN","{name}","","1 MAIN ST","","","JACKSONVILLE","FL","{zip_}","{county}","1","C","{status}",'
            f'"01/01/2015","01/01/2025","{exp}","","","{lic}",""')


def setup(s, tmp_path, lines, name="CONSTRUCTIONLICENSE_1.csv"):
    s.add(Area(key="duval", name="Duval", kind="county", spec={"geoid": "12031"}, is_target=True, enabled=True,
               geom="SRID=4326;MULTIPOLYGON(((-82 30,-82 31,-81 31,-81 30,-82 30)))"))
    l = Location(display_name="Ace Roofing", name_norm="ace roofing", county_fips="12031", operating_status="open",
                 geom="SRID=4326;POINT(-81.6 30.3)")
    s.add(l)
    s.flush()
    s.add(Address(owner_type="location", owner_id=l.id, role="listing", line1="1 Main St", zip5="32207", norm_key="k1",
                  county_fips="12031", is_operating_location=True))
    s.add(LocationAlias(location_id=l.id, alias="Ace Roofing", alias_norm="ace roofing", kind="listing"))
    s.add(LocationSourceLink(location_id=l.id, source_key="overture", external_id="ov1", match_method="t"))
    src = ensure_source(s, "dbpr", {"name": "dbpr"})
    blob = ("\n".join(lines) + "\n").encode()
    p = tmp_path / name
    p.write_bytes(blob)
    sf = register_file(s, src, kind="dbpr_licensees", remote_path=f"https://x/{name}",
                       dl=Downloaded(p, sha256_file(p), len(blob), True))
    s.flush()
    return l, src, sf, blob


def test_each_case_is_classified_and_only_valid_active_rows_become_evidence(db, tmp_path):
    lines = [row("CBC0000001"),                                  # valid future -> evidence
             row("CBC0000002", exp=""),                          # blank expiry -> not evidence
             row("CBC0000003", exp="08/31/2020"),                # valid but expired -> not evidence
             row("CBC0000004", exp="13/45/2028"),                # malformed -> quarantined
             row("CBC0000005", exp="02/30/2027"),                # impossible date -> quarantined
             row("CBC0000006", status=""),                       # missing status -> not evidence
             row("CBC0000007", status="X"),                      # invalid status -> quarantined
             row("CBC0000008", status="I"),                      # inactive -> not evidence
             row("", status="A"),                                # no licence number -> not evidence
             '"too","few"']                                      # wrong column count -> quarantined
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, lines)
        st = import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        assert (st.active, st.linked, st.expiry_blank, st.expired, st.status_blank, st.status_inactive,
                st.no_licence_number, st.quarantined, st.malformed) == (1, 1, 1, 1, 1, 1, 1, 4, 1)
        q = s.execute(text("select line_no, reason, field, raw_line from quarantine_record order by line_no")).all()
        assert [(x.line_no, x.field) for x in q] == [(4, "expiry_date"), (5, "expiry_date"), (7, "status"), (10, None)]
        assert "13/45/2028" in q[0].reason and bytes(q[0].raw_line) == lines[3].encode()  # raw value preserved
        assert "02/30/2027" in q[1].reason and "'X'" in q[2].reason
        facts = s.execute(text("select value_norm from fact where field='licence' and superseded_at is null")).scalars().all()
        assert facts == ["CBC0000001"]
        s.commit()


def test_previously_stored_malformed_evidence_is_withdrawn_and_stops_inflating_scores(db, tmp_path):
    cfg = ConfigService(Settings(config_dir=REPO_ROOT / "config")).effective(None)
    bad = row("CBC0000009", exp="13/45/2028")
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, [bad])
        # simulate evidence written by the earlier (lenient) importer from this same file
        rec = SourceRecord(source_id=src.id, record_kind="licence", external_id="CBC0000009", source_file_id=sf.id, line_no=1,
                           payload={"expiry_date": "13/45/2028"}, payload_hash="old")
        s.add(rec)
        s.flush()
        s.add(Fact(subject_type="location", subject_id=l.id, field="licence", value={"licence_number": "CBC0000009"},
                   value_norm="CBC0000009", source_record_id=rec.id, source_key="dbpr", provenance="registry"))
        s.add(LocationSourceLink(location_id=l.id, source_key="dbpr", external_id="CBC0000009", match_method="licence_name_zip"))
        s.flush()
        score_all(s, cfg, ["12031"], today=TODAY)
        before = s.execute(text("select value, evidence_points from score where location_id=:l and kind='bos' "
                                "and superseded_at is null"), {"l": l.id}).one()
        st = import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        assert st.quarantined == 1 and st.evidence_superseded == 1
        assert s.execute(text("select count(*) from fact where field='licence' and superseded_at is null")).scalar() == 0
        assert s.execute(text("select count(*) from fact where field='licence'")).scalar() == 1   # history kept
        assert s.execute(text("select active from location_source_link where source_key='dbpr'")).scalar() is False
        score_all(s, cfg, ["12031"], today=TODAY)
        after = s.execute(text("select value, evidence_points from score where location_id=:l and kind='bos' "
                               "and superseded_at is null"), {"l": l.id}).one()
        # the licence point goes, and so does multi_source (dbpr was this location's second source)
        pts = cfg["scoring"]["evidence_points"]
        assert after.evidence_points == before.evidence_points - pts["licence_or_btr"] - pts["multi_source"]
        assert after.value <= before.value
        f = s.execute(text("select f.factor from score_factor f join score sc on sc.id=f.score_id where sc.location_id=:l "
                           "and sc.kind='bos' and sc.superseded_at is null"), {"l": l.id}).scalars().all()
        assert "active_licence_or_btr" not in f
        s.commit()


def test_reimport_of_unchanged_valid_licence_keeps_evidence_current(db, tmp_path):
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, [row("CBC0000010")])
        import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        st = import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        assert st.evidence_superseded == 0
        assert s.execute(text("select count(*) from fact where field='licence' and superseded_at is null")).scalar() == 1
        assert s.execute(text("select bool_and(active) from location_source_link where source_key='dbpr'")).scalar() is True
        s.commit()


def test_withdrawn_evidence_returns_when_the_row_becomes_valid_again(db, tmp_path):
    good = row("CBC0000011")
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, [good])
        import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        # a later version of the same file drops the row -> withdrawn
        st = import_extract(s, src, sf, b"", ZC, TARGET, today=TODAY)
        assert st.evidence_superseded == 1
        assert s.execute(text("select count(*) from fact where field='licence' and superseded_at is null")).scalar() == 0
        # and a still later version restores it -> current again, link active, no duplicate rows
        import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        assert s.execute(text("select count(*) from fact where field='licence' and superseded_at is null")).scalar() == 1
        assert s.execute(text("select count(*) from fact where field='licence'")).scalar() == 1
        assert s.execute(text("select count(*), bool_and(active) from location_source_link where source_key='dbpr'")).one() == (1, True)
        s.commit()
