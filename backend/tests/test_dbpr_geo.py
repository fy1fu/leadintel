"""Fix 3: DBPR geographic validation - never place a licence in a target county on conflicting evidence."""
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import Address, Location, LocationAlias
from app.sources.dbpr.importer import DBPR_COUNTY_TO_FIPS, PARSER_VERSION, import_extract, resolve_geo
from tests.test_dbpr_validation import row, setup

TODAY = date(2026, 11, 3)
# 32207 Duval only; 32084 St. Johns only; 32099 spans Duval + Nassau; 33602 Hillsborough
ZC = {"32207": {"12031"}, "32084": {"12109"}, "32099": {"12031", "12089"}, "33602": {"12057"}}
TARGET = {"12019", "12031", "12109"}


def d(code="26", zip_="32207", state="FL", addr1="1 MAIN ST", city="JACKSONVILLE"):
    return {"county_code": code, "zip": zip_, "state": state, "addr1": addr1, "city": city}


def test_code_map_covers_all_67_counties_with_target_codes_correct():
    assert len(DBPR_COUNTY_TO_FIPS) == 67
    assert (DBPR_COUNTY_TO_FIPS["20"], DBPR_COUNTY_TO_FIPS["26"], DBPR_COUNTY_TO_FIPS["65"]) == ("12019", "12031", "12109")
    assert DBPR_COUNTY_TO_FIPS["23"] == "12086" and DBPR_COUNTY_TO_FIPS["39"] == "12057"  # Miami-Dade, Hillsborough
    assert len(set(DBPR_COUNTY_TO_FIPS.values())) == 67


@pytest.mark.parametrize("case,status,county,flag", [
    (d(), "agree", "12031", None),                                         # code and ZIP agree
    (d(code="26", zip_="32084"), "conflict", None, "county_zip_conflict"),  # disagree: Duval code, St. Johns ZIP
    (d(code="39", zip_="32207"), "conflict", None, "county_zip_conflict"),  # non-target code, target ZIP
    (d(code="", zip_="32099"), "ambiguous", None, None),                   # ZIP spans counties, no code to decide
    (d(code="26", zip_="32099"), "agree", "12031", "zip_spans_counties"),  # spanning ZIP, code picks a member county
    (d(code="", zip_="32207"), "zip_only", "12031", None),                 # no code, single-county ZIP (flagged by status)
    (d(code="99", zip_="32084"), "zip_only", "12109", "county_code_unknown:99"),
    (d(code="721", zip_="30303", state="GA"), "out_of_state", None, "county_code_unknown:721"),
    (d(code="721", zip_="32207"), "out_of_state", None, None),             # 7xx = out-of-state even with a FL ZIP
    (d(code="26", zip_=""), "code_only", "12031", "zip_missing"),          # missing ZIP
    (d(code="", zip_=""), "unresolved", None, "zip_missing"),
    (d(addr1="", city=""), "agree", "12031", "address_incomplete"),        # incomplete address -> flagged
    (d(code="26", zip_="33602"), "conflict", None, "county_zip_conflict"),  # target code, non-target ZIP
    (d(code="20", zip_="32099"), "conflict", None, "county_zip_conflict"),  # spanning ZIP that excludes the code county
    (d(code="26", zip_="99999"), "code_only", "12031", "county_from_inferred_code_map"),  # ZIP not in FL ZCTA
    (d(code="26", zip_=""), "code_only", "12031", "county_from_inferred_code_map"),
    (d(code="", zip_="32207"), "zip_only", "12031", "county_code_blank"),
    (d(code="", zip_="32207"), "zip_only", "12031", "county_from_zip_only"),
    (d(code="99", zip_="99999"), "unresolved", None, "zip_not_in_florida_zcta"),
    (d(code="26", zip_="32207", state=""), "agree", "12031", None),       # blank state treated as Florida
    (d(code="26", zip_="32207", state="GA"), "out_of_state", None, None),  # non-FL state wins over a FL code
])
def test_resolve_geo_policy(case, status, county, flag):
    before = dict(case)
    g = resolve_geo(case, ZC)
    assert (g.status, g.county_fips) == (status, county)
    if flag:
        assert flag in g.flags
    assert case == before                                              # source values preserved
    pay = g.as_payload(case)
    assert pay["code_map"] == "inferred_v13" and pay["reason"] and pay["raw_state"] == case["state"]
    assert g.as_payload(case)["raw_county_code"] == case["county_code"] and g.as_payload(case)["raw_zip"] == case["zip"]


