"""Search Area (M3 step 1, PLAN.md §14.2-14.3) against PostgreSQL + PostGIS.

Runs only with LEADINTEL_TEST_DATABASE_URL (a *_test database); synthetic businesses only. Distances are checked
against an independent ellipsoidal computation (Vincenty on WGS84, below), not against PostGIS itself.
"""
import csv
import io
import math
import random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import business
from app.api.filters import AREA_MAX_M, ProspectFilter, area_envelope
from app.api.main import create_app
from app.db.models import Address, Area, ContactPoint, Location, LocationContact, Qualification, Score
from app.normalize.core import name_norm

pytestmark = pytest.mark.usefixtures("db")
MI = 1609.344
CENTER = (30.30, -81.65)                       # lat, lon (Jacksonville area)
R10 = 10 * MI
TOKEN = "area-test-token-" + "x" * 30
ORIGIN = "http://127.0.0.1:8765"


# ---------------------------------------------------------------- independent geodesic distance (WGS84, Vincenty)
def vincenty_m(lat1, lon1, lat2, lon2) -> float:
    a, f = 6378137.0, 1 / 298.257223563
    b = (1 - f) * a
    L = math.radians(lon2 - lon1)
    U1, U2 = math.atan((1 - f) * math.tan(math.radians(lat1))), math.atan((1 - f) * math.tan(math.radians(lat2)))
    sU1, cU1, sU2, cU2 = math.sin(U1), math.cos(U1), math.sin(U2), math.cos(U2)
    lam = L
    for _ in range(200):
        sl, cl = math.sin(lam), math.cos(lam)
        ss = math.hypot(cU2 * sl, cU1 * sU2 - sU1 * cU2 * cl)
        if ss == 0:
            return 0.0
        cs = sU1 * sU2 + cU1 * cU2 * cl
        sig = math.atan2(ss, cs)
        sa = cU1 * cU2 * sl / ss
        c2a = 1 - sa * sa
        c2sm = cs - 2 * sU1 * sU2 / c2a if c2a else 0.0
        C = f / 16 * c2a * (4 + f * (4 - 3 * c2a))
        prev = lam
        lam = L + (1 - C) * f * sa * (sig + C * ss * (c2sm + C * cs * (-1 + 2 * c2sm * c2sm)))
        if abs(lam - prev) < 1e-12:
            break
    u2 = c2a * (a * a - b * b) / (b * b)
    A = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    B = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))
    ds = B * ss * (c2sm + B / 4 * (cs * (-1 + 2 * c2sm * c2sm) - B / 6 * c2sm * (-3 + 4 * ss * ss) * (-3 + 4 * c2sm * c2sm)))
    return b * A * (sig - ds)


def test_reference_distance_is_correct():
    # Flinders Peak -> Buninyong, Vincenty (1975) test line: 54,972.271 m
    d = vincenty_m(-(37 + 57 / 60 + 3.72030 / 3600), 144 + 25 / 60 + 29.52440 / 3600,
                   -(37 + 39 / 60 + 10.15610 / 3600), 143 + 55 / 60 + 35.38390 / 3600)
    assert abs(d - 54972.271) < 0.01


# ---------------------------------------------------------------- synthetic data
def _area(s, key, geoid, box):
    x0, y0, x1, y1 = box
    s.add(Area(key=key, name=key.title(), kind="county", spec={"geoid": geoid}, is_target=True, enabled=True,
               geom=f"SRID=4326;MULTIPOLYGON((({x0} {y0},{x0} {y1},{x1} {y1},{x1} {y0},{x0} {y0})))"))


def _loc(s, name, lat=None, lon=None, fips="12031", **kw):
    loc = Location(display_name=name, name_norm=name_norm(name), county_fips=fips,
                   geom=f"SRID=4326;POINT({lon} {lat})" if lat is not None else None, **kw)
    s.add(loc)
    s.flush()
    return loc


