"""Prospect filters, paging and counts against PostgreSQL (M1 S2b).

Runs only with LEADINTEL_TEST_DATABASE_URL (a *_test database; see conftest). A deterministic synthetic data set
covers every filter dimension, including the awkward cases: merged locations, non-target counties, superseded
scores and qualifications, inactive contacts, missing coordinates, listing-only and unknown website status.
An independent Python model computes the expected answer for every filter; the API must agree with it in
total, every page (order and no gaps or duplicates), the summary and the map points.
"""
import random
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import business
from app.api.main import create_app
from app.db.models import (Area, ContactPoint, Location, LocationContact, LocationSourceLink, OnlineProfile,
                           Qualification, Score)
from app.normalize.core import name_norm
from app.scoring.model import _presence

pytestmark = pytest.mark.usefixtures("db")
N = 400
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
COUNTIES = {"12031": "duval", "12019": "clay", "12089": "nassau"}       # nassau is not a target county
NAMES = ["Bob's Pressure Washing", "Acme 50% Cleaning", "The Green Lawn LLC", "Coastal_Pros", "LLC",
         "Sunny Landscaping", "Ace Cleaning Co", "Jax Lawn Care Inc"]
OPERATING = ["open", "open", "open", "permanently_closed", "temporarily_closed", "unknown", "registered"]
PLATFORMS = [[], [("website", "independent")], [("facebook", "social")], [("linktree", "link_hub")],
             [("yelp", "listing")], [("website", "independent"), ("facebook", "social")]]
QUALS = ["provisionally_qualified", "needs_research", "not_qualified", None]


def build(s: Session) -> list[dict]:
    rnd = random.Random(20261105)
    for geoid, key in COUNTIES.items():
        s.add(Area(key=key, name=key.title(), kind="county", spec={"geoid": geoid}, is_target=key != "nassau",
                   enabled=True, geom="SRID=4326;MULTIPOLYGON(((-82 30,-82 31,-81 31,-81 30,-82 30)))"))
    model, locs = [], []
    for i in range(N):
        name = f"{rnd.choice(NAMES)} {i}"
        fips = rnd.choice(list(COUNTIES))
        has_geom = rnd.random() < 0.8
        loc = Location(display_name=name, name_norm=name_norm(name), county_fips=fips,
                       industry_profile=rnd.choice(["cleaning", "landscaping", None]),
                       operating_status=rnd.choice(OPERATING),
                       geom=f"SRID=4326;POINT({-81.5 - rnd.random() / 10} {30.2 + rnd.random() / 10})" if has_geom else None)
        locs.append(loc)
        model.append({"name": name, "fips": fips, "geom": has_geom, "niche": loc.industry_profile,
                      "operating": loc.operating_status, "platforms": rnd.choice(PLATFORMS),
                      "source": rnd.random() < 0.85, "phone": rnd.random() < 0.6, "phone_inactive": rnd.random() < 0.2,
                      "email": rnd.random() < 0.3, "scored": rnd.random() < 0.9,
                      "bos": round(rnd.uniform(0, 100), 1), "lps": round(rnd.uniform(0, 100), 1),
                      "qual": rnd.choice(QUALS), "merged": rnd.random() < 0.03})
    s.add_all(locs)
    s.flush()
    for loc, m in zip(locs, model):
        m["id"] = loc.id
        if m["merged"]:
            loc.merged_into_id = locs[0].id if loc.id != locs[0].id else None
            m["merged"] = loc.merged_into_id is not None
        if m["source"]:
            s.add(LocationSourceLink(location_id=loc.id, source_key="overture", external_id=f"o{loc.id}", match_method="t"))
        s.add(LocationSourceLink(location_id=loc.id, source_key="old", external_id=f"x{loc.id}", match_method="t",
                                 active=False))                                  # inactive links never count
        for platform, kind in m["platforms"]:
            s.add(OnlineProfile(location_id=loc.id, url=f"https://{platform}.example/{loc.id}",
                                url_norm=f"https://{platform}.example/{loc.id}", platform=platform,
                                platform_kind=kind, source_key="overture"))
        for kind, wanted, active in (("phone", m["phone"], True), ("phone", m["phone_inactive"], False),
                                     ("email", m["email"], True)):
            if wanted:
                cp = ContactPoint(kind=kind, value_norm=f"{kind}{loc.id}{active}", display=f"{kind} {loc.id}")
                s.add(cp)
                s.flush()
                s.add(LocationContact(location_id=loc.id, contact_point_id=cp.id, source_key="overture", active=active))
        if m["scored"]:
            for kind in ("bos", "lps"):
                s.add(Score(location_id=loc.id, kind=kind, stage="provisional", value=99.9, superseded_at=NOW))  # old
                s.add(Score(location_id=loc.id, kind=kind, stage="provisional", value=m[kind]))
        if m["qual"]:
            s.add(Qualification(location_id=loc.id, status="not_qualified", stage="provisional", superseded_at=NOW))
            s.add(Qualification(location_id=loc.id, status=m["qual"], stage="provisional"))
    s.commit()
    return model


