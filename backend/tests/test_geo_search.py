"""Search Area center search (M3): Census Gazetteer ZIP codes and cities, business locations, and the Census
one-line address geocoder. Network calls are always mocked; the database tests use leadintel_test only."""
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from app.config import REPO_ROOT
from app.costs.guard import CostGuard
from app.geo import places
from app.geo.geocode import ONELINE_URL, geocode_one_line
from app.net.http import InfraClient, UnregisteredHostError
from app.net.registry import load_registry
from helpers import make_settings

REG = load_registry(REPO_ROOT / "config" / "network.yaml")
ZCTA_TXT = ("GEOID\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG                                     \n"
            "32207\t1\t1\t1\t1\t30.290745\t-81.636968\n"
            "32202\t1\t1\t1\t1\t30.326436\t-81.649574    \n"
            "10001\t1\t1\t1\t1\t40.750636\t-73.997177\n"
            "3220X\t1\t1\t1\t1\t30.0\t-81.0\n"                          # not a ZIP code
            "32099\t1\t1\t1\t1\tnot-a-number\t-81.0\n")                 # bad coordinate
PLACE_TXT = ("USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
             "FL\t1235000\t02404783\tJacksonville city\t25\tA\t1\t1\t1\t1\t30.336864\t-81.661603\n"
             "FL\t1235050\t02404785\tJacksonville Beach city\t25\tA\t1\t1\t1\t1\t30.272466\t-81.385604\n"
             "FL\t1252125\t02407050\tOrange Park town\t43\tA\t1\t1\t1\t1\t30.170853\t-81.704672\n"
             "FL\t1223000\t02403000\tFruit Cove CDP\t57\tS\t1\t1\t1\t1\t30.11\t-81.62\n"
             "GA\t1300000\t00000000\tFolkston city\t25\tA\t1\t1\t1\t1\t30.83\t-82.01\n")


# ---------------------------------------------------------------- parsing (offline)
def test_parse_zcta_keeps_valid_zip_codes_and_trims_headers():
    z = places.parse_zcta(ZCTA_TXT)
    assert z == {"32207": [30.290745, -81.636968], "32202": [30.326436, -81.649574], "10001": [40.750636, -73.997177]}
    assert set(places.parse_zcta(ZCTA_TXT, keep={"32207", "32202"})) == {"32207", "32202"}


def test_parse_places_strips_area_type_and_keeps_only_the_state():
    p = places.parse_places(PLACE_TXT, "FL")
    assert [x[0] for x in p] == ["Fruit Cove", "Jacksonville", "Jacksonville Beach", "Orange Park"]


def _doc():
    return {"format": 1, "year": "2024", "state": "FL", "zcta": places.parse_zcta(ZCTA_TXT),
            "places": places.parse_places(PLACE_TXT, "FL")}


def _write_doc(data_dir, doc=None):
    p = places.gazetteer_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc or _doc()))
    return p


def test_find_zip_and_places(tmp_path):
    _write_doc(tmp_path)
    doc = places.load_gazetteer(tmp_path)
    assert places.find_zip(doc, "32207")[0]["lat"] == 30.290745
    assert places.find_zip(doc, "99999") == [] and places.find_zip(doc, "3220") == []
    assert [r["label"] for r in places.find_places(doc, "jackson")] == ["Jacksonville, FL", "Jacksonville Beach, FL"]
    assert [r["label"] for r in places.find_places(doc, "Orange Park, FL")] == ["Orange Park, FL"]
    assert [r["label"] for r in places.find_places(doc, "park")] == ["Orange Park, FL"]       # contains, after prefix
    assert places.find_places(doc, "j") == []                                                 # too short
    assert all(r["precision"].endswith("(approximate)") for r in places.find_places(doc, "jack"))


def test_load_gazetteer_is_cached_and_rejects_unknown_formats(tmp_path):
    assert places.load_gazetteer(tmp_path) is None                        # before the first Load Data
    p = _write_doc(tmp_path)
    a = places.load_gazetteer(tmp_path)
    assert places.load_gazetteer(tmp_path) is a                           # cached while the file is unchanged
    p.write_text(json.dumps({**_doc(), "format": 99}))
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert places.load_gazetteer(tmp_path) is None
    p.write_text("{broken")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 10))
    assert places.load_gazetteer(tmp_path) is None


# ---------------------------------------------------------------- download and reduce (Load Data step)
class FakeCensus:
    def __init__(self, zcta: bytes, place: bytes):
        self.files = {places.ZCTA_URL: zcta, places.PLACE_URL.format(year=places.GAZETTEER_YEAR, fips="12"): place}
        self.urls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.urls.append(url)
        body = self.files.get(url)
        if body is None:
            return httpx.Response(404)
        hdr = {"Last-Modified": format_datetime(datetime(2024, 8, 21, tzinfo=timezone.utc), usegmt=True),
               "Content-Length": str(len(body))}
        return httpx.Response(200, headers=hdr, content=b"" if req.method == "HEAD" else body)


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2024_Gaz_zcta_national.txt", text)
    return buf.getvalue()