def _project(db, lat, lon, dist, bearing_deg):
    with db.connect() as c:
        g = c.execute(text("SELECT ST_Y(p::geometry), ST_X(p::geometry) FROM (SELECT ST_Project("
                           "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :d, radians(:b)) p) q"),
                      {"lat": lat, "lon": lon, "d": dist, "b": bearing_deg}).first()
    return g[0], g[1]


def _db_distance(db, lat, lon, plat, plon):
    with db.connect() as c:
        return c.execute(text("SELECT ST_Distance(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, "
                              "ST_SetSRID(ST_MakePoint(:plon, :plat), 4326)::geography)"),
                         {"lat": lat, "lon": lon, "plat": plat, "plon": plon}).scalar()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", db.url.render_as_string(hide_password=False))
    business._engine.cache_clear()
    yield TestClient(create_app(SimpleNamespace(diagnostics=None), token=TOKEN), base_url=ORIGIN)
    business._engine.cache_clear()


def _area_params(lat=CENTER[0], lon=CENTER[1], radius=10, unit="mi"):
    return {"near_lat": lat, "near_lon": lon, "radius": radius, "unit": unit}


def _ids(c, **params):
    out, page = [], 1
    while True:
        d = c.get("/api/prospects", params={"qualification": "", "page": page, "page_size": 200, **params})
        assert d.status_code == 200, d.text
        d = d.json()
        out += [r["id"] for r in d["items"]]
        if not d["has_more"]:
            break
        page += 1
    assert len(out) == len(set(out)) == d["total"], "pages must have no gaps or duplicates"
    return set(out)


# ---------------------------------------------------------------- boundary inclusion
@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 225, 270, 333])
def test_boundary_is_inclusive_at_every_bearing(db, client, bearing):
    with Session(db) as s:
        _area(s, "duval", "12031", (-83, 29, -80, 32))
        s.commit()
    lat, lon = _project(db, *CENTER, R10, bearing)
    exact = _db_distance(db, *CENTER, lat, lon)          # the stored point's own distance, to the last bit
    pts = {}
    with Session(db) as s:
        pts["on"] = _loc(s, "On boundary", lat, lon).id
        for tag, d in (("in", R10 - 1), ("out", R10 + 1)):
            pts[tag] = _loc(s, f"Point {tag}", *_project(db, *CENTER, d, bearing)).id
        s.commit()
    # radius exactly equal to the boundary point's own distance: that point must be inside (inclusive)
    inside = _ids(client, near_lat=CENTER[0], near_lon=CENTER[1], radius=exact / 1000, unit="km")
    assert pts["on"] in inside and pts["in"] in inside and pts["out"] not in inside
    # independent check of the set-up: the reference distances agree with the intent to well under a millimetre
    with db.connect() as c:
        for tag, want in (("in", R10 - 1), ("out", R10 + 1)):
            plat, plon = c.execute(text("SELECT ST_Y(geom), ST_X(geom) FROM location WHERE id = :i"), {"i": pts[tag]}).first()
            assert abs(vincenty_m(*CENTER, plat, plon) - want) < 0.01


