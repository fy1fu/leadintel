"""Coverage semantics of a discovery run, with every network call mocked."""
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import shapefile
from sqlalchemy import text

from app.config import REPO_ROOT, Settings
from app.costs.guard import CostGuard
from app.net.http import InfraClient
from app.net.registry import load_registry
from app.pipeline.discover import run_discovery

COUNTIES = {"12031": "Duval County", "12109": "St. Johns County", "12019": "Clay County", "12001": "Alachua County"}


def county_zip() -> bytes:
    """A tiny cartographic-boundary style shapefile with four square 'counties'."""
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    for f in ("STATEFP", "GEOID", "NAMELSAD"):
        w.field(f, "C", 40)
    for i, (geoid, name) in enumerate(COUNTIES.items()):
        x = -82.0 + i * 0.2
        w.poly([[[x, 30.0], [x, 30.2], [x + 0.2, 30.2], [x + 0.2, 30.0], [x, 30.0]]])
        w.record("12", geoid, name)
    w.close()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for ext, b in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            z.writestr(f"cb_2025_us_county_500k.{ext}", b.getvalue())
    return out.getvalue()


def test_failed_source_is_recorded_as_failed_not_as_zero_businesses(db, tmp_path, monkeypatch):
    blob = county_zip()

    def handler(req: httpx.Request):
        if req.url.host == "www2.census.gov":
            if req.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(blob))})
            return httpx.Response(200, content=blob)
        raise httpx.ConnectError(f"simulated outage of {req.url.host}")

    monkeypatch.setenv("LEADINTEL_DATABASE_URL", str(db.url.render_as_string(hide_password=False)))
    settings = Settings(config_dir=REPO_ROOT / "config", data_dir=tmp_path / "data")
    reg = load_registry(settings.network_registry_path)
    with InfraClient(reg, settings, CostGuard(reg, settings), transport=httpx.MockTransport(handler)) as c:
        res = run_discovery(settings, since=date(2026, 10, 1), until=date(2026, 10, 2), sunbiz=False, client=c, engine=db)
    assert res.status == "partial"
    cov = {(r["source"], r["area"]): r for r in res.coverage}
    assert {a for _, a in cov} == {"duval", "st_johns", "clay"}
    assert all(cov[("overture", a)]["state"] == "failed" and "outage" in cov[("overture", a)]["reason"] for a in ("duval", "st_johns", "clay"))
    assert all(cov[("sunbiz", a)]["state"] == "not_run" for a in ("duval", "st_johns", "clay"))
    assert all(cov[("dbpr", a)]["state"] == "failed" for a in ("duval", "st_johns", "clay"))
    assert res.counts["paid_requests"] == 0
    # M3: the Gazetteer step (Search Area centers) is optional: its failure is recorded, never a coverage row
    assert "error" in res.counts["gazetteer"] and not any(src == "gazetteer" for src, _ in cov)
    with db.connect() as conn:
        assert conn.execute(text("select count(*) from location")).scalar() == 0
        assert conn.execute(text("select status from discovery_run")).scalar() == "partial"