def _client(tmp_path, handler):
    settings = make_settings(tmp_path)
    return InfraClient(REG, settings, CostGuard(REG, settings), transport=httpx.MockTransport(handler))


@pytest.mark.usefixtures("unlocked_files")
def test_refresh_writes_the_reduced_gazetteer(tmp_path):
    fake = FakeCensus(_zip(ZCTA_TXT), PLACE_TXT.encode())
    with _client(tmp_path, fake.handler) as c:
        res = places.refresh_gazetteer(c, tmp_path / "data", {"32207", "32202"})
    assert res == {"zip_codes": 2, "places": 4, "year": "2024"}
    doc = places.load_gazetteer(tmp_path / "data")
    assert set(doc["zcta"]) == {"32207", "32202"} and doc["state"] == "FL"
    assert all(u.startswith("https://www2.census.gov/") for u in fake.urls)


@pytest.mark.usefixtures("unlocked_files")
def test_refresh_keeps_the_previous_file_when_the_download_looks_empty(tmp_path):
    good = FakeCensus(_zip(ZCTA_TXT), PLACE_TXT.encode())
    with _client(tmp_path, good.handler) as c:
        places.refresh_gazetteer(c, tmp_path / "data", None)
    before = places.gazetteer_path(tmp_path / "data").read_text()
    empty = FakeCensus(_zip(ZCTA_TXT.splitlines()[0] + "\n"), b"USPS\tNAME\n")
    with _client(tmp_path, empty.handler) as c, pytest.raises(ValueError, match="looks empty"):
        places.refresh_gazetteer(c, tmp_path / "data", None)
    assert places.gazetteer_path(tmp_path / "data").read_text() == before


def test_refresh_needs_the_maintenance_lock(tmp_path):
    from app.db.locks import UnlockedWrite
    fake = FakeCensus(_zip(ZCTA_TXT), PLACE_TXT.encode())
    with _client(tmp_path, fake.handler) as c, pytest.raises(UnlockedWrite):
        places.refresh_gazetteer(c, tmp_path / "data", None)


# ---------------------------------------------------------------- one-line geocoder (offline, mocked)
def _match(x, y, label="117 W DUVAL ST, JACKSONVILLE, FL, 32202"):
    return {"coordinates": {"x": x, "y": y}, "matchedAddress": label}


def _geo_handler(payload, calls):
    def handler(req):
        calls.append(req)
        if callable(payload):
            return payload(req)
        return httpx.Response(200, json=payload) if not isinstance(payload, (bytes, str)) else httpx.Response(200, content=payload)
    return handler


def test_geocode_one_line_parses_matches_and_drops_bad_coordinates(tmp_path):
    calls = []
    payload = {"result": {"addressMatches": [_match(-81.6588, 30.3297), _match("x", 30.0), _match(-81.0, float("nan")),
                                              _match(10.0, 89.0), {"coordinates": None}, "junk", _match(-81.7, 30.2, "OTHER")]}}
    raw = json.dumps(payload, allow_nan=True).encode()            # NaN as a JSON parser would accept it
    with _client(tmp_path, _geo_handler(raw, calls)) as c:
        out = geocode_one_line(c, "117 W Duval St, Jacksonville, FL")
    assert out == [{"label": "117 W DUVAL ST, JACKSONVILLE, FL, 32202", "lat": 30.3297, "lon": -81.6588},
                   {"label": "OTHER", "lat": 30.2, "lon": -81.7}]
    assert str(calls[0].url).startswith(ONELINE_URL) and calls[0].url.params["benchmark"] == "Public_AR_Current"
    assert calls[0].url.params["address"] == "117 W Duval St, Jacksonville, FL"


@pytest.mark.parametrize("payload", [b"<html>not json</html>", {"result": {}}, {"result": {"addressMatches": "x"}}, []])
def test_geocode_one_line_returns_nothing_for_malformed_answers(tmp_path, payload):
    with _client(tmp_path, _geo_handler(payload, [])) as c:
        assert geocode_one_line(c, "1 Main St, Jacksonville, FL") == []


def test_geocoder_client_only_reaches_registered_hosts(tmp_path):
    with _client(tmp_path, lambda req: httpx.Response(200)) as c, pytest.raises(UnregisteredHostError):
        c.get("https://evil.example/geocoder?address=x")