# ---------------------------------------------------------------- the main synthetic model
@pytest.fixture
def model(db, client):
    """300 businesses around the center in two target counties, with varied attributes; 25 without coordinates."""
    rnd = random.Random(20261106)
    rows = []
    with Session(db) as s:
        _area(s, "duval", "12031", (-81.65, 29.5, -80.5, 31.2))
        _area(s, "clay", "12019", (-82.5, 29.5, -81.65, 31.2))
        for i in range(325):
            has_geom = i >= 25
            lat = CENTER[0] + rnd.uniform(-0.35, 0.35) if has_geom else None
            lon = CENTER[1] + rnd.uniform(-0.40, 0.40) if has_geom else None
            fips = "12031" if (lon is None and i % 2) or (lon is not None and lon >= -81.65) else "12019"
            niche = rnd.choice(["cleaning", "landscaping"])
            op = rnd.choice(["open", "open", "permanently_closed", None])
            loc = _loc(s, f"Area Biz {i}", lat, lon, fips, industry_profile=niche, operating_status=op)
            phone = rnd.random() < 0.5
            if phone:
                cp = ContactPoint(kind="phone", value_norm=f"+1904{i:07d}", display=f"(904) {i:07d}")
                s.add(cp)
                s.flush()
                s.add(LocationContact(location_id=loc.id, contact_point_id=cp.id, source_key="overture", active=True))
            bos, lps = round(rnd.uniform(0, 100), 1), round(rnd.uniform(0, 100), 1)
            s.add(Score(location_id=loc.id, kind="bos", stage="provisional", value=bos))
            s.add(Score(location_id=loc.id, kind="lps", stage="provisional", value=lps))
            qual = rnd.choice(["provisionally_qualified", "needs_research", "not_qualified"])
            s.add(Qualification(location_id=loc.id, status=qual, reasons=[], stage="provisional"))
            approx = has_geom and i % 37 == 0
            if approx:
                s.add(Address(owner_type="location", owner_id=loc.id, role="principal", line1="x", city="Jax",
                              state="FL", zip5="32207", country="US", norm_key=f"k{i}", geocode_method="zip_centroid",
                              is_operating_location=True))
            rows.append({"id": loc.id, "lat": lat, "lon": lon, "fips": fips, "niche": niche, "op": op, "phone": phone,
                         "bos": bos, "lps": lps, "qual": qual, "approx": approx})
        s.commit()
    for r in rows:
        r["d"] = vincenty_m(*CENTER, r["lat"], r["lon"]) if r["lat"] is not None else None
    return rows


def _expect(model, radius_m, pred=lambda r: True):
    """Model answer; points within 0.5 m of the boundary are reported separately (formula differences there)."""
    inside = {r["id"] for r in model if r["d"] is not None and r["d"] <= radius_m - 0.5 and pred(r)}
    edge = {r["id"] for r in model if r["d"] is not None and abs(r["d"] - radius_m) < 0.5 and pred(r)}
    return inside, edge


@pytest.mark.parametrize("radius_mi", [0.25, 3, 10, 18])
def test_area_matches_an_independent_geodesic_computation(client, model, radius_mi):
    got = _ids(client, **_area_params(radius=radius_mi))
    inside, edge = _expect(model, radius_mi * MI)
    assert inside <= got and got - inside <= edge


def test_miles_and_kilometres_give_identical_results(client, model):
    mi = _ids(client, **_area_params(radius=10, unit="mi"))
    km = _ids(client, **_area_params(radius=16.09344, unit="km"))
    assert mi == km and mi
    s_mi = client.get("/api/prospects/summary", params={"qualification": "", **_area_params(radius=10)}).json()
    s_km = client.get("/api/prospects/summary", params={"qualification": "", **_area_params(radius=16.09344, unit="km")}).json()
    assert s_mi["total"] == s_km["total"] and abs(s_mi["area"]["radius_m"] - s_km["area"]["radius_m"]) < 1e-6


COMBOS = [
    ({}, lambda r: True),
    ({"county": "duval"}, lambda r: r["fips"] == "12031"),
    ({"county": "clay"}, lambda r: r["fips"] == "12019"),
    ({"niche": "cleaning"}, lambda r: r["niche"] == "cleaning"),
    ({"operating": "open"}, lambda r: r["op"] == "open"),
    ({"operating": "unknown"}, lambda r: r["op"] is None),
    ({"phone": "true"}, lambda r: r["phone"]),
    ({"phone": "false"}, lambda r: not r["phone"]),
    ({"qualification": "needs_research"}, lambda r: r["qual"] == "needs_research"),
    ({"bos_min": 40, "lps_max": 70}, lambda r: r["bos"] >= 40 and r["lps"] <= 70),
    ({"county": "duval", "niche": "landscaping", "phone": "true", "lps_min": 30},
     lambda r: r["fips"] == "12031" and r["niche"] == "landscaping" and r["phone"] and r["lps"] >= 30),
]