def website_status(m) -> str:
    return {"independent_website": "listed", "facebook_only": "social_only", "instagram_only": "social_only",
            "social_only": "social_only", "directory_only": "none_recorded",
            "unknown": "unknown"}[_presence(m["platforms"], m["source"])]


def operating_bucket(v) -> str:
    return "open" if v == "open" else "closed" if v in ("permanently_closed", "temporarily_closed") else "unknown"


def expected(model, **f) -> list[dict]:
    """Independent Python model of ProspectFilter semantics, sorted like the API."""
    rows = [m for m in model if not m["merged"] and COUNTIES[m["fips"]] != "nassau"]
    if "county" in f:
        rows = [m for m in rows if COUNTIES[m["fips"]] in f["county"].split(",")]
    if "niche" in f:
        rows = [m for m in rows if m["niche"] in f["niche"].split(",")]
    if "qualification" in f and f["qualification"]:
        want = f["qualification"].split(",")
        rows = [m for m in rows if (m["qual"] or "unscored") in want]
    if "q" in f:
        n = name_norm(f["q"])
        rows = [m for m in rows if (n in name_norm(m["name"]) if n else f["q"].lower() in m["name"].lower())]
    if "website" in f:
        rows = [m for m in rows if website_status(m) in f["website"].split(",")]
    if "operating" in f:
        rows = [m for m in rows if operating_bucket(m["operating"]) in f["operating"].split(",")]
    if "phone" in f:
        rows = [m for m in rows if m["phone"] == (f["phone"] == "true")]
    if "email" in f:
        rows = [m for m in rows if m["email"] == (f["email"] == "true")]
    for key, col, op in (("bos_min", "bos", "ge"), ("bos_max", "bos", "le"), ("lps_min", "lps", "ge"), ("lps_max", "lps", "le")):
        if key in f:
            rows = [m for m in rows if m["scored"] and (m[col] >= f[key] if op == "ge" else m[col] <= f[key])]
    sort = f.get("sort", "lps")
    if sort == "name":
        rows.sort(key=lambda m: (m["name"].lower(), m["id"]))
    else:
        rows.sort(key=lambda m: (0, -m[sort], m["id"]) if m["scored"] else (1, 0, m["id"]))
    return rows


@pytest.fixture
def api(db, tmp_path, monkeypatch):
    with Session(db) as s:
        model = build(s)
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", db.url.render_as_string(hide_password=False))
    business._engine.cache_clear()
    from types import SimpleNamespace
    yield TestClient(create_app(SimpleNamespace(diagnostics=None)), base_url="http://127.0.0.1:8765"), model
    business._engine.cache_clear()


FILTERS = [
    {}, {"county": "duval"}, {"county": "duval,clay"}, {"county": "nassau"}, {"niche": "cleaning"},
    {"qualification": "needs_research"}, {"qualification": "unscored"}, {"qualification": "provisionally_qualified,unscored"},
    {"qualification": ""}, {"q": "bob"}, {"q": "50%"}, {"q": "LLC"}, {"q": "coastal_pros"}, {"q": "the green"},
    {"website": "listed"}, {"website": "social_only"}, {"website": "none_recorded"}, {"website": "unknown"},
    {"website": "none_recorded,unknown"}, {"operating": "open"}, {"operating": "closed"}, {"operating": "unknown"},
    {"operating": "open,unknown"}, {"phone": "true"}, {"phone": "false"}, {"email": "true"},
    {"bos_min": 40, "bos_max": 70}, {"lps_min": 80}, {"sort": "bos"}, {"sort": "name"},
    {"county": "duval", "niche": "landscaping", "website": "none_recorded", "phone": "true", "operating": "open,unknown",
     "lps_min": 20, "qualification": "provisionally_qualified,needs_research"},
]


@pytest.mark.parametrize("f", FILTERS, ids=lambda f: ",".join(f"{k}={v}" for k, v in f.items()) or "all")
def test_list_pages_summary_and_points_agree_with_the_model(api, f):
    c, model = api
    want = expected(model, **f)
    want_ids = [m["id"] for m in want]
    seen, page = [], 1
    while True:                                                       # walk every page
        d = c.get("/api/prospects", params={**f, "page": page, "page_size": 37}).json()
        assert d["total"] == len(want)
        seen += [i["id"] for i in d["items"]]
        if not d["has_more"]:
            break
        page += 1
    assert seen == want_ids, "pages must cover the model exactly, in order, without gaps or duplicates"
    s = c.get("/api/prospects/summary", params=f).json()
    mappable = {m["id"] for m in want if m["geom"]}
    assert s["total"] == len(want) and s["with_coordinates"] == len(mappable)
    assert s["without_coordinates"] == len(want) - len(mappable)
    assert sum(s["by_qualification"].values()) == len(want)
    p = c.get("/api/prospects/points", params=f).json()
    assert {pt[0] for pt in p["points"]} == mappable and p["returned"] == len(mappable) and not p["truncated"]
    assert p["total"] == len(want) and p["with_coordinates"] == len(mappable)