# ---------------------------------------------------------------- API (leadintel_test, synthetic)
@pytest.fixture
def api(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.api import business
    from app.api.main import create_app
    from app.db.models import Area, Location
    from app.normalize.core import name_norm
    with Session(db) as s:
        s.add(Area(key="duval", name="Duval", kind="county", spec={"geoid": "12031"}, is_target=True, enabled=True,
                   geom="SRID=4326;MULTIPOLYGON(((-82 30,-82 30.6,-81.5 30.6,-81.5 30,-82 30)))"))
        ids = {}
        for name, geom in (("Riverside Cleaning", "POINT(-81.68 30.31)"), ("Riverside Lawn", None),
                           ("Riverside Old", "POINT(-81.6 30.3)")):
            loc = Location(display_name=name, name_norm=name_norm(name), county_fips="12031",
                           geom=f"SRID=4326;{geom}" if geom else None)
            s.add(loc)
            s.flush()
            ids[name] = loc.id
        s.get(Location, ids["Riverside Old"]).merged_into_id = ids["Riverside Cleaning"]
        s.commit()
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", db.url.render_as_string(hide_password=False))
    monkeypatch.setenv("LEADINTEL_DATA_DIR", str(tmp_path / "data"))
    business._engine.cache_clear()
    calls = []
    state = SimpleNamespace(calls=calls, payloads=[])

    def handler(req):
        calls.append(req)
        p = state.payloads.pop(0) if state.payloads else {"result": {"addressMatches": []}}
        return p if isinstance(p, httpx.Response) else httpx.Response(200, json=p)

    monkeypatch.setattr(business, "_geocoder_client", lambda: _client(tmp_path, handler))
    yield TestClient(create_app(SimpleNamespace(diagnostics=None)), base_url="http://127.0.0.1:8765"), ids, state, tmp_path
    business._engine.cache_clear()


def test_businesses_are_found_by_name_with_their_own_location(api):
    c, ids, _, _ = api
    r = c.get("/api/geo/search", params={"q": "riverside"}).json()
    biz = [x for x in r["results"] if x["kind"] == "business"]
    assert [x["id"] for x in biz] == [ids["Riverside Cleaning"]]        # no-location and merged businesses left out
    assert biz[0]["lat"] == 30.31 and biz[0]["in_target_counties"] is True


def test_zip_and_city_need_the_gazetteer_and_say_so(api):
    c, _, _, tmp = api
    r = c.get("/api/geo/search", params={"q": "32207"}).json()
    assert not [x for x in r["results"] if x["kind"] == "zip"]
    assert any("after the next Load Data" in n for n in r["notes"])
    _write_doc(tmp / "data")
    r = c.get("/api/geo/search", params={"q": "32207"}).json()
    z = [x for x in r["results"] if x["kind"] == "zip"]
    assert z and z[0]["lat"] == 30.290745 and z[0]["in_target_counties"] is True and not r["notes"]
    r = c.get("/api/geo/search", params={"q": "Jacksonville Beach"}).json()
    city = [x for x in r["results"] if x["kind"] == "city"]
    assert city[0]["label"] == "Jacksonville Beach, FL" and city[0]["in_target_counties"] is False   # east of the county box
    r = c.get("/api/geo/search", params={"q": "10001"}).json()
    assert r["results"][0]["in_target_counties"] is False


def test_addresses_are_looked_up_only_when_asked(api):
    c, _, st, tmp = api
    _write_doc(tmp / "data")
    r = c.get("/api/geo/search", params={"q": "117 W Duval St"}).json()
    assert st.calls == [] and any("Press Enter" in n for n in r["notes"])
    for q in ("Jacksonville", "32207", "riverside"):
        c.get("/api/geo/search", params={"q": q, "online": "true"})
    assert st.calls == []                                                 # not address-like: never online
    st.payloads = [{"result": {"addressMatches": []}}, {"result": {"addressMatches": [_match(-81.6588, 30.3297)]}}]
    r = c.get("/api/geo/search", params={"q": "117 W Duval St", "online": "true"}).json()
    assert [x["kind"] for x in r["results"]] == ["address"] and r["results"][0]["in_target_counties"] is True
    assert [q.url.params["address"] for q in st.calls] == ["117 W Duval St", "117 W Duval St, FL"]   # Florida retry


def test_address_lookup_failures_are_reported_and_local_results_kept(api):
    c, _, st, tmp = api
    _write_doc(tmp / "data")
    st.payloads = [httpx.Response(503), httpx.Response(503)]
    r = c.get("/api/geo/search", params={"q": "117 W Duval St, Jacksonville, FL", "online": "true"}).json()
    assert r["results"] == [] and any("unavailable right now" in n for n in r["notes"])
    st.payloads = [{"result": {"addressMatches": []}}]
    r = c.get("/api/geo/search", params={"q": "9999 Nowhere Rd, Jacksonville, FL 32207", "online": "true"}).json()
    assert len(st.calls) == 2 and any("No street address matched" in n for n in r["notes"])   # state given: no retry


def test_search_input_is_bounded_and_hostile_text_is_harmless(api):
    c, _, st, _ = api
    assert c.get("/api/geo/search", params={"q": "x" * 201}).status_code == 422
    assert c.get("/api/geo/search", params={"q": " "}).json() == {"query": "", "results": [], "notes": []}
    for q in ("%", "_", "'; DROP TABLE location; --", "<img src=x onerror=alert(1)>", "\\"):
        assert c.get("/api/geo/search", params={"q": q}).status_code == 200
    assert st.calls == []