@pytest.mark.parametrize("extra,pred", COMBOS)
def test_list_count_summary_points_and_export_agree_with_combined_filters(client, model, extra, pred):
    params = {"qualification": "", **_area_params(radius=12), **extra}
    if "qualification" in extra:
        params["qualification"] = extra["qualification"]
    got = _ids(client, **params)
    inside, edge = _expect(model, 12 * MI, pred)
    assert inside <= got and got - inside <= edge
    summary = client.get("/api/prospects/summary", params=params).json()
    points = client.get("/api/prospects/points", params=params).json()
    r = client.get("/api/prospects/export.csv", params=params)
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert summary["total"] == points["total"] == len(got) == int(r.headers["X-LeadIntel-Total"]) == len(rows)
    assert {p[0] for p in points["points"]} == got == {int(x["id"]) for x in rows}
    assert summary["without_coordinates"] == 0                     # everything inside an area has a location
    no_loc = sum(1 for m in model if m["lat"] is None and pred(m))
    assert summary["area"]["excluded_no_location"] == points["area"]["excluded_no_location"] == no_loc
    # an area never widens a filter: the area result is a subset of the same filter without the area
    without = {k: v for k, v in params.items() if k not in ("near_lat", "near_lon", "radius", "unit")}
    assert got <= _ids(client, **without)


def test_clearing_the_area_restores_the_previous_result_exactly(client, model):
    base = {"county": "duval", "niche": "cleaning"}
    before = client.get("/api/prospects", params={"qualification": "", "page_size": 200, **base}).json()
    client.get("/api/prospects", params={"qualification": "", "page_size": 200, **base, **_area_params()})
    after = client.get("/api/prospects", params={"qualification": "", "page_size": 200, **base}).json()
    assert before == after
    assert "distance_m" not in before["items"][0]
    summ = client.get("/api/prospects/summary", params={"qualification": "", **base}).json()
    assert "area" not in summ                                        # off: responses exactly as before


def test_distance_column_and_sort(client, model):
    d = client.get("/api/prospects", params={"qualification": "", "page_size": 200, "sort": "distance",
                                             **_area_params(radius=8)}).json()
    dist = [r["distance_m"] for r in d["items"]]
    assert dist == sorted(dist) and all(x <= 8 * MI for x in dist)
    by_id = {m["id"]: m for m in model}
    for r in d["items"]:
        assert abs(r["distance_m"] - by_id[r["id"]]["d"]) < 0.5      # PostGIS vs independent Vincenty
    r = client.get("/api/prospects/export.csv", params={"qualification": "", "sort": "distance", **_area_params(radius=8)})
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [int(x["id"]) for x in rows] == [x["id"] for x in d["items"]]
    assert all(abs(float(x["distance_mi"]) * MI - float(x["distance_m"])) < 1 for x in rows)


def test_approximate_locations_are_flagged_and_precise_only_excludes_exactly_them(client, model):
    params = {"qualification": "", **_area_params(radius=25)}
    allr, page = [], 1
    while True:                                                      # every page, not just the first
        d = client.get("/api/prospects", params={**params, "page": page, "page_size": 200}).json()
        allr += d["items"]
        if not d["has_more"]:
            break
        page += 1
    approx = {r["id"] for r in allr if r["approximate_location"]}
    assert approx == {m["id"] for m in model if m["approx"] and m["id"] in {r["id"] for r in allr}} and approx
    precise = _ids(client, **params, precise_only="true")
    assert precise == {r["id"] for r in allr} - approx


