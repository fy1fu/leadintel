from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import business
from app.api.main import create_app
from app.appconfig.store import ConfigService
from app.config import REPO_ROOT, Settings
from app.costs.guard import CostGuard
from app.net.diagnostics import Diagnostics
from app.net.registry import load_registry
from app.net.service import NetContext
from app.net.store import NetStore
from app.scoring.runner import score_all
from test_scoring_runner import seed


def _client(tmp_path):
    st = Settings(config_dir=REPO_ROOT / "config", data_dir=tmp_path)
    reg = load_registry(st.network_registry_path)
    store, guard = NetStore("sqlite://"), CostGuard(reg, st)
    # LeadIntel accepts only 127.0.0.1/localhost as Host (DNS-rebinding defence), so tests use that base URL.
    return TestClient(create_app(NetContext(st, reg, store, guard, Diagnostics(reg, st, store, guard))),
                      base_url="http://127.0.0.1:8765")


def test_prospect_list_profile_and_page(db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", db.url.render_as_string(hide_password=False))
    business._engine.cache_clear()
    with Session(db) as s:
        l1, _ = seed(s)
        l1_id = l1.id
        score_all(s, ConfigService(Settings(config_dir=REPO_ROOT / "config")).effective(None), ["12031"], today=date(2026, 11, 3))
        s.commit()
    c = _client(tmp_path)
    items = c.get("/api/prospects", params={"qualification": ""}).json()["items"]
    assert {i["name"] for i in items} == {"Bob's Pressure Washing", "Coastal Capital Holdings LLC"}
    p = c.get(f"/api/prospects/{l1_id}").json()
    bos = next(x for x in p["scores"] if x["kind"] == "bos")
    assert bos["stage"] == "provisional" and bos["factors"]
    assert p["contacts"][0]["verification_state"] == "ingested"
    assert c.get("/api/prospects/999999").status_code == 404
    assert "LeadIntel" in c.get("/").text
    business._engine.cache_clear()


def test_business_db_down_returns_503_while_diagnostics_work(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/none")
    business._engine.cache_clear()
    c = _client(tmp_path)
    r = c.get("/api/prospects")
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]
    assert c.get("/api/network/diagnostics").status_code == 200
    business._engine.cache_clear()
