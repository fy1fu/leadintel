import csv
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.appconfig.store import ConfigService
from app.config import REPO_ROOT, Settings
from app.db.models import Area, ContactPoint, Location, LocationContact, LocationSourceLink, OnlineProfile, Address
from app.reports.prospects import export_csv, niche_comparison
from app.scoring.runner import score_all


def seed(s):
    s.add(Area(key="duval", name="Duval", kind="county", spec={"geoid": "12031"}, is_target=True, enabled=True,
               geom="SRID=4326;MULTIPOLYGON(((-82 30,-82 31,-81 31,-81 30,-82 30)))"))
    l1 = Location(display_name="Bob's Pressure Washing", name_norm="bobs pressure washing", county_fips="12031",
                  category="pressure_washing", operating_status="open", geom="SRID=4326;POINT(-81.6 30.3)")
    l2 = Location(display_name="Coastal Capital Holdings LLC", name_norm="coastal capital holdings", county_fips="12031",
                  operating_status="registered", geom="SRID=4326;POINT(-81.61 30.31)")
    s.add_all([l1, l2])
    s.flush()
    for l, src in ((l1, "overture"), (l1, "sunbiz"), (l2, "sunbiz")):
        s.add(LocationSourceLink(location_id=l.id, source_key=src, external_id=f"{src}-{l.id}", match_method="t"))
    cp = ContactPoint(kind="phone", value_norm="+19045550101", display="+19045550101")
    s.add(cp)
    s.flush()
    s.add(LocationContact(location_id=l1.id, contact_point_id=cp.id, source_key="overture"))
    s.add(OnlineProfile(location_id=l1.id, url="https://facebook.com/bobs", url_norm="https://facebook.com/bobs",
                        platform="facebook", platform_kind="social", source_key="overture"))
    s.add(Address(owner_type="location", owner_id=l2.id, role="principal", line1="1 Filer Plaza", zip5="32202",
                  norm_key="1 FILER PLZ|32202", is_operating_location=True))
    from app.db.models import Organization, LocationOrganization, Source
    src = Source(key="sunbiz", name="Sunbiz")
    s.add(src)
    s.flush()
    org = Organization(source_id=src.id, document_number="G26000000001", name="BOBS PRESSURE WASHING", name_norm="bobs pressure washing",
                       filing_kind="fictitious_name", filing_type_code="FIC", status="active", registration_date=date(2026, 10, 1))
    s.add(org)
    s.flush()
    s.add(LocationOrganization(location_id=l1.id, organization_id=org.id, role="trade_name"))  # regression: org dicts with text fields
    s.flush()
    return l1, l2


def test_scores_persist_with_factors_and_unchanged_inputs_add_no_rows(db, tmp_path):
    cfg = ConfigService(Settings(config_dir=REPO_ROOT / "config")).effective(None)
    with Session(db) as s:
        l1, l2 = seed(s)
        st = score_all(s, cfg, ["12031"], today=date(2026, 11, 3))
        assert st["scored"] == 2
        n = s.execute(text("select count(*) from score")).scalar()
        assert s.execute(text("select count(*) from score_factor")).scalar() > 10
        assert score_all(s, cfg, ["12031"], today=date(2026, 11, 3)).get("unchanged") == 2
        assert s.execute(text("select count(*) from score")).scalar() == n          # append-only, but no churn
        ind = s.execute(text("select industry_profile from location where id=:i"), {"i": l1.id}).scalar()
        assert ind == "pressure_washing"
        hold = s.execute(text("select id, value from score where location_id=:i and kind='bos'"), {"i": l2.id}).one()
        assert hold.value <= 35  # below the cap already: the holding-entity multiplier applied first
        assert s.execute(text("select count(*) from score_factor where score_id=:i and factor='likely_holding_or_investment_entity'"),
                         {"i": hold.id}).scalar() == 1
        groups = set(s.execute(text("select \"group\" from keyword_hit where location_id=:i"), {"i": l2.id}).scalars())
        assert {"holding", "investment"} <= groups
        rep = niche_comparison(s, ["12031"])
        assert rep["rows"] and all(r["outdated_websites"].startswith("not measured") for r in rep["rows"])
        out = tmp_path / "p.csv"
        assert export_csv(s, out, ["12031"]) == 2
        rows = list(csv.DictReader(open(out)))
        bob = next(r for r in rows if r["display_name"].startswith("Bob"))
        assert "[ingested, overture]" in bob["phones"] and bob["emails"] == ""
        assert "emails: not published" in bob["missing"]
        s.commit()