def test_sales_filters_combine_with_the_area(db, client, model):
    from app.crm import access
    from app.crm import service as crm
    access.forget_writers()
    w = crm.crm_writer(db, "")
    crm.setup_identity(w, "Area Seller", "AS")
    near = sorted((m for m in model if m["d"] is not None), key=lambda m: m["d"])
    mine_in, mine_out = near[0]["id"], near[-1]["id"]                # one inside a 5-mile area, one far outside
    assert near[0]["d"] < 5 * MI < near[-1]["d"]
    for i in (mine_in, mine_out):
        crm.claim(w, i)
    crm.update(w, mine_in, 2, status="contacted")
    assert _ids(client, claim="mine", **_area_params(radius=5)) == {mine_in}
    assert _ids(client, claim="mine") == {mine_in, mine_out}
    assert _ids(client, sales="contacted", **_area_params(radius=5)) == {mine_in}
    assert mine_in not in _ids(client, claim="unclaimed", **_area_params(radius=5))


# ---------------------------------------------------------------- index safety (the box prefilter loses nothing)
@pytest.mark.parametrize("lat", [25.0, 30.3, 45.0, 60.0])
def test_bounding_box_prefilter_never_loses_a_point_inside(db, client, lat):
    with Session(db) as s:
        _area(s, "duval", "12031", (-180, -85, 180, 85))
        s.commit()
    ids = []
    with Session(db) as s:
        for b in range(0, 360, 10):
            ids.append(_loc(s, f"Edge {b}", *_project(db, lat, -81.6, AREA_MAX_M - 5, b)).id)
        s.commit()
    got = _ids(client, near_lat=lat, near_lon=-81.6, radius=100, unit="mi")
    assert set(ids) <= got
    x0, y0, x1, y1 = area_envelope(lat, -81.6, AREA_MAX_M)
    with db.connect() as c:
        outside_box = c.execute(text("SELECT count(*) FROM location WHERE id = ANY(:i) AND NOT (geom && "
                                     "ST_MakeEnvelope(:a, :b, :c, :d, 4326))"),
                                {"i": ids, "a": x0, "b": y0, "c": x1, "d": y1}).scalar()
    assert outside_box == 0


# ---------------------------------------------------------------- validation (422, never reaching SQL)
@pytest.mark.parametrize("params", [
    {"near_lat": 30.3},                                                   # incomplete
    {"near_lat": 30.3, "near_lon": -81.6},                                # no radius
    {"radius": 10},                                                       # no center
    {"near_lat": 30.3, "near_lon": -81.6, "radius": 0.1},                 # below 0.25 mi
    {"near_lat": 30.3, "near_lon": -81.6, "radius": 101},                 # above 100 mi
    {"near_lat": 30.3, "near_lon": -81.6, "radius": 161, "unit": "km"},   # above 100 mi in km
    {"near_lat": 30.3, "near_lon": -81.6, "radius": 0.3, "unit": "km"},   # below 0.4 km
    {"near_lat": 30.3, "near_lon": -81.6, "radius": 10, "unit": "ft"},    # unknown unit
    {"near_lat": 30.3, "near_lon": -81.6, "radius": 5000, "unit": "m"},   # metres are internal only
    {"near_lat": 86, "near_lon": -81.6, "radius": 10},                    # latitude out of range
    {"near_lat": 30.3, "near_lon": -181, "radius": 10},                   # longitude out of range
    {"near_lat": "nan", "near_lon": -81.6, "radius": 10},                 # not finite
    {"near_lat": 30.3, "near_lon": -81.6, "radius": "inf"},
    {"near_lat": "thirty", "near_lon": -81.6, "radius": 10},              # not a number
    {"sort": "distance"},                                                 # distance sort needs an area
    {"precise_only": "true"},                                             # precise_only needs an area
])
@pytest.mark.parametrize("path", ["/api/prospects", "/api/prospects/summary", "/api/prospects/points",
                                  "/api/prospects/export.csv"])