def test_items_carry_status_fields_that_match_the_model(api):
    c, model = api
    by_id = {m["id"]: m for m in model}
    items = c.get("/api/prospects", params={"limit": 5000}).json()["items"]
    assert items
    for it in items:
        m = by_id[it["id"]]
        assert it["website_status"] == website_status(m)
        assert it["website_listed"] == (website_status(m) == "listed")
        assert it["has_phone"] == m["phone"] and it["has_email"] == m["email"]
        assert it["county"] == COUNTIES[m["fips"]] and it["qualification"] == m["qual"]
        assert (it["bos"], it["lps"]) == ((m["bos"], m["lps"]) if m["scored"] else (None, None))  # current, not superseded


def test_legacy_mode_reports_truncation_and_limit_zero(api):
    c, model = api
    total = len(expected(model))
    d = c.get("/api/prospects", params={"limit": 10}).json()
    assert d["count"] == 10 and d["total"] == total and d["truncated"] is True
    d = c.get("/api/prospects", params={"limit": 0}).json()
    assert d["count"] == 0 and d["items"] == [] and d["total"] == total


def test_points_truncation_against_the_database(api, monkeypatch):
    c, model = api
    monkeypatch.setattr(business, "MAX_POINTS", 5)
    p = c.get("/api/prospects/points").json()
    assert p["returned"] == 5 and p["truncated"] is True and p["with_coordinates"] > 5


def test_page_beyond_the_end_is_empty_not_an_error(api):
    c, model = api
    d = c.get("/api/prospects", params={"page": 999, "page_size": 50}).json()
    assert d["items"] == [] and d["has_more"] is False and d["total"] == len(expected(model))


# ---------------------------------------------------------------- exports use the same filter (S3)

EXPORT_FILTERS = [{}, {"county": "duval"}, {"website": "none_recorded", "phone": "true"},
                  {"operating": "closed"}, {"q": "50%"}, {"qualification": "unscored", "sort": "name"}]


@pytest.mark.parametrize("f", EXPORT_FILTERS, ids=lambda f: ",".join(f"{k}={v}" for k, v in f.items()) or "all")
def test_csv_export_matches_the_list(api, f):
    import csv
    import io
    c, model = api
    want = [m["id"] for m in expected(model, **f)]
    r = c.get("/api/prospects/export.csv", params=f)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="leadintel-prospects-')
    assert r.headers["x-leadintel-total"] == str(len(want)) and r.headers["x-leadintel-exported"] == str(len(want))
    assert r.headers["x-leadintel-truncated"] == "false"
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [int(x["id"]) for x in rows] == want, "export rows must be exactly the list's rows, in the same order"
    assert c.get("/api/prospects", params={**f, "limit": 0}).json()["total"] == len(want)


def test_csv_export_reports_truncation(api):
    import csv
    import io
    c, model = api
    total = len(expected(model))
    r = c.get("/api/prospects/export.csv", params={"limit": 7})
    assert r.headers["x-leadintel-total"] == str(total) and r.headers["x-leadintel-exported"] == "7"
    assert r.headers["x-leadintel-truncated"] == "true" and len(list(csv.DictReader(io.StringIO(r.text)))) == 7


def test_csv_export_streams_in_chunks_without_losing_rows(api, monkeypatch):
    import csv
    import io
    monkeypatch.setattr(business, "EXPORT_CHUNK_ROWS", 7)            # many chunks, including a partial last one
    c, model = api
    want = [m["id"] for m in expected(model)]
    text = c.get("/api/prospects/export.csv").text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [int(r["id"]) for r in rows] == want and len(want) > 7 * 10
    assert text.count("id,display_name,") == 1                          # header written once
    assert rows[0].keys() >= {"id", "display_name", "phones", "missing"}


def test_cli_export_uses_the_same_filter(api, db, tmp_path):
    import csv
    from sqlalchemy.orm import Session as S
    from app.api.filters import ProspectFilter
    from app.reports.prospects import export_counts, export_csv
    _, model = api
    flt = ProspectFilter(counties=["duval"], website=["none_recorded"], operating=["open", "unknown"])
    want = [m["id"] for m in expected(model, county="duval", website="none_recorded", operating="open,unknown")]
    with S(db) as s:
        out = tmp_path / "x.csv"
        assert export_csv(s, out, flt=flt) == len(want)
        assert export_counts(s, flt, 3) == {"total": len(want), "exported": min(3, len(want)), "limit": 3,
                                            "truncated": len(want) > 3}
    assert [int(r["id"]) for r in csv.DictReader(open(out, encoding="utf-8"))] == want


def test_empty_export_still_has_a_header(api):
    c, _ = api
    r = c.get("/api/prospects/export.csv", params={"county": "nassau"})
    assert r.headers["x-leadintel-total"] == "0" and r.text.splitlines()[0].startswith("id,display_name,")