def test_import_applies_policy_and_mismatches_never_reach_a_target_county(db, tmp_path):
    lines = [row("CBC0000101"),                                 # agree -> linked
             row("CBC0000102", county="39"),                   # Hillsborough code + Duval ZIP -> excluded, counted
             row("CBC0000103", zip_="32084"),                  # Duval code + St. Johns ZIP -> excluded, counted
             row("CBC0000104", county="", zip_="32099"),       # ambiguous ZIP -> excluded
             row("CBC0000105", county="721"),                  # out of state -> excluded
             row("CBC0000106", zip_="", county="26")]          # missing ZIP -> in Duval, but cannot be matched
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, lines)
        st = import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        assert st.in_target == 2 and st.linked == 1 and st.unmatched == 1
        assert st.county_zip_mismatch == 2
        assert st.geo == {"agree": 1, "conflict": 2, "ambiguous": 1, "out_of_state": 1, "code_only": 1}
        assert s.execute(text("select array_agg(value_norm) from fact where field='licence'")).scalar() == ["CBC0000101"]
        geo = s.execute(text("select payload->'geo' from source_record where external_id='CBC0000101'")).scalar()
        assert geo["status"] == "agree" and geo["raw_county_code"] == "26" and geo["raw_zip"] == "32207"
        assert sf.parser_version == PARSER_VERSION
        s.commit()


def test_matching_is_restricted_to_the_resolved_county(db, tmp_path):
    # A same-name location exists in the spanning ZIP but in Nassau (non-target); a Duval-coded licence must not attach.
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, [row("CBC0000201", zip_="32099", name="BAYSIDE PLUMBING")])
        n = Location(display_name="Bayside Plumbing", name_norm="bayside plumbing", county_fips="12089", operating_status="open",
                     geom="SRID=4326;POINT(-81.6 30.6)")
        s.add(n)
        s.flush()
        s.add(Address(owner_type="location", owner_id=n.id, role="listing", line1="9 Main St", zip5="32099", norm_key="k9",
                      county_fips="12089", is_operating_location=True))
        s.add(LocationAlias(location_id=n.id, alias="Bayside Plumbing", alias_norm="bayside plumbing", kind="listing"))
        s.flush()
        st = import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        assert st.in_target == 1 and st.linked == 0 and st.unmatched == 1
        assert s.execute(text("select count(*) from fact where field='licence'")).scalar() == 0
        s.commit()


def test_flagged_geography_lowers_confidence(db, tmp_path):
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, [row("CBC0000301", county="")])   # ZIP-only placement
        import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        conf, v = s.execute(text("select confidence, value from fact where field='licence'")).one()
        assert v["geo_status"] == "zip_only" and conf < 0.9
        s.commit()


CODES = ["", "20", "26", "65", "39", "23", "99", "721", "7"]
ZIPS = ["", "32207", "32084", "32099", "33602", "99999", "3220"]
STATES = ["FL", "", "GA"]


@pytest.mark.parametrize("state", STATES)
def test_geo_invariants_hold_for_every_code_zip_combination(state):
    for code in CODES:
        for z in ZIPS:
            g = resolve_geo(d(code=code, zip_=z, state=state), ZC)
            zf = ZC.get(z, set())
            code_fips = DBPR_COUNTY_TO_FIPS.get(code)
            if g.county_fips is None:
                continue
            # 1. a county is never assigned against the ZIP's counties
            assert not zf or g.county_fips in zf, (code, z, state, g)
            # 2. a county is never assigned against a known code
            assert code_fips is None or g.county_fips == code_fips, (code, z, state, g)
            # 3. anything not corroborated by both code and ZIP says how it was derived
            if g.status != "agree":
                assert {"county_from_inferred_code_map", "county_from_zip_only"} & set(g.flags), (code, z, g)
            # 4. unknown / blank codes never get a county silently
            if code_fips is None:
                assert g.status == "zip_only" and ("county_code_blank" in g.flags or f"county_code_unknown:{code}" in g.flags)
            # 5. out-of-state never gets a county
            assert state != "GA" and not (len(code) == 3 and code.startswith("7"))


def test_audit_trail_on_stored_record(db, tmp_path):
    with Session(db) as s:
        l, src, sf, blob = setup(s, tmp_path, [row("CBC0000401", county="")])
        import_extract(s, src, sf, blob, ZC, TARGET, today=TODAY)
        geo = s.execute(text("select payload->'geo' from source_record where external_id='CBC0000401'")).scalar()
        assert geo["status"] == "zip_only" and geo["code_map"] == "inferred_v13" and "county_from_zip_only" in geo["flags"]
        assert geo["raw_county_code"] == "" and geo["raw_zip"] == "32207" and "32207" in geo["reason"]
        v = s.execute(text("select value from fact where field='licence'")).scalar()
        assert v["county_code_map"] == "inferred_v13" and v["geo_reason"] == geo["reason"]
        s.commit()


def test_missing_zip_is_unresolved_even_if_the_relationship_table_has_a_blank_key():
    # the Census relationship file contains county rows with a blank ZCTA; they must never act as a ZIP
    g = resolve_geo(d(code="", zip_=""), {**ZC, "": {"12031", "12089"}})
    assert (g.status, g.county_fips) == ("unresolved", None) and "zip_missing" in g.flags