def test_invalid_area_parameters_are_refused(client, params, path):
    assert client.get(path, params=params).status_code == 422


def test_limits_are_accepted_at_the_edges(client, db):
    with Session(db) as s:
        _area(s, "duval", "12031", (-83, 29, -80, 32))
        s.commit()
    for radius, unit in ((0.25, "mi"), (100, "mi"), (0.4, "km"), (160, "km")):
        assert client.get("/api/prospects", params=_area_params(radius=radius, unit=unit)).status_code == 200


# ---------------------------------------------------------------- the circle shown is the circle searched
def test_circle_endpoint_is_the_geodesic_boundary(client, db):
    with Session(db) as s:
        _area(s, "duval", "12031", (-82.0, 30.0, -81.3, 30.6))
        s.commit()
    for radius, unit in ((10, "mi"), (25, "km"), (0.25, "mi")):
        d = client.get("/api/geo/circle", params=_area_params(radius=radius, unit=unit)).json()
        r_m = radius * (MI if unit == "mi" else 1000)
        ring = d["geometry"]["coordinates"][0]
        assert len(ring) >= 64 and ring[0] == ring[-1]
        for lon, lat in ring:
            assert abs(vincenty_m(*CENTER, lat, lon) - r_m) <= r_m * 0.001
        assert abs(d["radius_m"] - r_m) < 1e-6 and d["center"] == [CENTER[1], CENTER[0]]
    small = client.get("/api/geo/circle", params=_area_params(radius=2)).json()
    large = client.get("/api/geo/circle", params=_area_params(radius=60)).json()
    assert small["beyond_target_counties"] is False and large["beyond_target_counties"] is True
    assert client.get("/api/geo/circle", params=_area_params(radius=500)).status_code == 422


def test_filter_object_rejects_bad_types_directly():
    from app.api.filters import FilterError
    for kw in ({"near_lat": True, "near_lon": 1.0, "radius_m": 1000.0},
               {"near_lat": "30", "near_lon": 1.0, "radius_m": 1000.0},
               {"near_lat": 30.0, "near_lon": 1.0, "radius_m": float("inf")}):
        with pytest.raises(FilterError):
            ProspectFilter(**kw)


# ---------------------------------------------------------------- one canonical range in metres, both units
@pytest.mark.parametrize("radius,unit,metres", [
    (0.4, "km", 400.0),                       # canonical minimum
    (400 / 1609.344, "mi", 400.0),            # the same minimum as the browser sends it in miles (0.2485 mi)
    (0.25, "mi", 402.336),
    (100, "mi", 160934.4),                    # canonical maximum
    (160934.4 / 1000, "km", 160934.4),        # the same maximum as the browser sends it in km (160.9344 km)
    (160.9344, "km", 160934.4),
    (160, "km", 160000.0),
])
def test_canonical_limits_are_accepted_in_both_units(client, db, radius, unit, metres):
    with Session(db) as s:
        _area(s, "duval", "12031", (-83, 29, -80, 32))
        s.commit()
    r = client.get("/api/prospects/summary", params=_area_params(radius=radius, unit=unit))
    assert r.status_code == 200, r.text
    assert abs(r.json()["area"]["radius_m"] - metres) < 1e-6
    c = client.get("/api/geo/circle", params=_area_params(radius=radius, unit=unit))
    assert c.status_code == 200 and abs(c.json()["radius_m"] - metres) < 1e-6


@pytest.mark.parametrize("radius,unit", [(0.39999, "km"), (399.99 / 1609.344, "mi"), (0.2485, "mi"),
                                         (160.935, "km"), (100.0001, "mi")])
def test_just_outside_the_canonical_range_is_refused_in_both_units(client, radius, unit):
    for path in ("/api/prospects", "/api/geo/circle"):
        assert client.get(path, params=_area_params(radius=radius, unit=unit)).status_code == 422
