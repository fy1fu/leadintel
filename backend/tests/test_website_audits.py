"""Website audits in LeadIntel (M3) against PostgreSQL: migration 0007, the audit service end to end with a real browser
on synthetic sites, storage (pages, findings, artifacts with checksums, WQS score rows, recommendations, benchmark),
artifact path safety, interrupted and failed runs, one audit at a time, the Load Data conflict, the peer benchmark
threshold, BOS/LPS rescoring leaving the WQS alone, the API (token required) and saved searches.

Runs only with LEADINTEL_TEST_DATABASE_URL (a *_test database; see conftest). Synthetic businesses and sites only.
"""
import hashlib
import ipaddress
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.audit import benchmark, service
from app.crm import service as crm
from app.db.locks import OperationInProgress, maintenance_lock
from app.db.models import Location, OnlineProfile
from app.normalize.core import name_norm

pytestmark = pytest.mark.usefixtures("db")
TEST_NET = (ipaddress.ip_network("127.0.0.2/32"),)
AUDIT_TABLES = ("website_audit", "website_audit_page", "website_audit_artifact", "website_audit_finding",
                "website_audit_recommendation", "website_benchmark", "crm_saved_search")


def _location(db, name, *, url=None, niche="lawn_care", fips="12031") -> int:
    with Session(db) as s:
        loc = Location(display_name=name, name_norm=name_norm(name), county_fips=fips, operating_status="open",
                       industry_profile=niche)
        s.add(loc)
        s.flush()
        if url:
            s.add(OnlineProfile(location_id=loc.id, url=url, url_norm=url.lower(), platform="website",
                                platform_kind="independent", source_key="test"))
        s.commit()
        return loc.id


def _alembic(db, target):
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    with db.begin() as conn:
        cfg.attributes["connection"] = conn
        (command.downgrade if target != "head" else command.upgrade)(cfg, target)


# ---------------------------------------------------------------- migration 0007
def test_migration_0007_creates_the_audit_tables(db):
    insp = inspect(db)
    for t in AUDIT_TABLES:
        assert insp.has_table(t), t
    with db.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0007"
    with pytest.raises(Exception):                                   # the status values are enforced
        with db.begin() as c:
            lid = _location(db, "Check Status")
            c.execute(text("INSERT INTO website_audit (location_id, status) VALUES (:l, 'bogus')"), {"l": lid})


def test_migration_0007_downgrade_removes_only_its_tables_and_upgrade_restores_them(db):
    lid = _location(db, "Keep Me", url="http://127.0.0.2:9/")
    with db.begin() as c:
        c.execute(text("INSERT INTO website_audit (location_id, status) VALUES (:l, 'failed')"), {"l": lid})
    try:
        _alembic(db, "0006")
        insp = inspect(db)
        assert not any(insp.has_table(t) for t in AUDIT_TABLES)
        assert insp.has_table("crm_prospect") and insp.has_table("location")
        with db.connect() as c:
            assert c.execute(text("SELECT display_name FROM location WHERE id = :l"), {"l": lid}).scalar() == "Keep Me"
    finally:
        _alembic(db, "head")
    assert all(inspect(db).has_table(t) for t in AUDIT_TABLES)


# ---------------------------------------------------------------- the audit service with a real browser
pw = pytest.importorskip("playwright.sync_api")
from app.config import REPO_ROOT  # noqa: E402
from app.costs.guard import CostGuard  # noqa: E402
from app.crawl.runner import CrawlLimits  # noqa: E402
from app.net.browser_proxy import BrowserEgressProxy  # noqa: E402
from app.net.registry import load_registry  # noqa: E402
from app.net.safe_egress import EgressPolicy  # noqa: E402
from helpers import make_settings  # noqa: E402
from test_website_checks import BAD, GOOD  # noqa: E402
from test_website_fixtures import Server, page  # noqa: E402


def _deps(tmp_path, ports, **limits):
    reg = load_registry(REPO_ROOT / "config" / "network.yaml")

    def launch(p):
        try:
            return p.chromium.launch(args=BrowserEgressProxy.chromium_args())
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium available: {exc}")
    return service.AuditDeps(launch_browser=launch, guard=CostGuard(reg, make_settings(tmp_path)),
                             policy=EgressPolicy(allowed_ports=frozenset(ports), _test_allow_networks=TEST_NET),
                             limits=CrawlLimits(per_host_interval_s=0.02, page_timeout_s=8, page_budget_s=40, **limits))


@pytest.fixture
def good_site():
    s = Server("127.0.0.2", GOOD)
    yield s
    s.close()


def _rows(db, sql, **kw):
    with db.connect() as c:
        return [dict(r) for r in c.execute(text(sql), kw).mappings()]


def test_audit_end_to_end_stores_evidence_scores_and_screenshots(db, tmp_path, good_site):
    lid = _location(db, "Acme Lawn", url=good_site.origin + "/")
    data = tmp_path / "data"
    aid = service.run_audit(db, data, lid, _deps(tmp_path, {good_site.port}))
    a = _rows(db, "SELECT * FROM website_audit WHERE id = :i", i=aid)[0]
    assert a["status"] == "completed" and a["pages_crawled"] == 3 and a["finished_at"] and a["robots"] == "none"
    assert 0 < a["wqs"] <= 100 and a["wqs_version"] == "wqs-1.0" and a["wqs_status"] in ("provisional", "low confidence")
    assert set(a["dimensions"]) == {"visual", "ux", "effectiveness", "technical", "content", "seo", "modernity"}
    assert a["dimensions"]["visual"]["score"] is None                       # not assessed, never poor
    assert a["observations"]["timing"]["bytes"] > 0
    findings = _rows(db, "SELECT * FROM website_audit_finding WHERE audit_id = :i", i=aid)
    assert {f["kind"] for f in findings} == {"measured", "heuristic"} and all(f["evidence"] and f["source"] for f in findings)
    assert any(f["passed"] is None for f in findings)                       # unknown stays unknown
    # the WQS joins the existing score model (kind 'wqs') with explainable per-dimension factors
    sc = _rows(db, "SELECT * FROM score WHERE location_id = :l AND kind = 'wqs'", l=lid)
    assert len(sc) == 1 and sc[0]["superseded_at"] is None and sc[0]["value"] == a["wqs"] and a["score_id"] == sc[0]["id"]
    assert sc[0]["config_versions"]["audit_id"] == aid and sc[0]["config_versions"]["calibration"] == "provisional"
    factors = _rows(db, "SELECT * FROM score_factor WHERE score_id = :s", s=sc[0]["id"])
    assert {f["factor"] for f in factors} <= set(a["dimensions"]) and all(f["component"] == "wqs_dimension" for f in factors)
    # screenshots: inside <data>/crawl, checksums match, served only through the API helper
    arts = _rows(db, "SELECT * FROM website_audit_artifact WHERE audit_id = :i", i=aid)
    assert len(arts) >= 4 and {x["kind"] for x in arts} == {"screenshot_desktop", "screenshot_mobile"}
    for x in arts:
        p = data / x["rel_path"]
        assert p.resolve().is_relative_to((data / "crawl").resolve()) and p.stat().st_size == x["bytes"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == x["sha256"]
        with db.connect() as c:
            assert service.artifact_bytes(c, data, x["id"])[:2] == b"\xff\xd8"           # JPEG
    # nothing typed or sent: the site only ever received GET requests
    assert not good_site.others
    # the read model used by the evidence viewer
    with db.connect() as c:
        d = service.detail(c, aid)
        h = service.history(c, lid)
    assert len(d["pages"]) == 3 and d["pages"][0]["artifacts"] and d["calibration_note"]
    assert d["benchmark"]["scope"] == "none" and "Not enough comparable data" in d["benchmark"]["message"]
    assert [x["id"] for x in h] == [aid] and d["previous"] is None


def test_bad_site_gets_ordered_recommendations_and_a_rerun_keeps_history(db, tmp_path):
    s = Server("127.0.0.2", BAD)
    lid = _location(db, "Old Site", url=s.origin + "/")
    try:
        first = service.run_audit(db, tmp_path / "data", lid, _deps(tmp_path, {s.port}))
        second = service.run_audit(db, tmp_path / "data", lid, _deps(tmp_path, {s.port}))
    finally:
        s.close()
    recs = _rows(db, "SELECT * FROM website_audit_recommendation WHERE audit_id = :i ORDER BY priority", i=second)
    assert 3 <= len(recs) <= 5 and [r["priority"] for r in recs] == list(range(1, len(recs) + 1))
    assert recs[0]["severity"] == "high" and all(r["evidence"] and r["why"] and r["suggestion"] for r in recs)
    with db.connect() as c:
        d = service.detail(c, second)
        assert [x["id"] for x in service.history(c, lid)] == [second, first]
    assert d["previous"]["id"] == first and d["previous"]["dimensions"]
    current = _rows(db, "SELECT config_versions->>'audit_id' a FROM score WHERE location_id = :l AND kind = 'wqs' "
                        "AND superseded_at IS NULL", l=lid)
    assert current == [{"a": str(second)}]                                  # the newer audit supersedes the older


def test_robots_blocked_site_is_recorded_without_a_score(db, tmp_path):
    s = Server("127.0.0.2", {"/": page("x", "y")}, robots=(200, "User-agent: *\nDisallow: /\n"))
    lid = _location(db, "No Robots", url=s.origin + "/")
    try:
        aid = service.run_audit(db, tmp_path / "data", lid, _deps(tmp_path, {s.port}))
    finally:
        s.close()
    a = _rows(db, "SELECT status, wqs, reason FROM website_audit WHERE id = :i", i=aid)[0]
    assert a["status"] == "robots_disallowed" and a["wqs"] is None and "robots.txt" in a["reason"]
    assert _rows(db, "SELECT id FROM score WHERE location_id = :l", l=lid) == []
    assert [x["path"] for x in [{"path": g[0]} for g in s.gets]] == ["/robots.txt"]


def test_partially_loaded_site_is_partial(db, tmp_path):
    s = Server("127.0.0.2", {"/": page("Home", '<h1>Home</h1><a href="/gone">Gone</a>')})
    lid = _location(db, "Half Site", url=s.origin + "/")
    try:
        aid = service.run_audit(db, tmp_path / "data", lid, _deps(tmp_path, {s.port}, screenshots=False))
    finally:
        s.close()
    a = _rows(db, "SELECT status, reason, wqs FROM website_audit WHERE id = :i", i=aid)[0]
    assert a["status"] == "partial" and "1 of 2 pages" in a["reason"] and a["wqs"] is not None


def test_stale_running_audit_is_marked_interrupted_and_a_crash_marks_failed(db, tmp_path):
    lid = _location(db, "Crashy", url="http://127.0.0.2:9/")
    with db.begin() as c:
        stale = c.execute(text("INSERT INTO website_audit (location_id, status) VALUES (:l, 'running') RETURNING id"),
                          {"l": lid}).scalar()
    deps = _deps(tmp_path, {9})
    deps.launch_browser = lambda p: (_ for _ in ()).throw(RuntimeError("browser would not start"))
    with pytest.raises(RuntimeError):
        service.run_audit(db, tmp_path / "data", lid, deps)
    rows = {r["id"]: r for r in _rows(db, "SELECT id, status, reason, finished_at FROM website_audit WHERE location_id = :l", l=lid)}
    assert rows[stale]["status"] == "interrupted" and rows[stale]["finished_at"]
    (new,) = [r for i, r in rows.items() if i != stale]
    assert new["status"] == "failed" and "could not be started" in new["reason"] and "browser would not start" in new["reason"]


def test_no_website_is_refused_before_anything_is_stored(db, tmp_path):
    lid = _location(db, "Offline Only")
    with pytest.raises(service.AuditUnavailable):
        service.run_audit(db, tmp_path / "data", lid, _deps(tmp_path, {9}))
    assert _rows(db, "SELECT id FROM website_audit") == []


def test_screenshots_are_kept_for_the_newest_three_audits_only(db, tmp_path, good_site):
    lid = _location(db, "Retained", url=good_site.origin + "/")
    data = tmp_path / "data"
    ids = [service.run_audit(db, data, lid, _deps(tmp_path, {good_site.port})) for _ in range(4)]
    with_art = {r["audit_id"] for r in _rows(db, "SELECT DISTINCT audit_id FROM website_audit_artifact")}
    assert with_art == set(ids[1:])
    files = sorted(p.relative_to(data).as_posix() for p in (data / "crawl").rglob("*.jpg"))
    stored = sorted(r["rel_path"] for r in _rows(db, "SELECT rel_path FROM website_audit_artifact"))
    assert files == stored                                          # the oldest audit's files are gone, nothing else
    assert len(_rows(db, "SELECT id FROM website_audit_finding WHERE audit_id = :i", i=ids[0])) > 0   # evidence kept
    with db.connect() as c:
        assert service.detail(c, ids[0])["pages"][0]["artifacts"] == []


def test_expired_file_removal_never_leaves_the_crawl_folder(tmp_path, unlocked_files):
    data = tmp_path / "data"
    (data / "crawl" / "a" / "run").mkdir(parents=True)
    keep = tmp_path / "keep.txt"
    keep.write_text("x")
    (data / "crawl" / "a" / "run" / "1.jpg").write_bytes(b"j")
    service._remove_files(data, ["crawl/a/run/1.jpg", "../keep.txt", str(keep), "crawl", "crawl/../keep.txt"])
    assert keep.exists() and (data / "crawl").is_dir() and not (data / "crawl" / "a").exists()


# ---------------------------------------------------------------- storage safety
def test_artifacts_outside_the_crawl_folder_or_with_a_wrong_checksum_are_refused(db, tmp_path):
    data = tmp_path / "data"
    (data / "crawl" / "x").mkdir(parents=True)
    good = data / "crawl" / "x" / "a.jpg"
    good.write_bytes(b"\xff\xd8 fake jpeg")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve")
    digest = hashlib.sha256(good.read_bytes()).hexdigest()
    assert service._artifact_ok(data, {"path": "crawl/x/a.jpg", "sha256": digest})
    assert not service._artifact_ok(data, {"path": "crawl/x/a.jpg", "sha256": "0" * 64})
    assert not service._artifact_ok(data, {"path": "../secret.txt", "sha256": hashlib.sha256(b"do not serve").hexdigest()})
    assert not service._artifact_ok(data, {"path": "crawl/../../secret.txt", "sha256": "x"})
    lid = _location(db, "Art")
    with db.begin() as c:
        aid = c.execute(text("INSERT INTO website_audit (location_id, status) VALUES (:l, 'completed') RETURNING id"),
                        {"l": lid}).scalar()
        ids = [c.execute(text("INSERT INTO website_audit_artifact (audit_id, kind, rel_path, sha256, bytes) "
                              "VALUES (:a, 'screenshot_desktop', :p, :h, 1) RETURNING id"), {"a": aid, "p": p, "h": h}).scalar()
               for p, h in [("crawl/x/a.jpg", digest), ("../secret.txt", hashlib.sha256(b"do not serve").hexdigest()),
                            (str(secret), hashlib.sha256(b"do not serve").hexdigest()), ("crawl/x/a.jpg", "0" * 64)]]
    with db.connect() as c:
        got = [service.artifact_bytes(c, data, i) for i in ids]
        assert service.artifact_bytes(c, data, 999999) is None
    assert got == [good.read_bytes(), None, None, None]


# ---------------------------------------------------------------- one audit at a time, and Load Data first
def test_only_one_audit_runs_at_a_time(db, tmp_path, monkeypatch):
    release, started = threading.Event(), threading.Event()

    def slow(engine, data_dir, location_id, deps, requested_by=None, on_started=None):
        on_started(41)
        started.set()
        release.wait(10)
        return 41
    monkeypatch.setattr(service, "run_audit", slow)
    jobs = service.AuditJobs()
    jobs.start(db, tmp_path, 1, None)
    assert started.wait(5) and jobs.status()["state"] == "running" and jobs.status()["audit_id"] == 41
    with pytest.raises(service.AuditUnavailable, match="another website audit"):
        jobs.start(db, tmp_path, 2, None)
    release.set()
    for _ in range(100):
        if not jobs.busy():
            break
        time.sleep(0.05)
    assert jobs.status()["state"] == "done" and not jobs.status()["running"]


def test_an_audit_waits_for_load_data_instead_of_running_beside_it(db, tmp_path):
    lid = _location(db, "Busy", url="http://127.0.0.2:9/")
    jobs = service.AuditJobs()
    with maintenance_lock(db, "Load Data", data_dir=tmp_path):
        jobs.start(db, tmp_path, lid, _deps(tmp_path, {9}))
        for _ in range(100):
            if not jobs.busy():
                break
            time.sleep(0.05)
    st = jobs.status()
    assert st["state"] == "error" and "Load Data" in st["message"]
    assert _rows(db, "SELECT id FROM website_audit") == []
    refused = []                                                # and the reverse: Load Data waits for an audit

    def load_data():
        try:
            with maintenance_lock(db, "Load Data", data_dir=tmp_path):
                pass
        except OperationInProgress as exc:
            refused.append(str(exc))
    with maintenance_lock(db, "website audit", data_dir=tmp_path):
        t = threading.Thread(target=load_data)
        t.start()
        t.join(10)
    assert refused and "website audit" in refused[0]


# ---------------------------------------------------------------- benchmark
def _peer(db, name, wqs, *, niche="lawn_care", fips="12031", status="completed"):
    lid = _location(db, name, niche=niche, fips=fips)
    with db.begin() as c:
        aid = c.execute(text("INSERT INTO website_audit (location_id, status, wqs) VALUES (:l, :s, :w) RETURNING id"),
                        {"l": lid, "s": status, "w": wqs}).scalar()
        c.execute(text("INSERT INTO website_audit_finding (audit_id, criterion, dimension, kind, passed, weight, evidence, source) "
                       "VALUES (:a, 'https', 'technical', 'measured', :p, 1, 'e', 's')"), {"a": aid, "p": wqs >= 50})
    return lid


@pytest.fixture
def counties(db):
    with db.begin() as c:
        for key, name, fips, target in [("duval", "Duval", "12031", True), ("clay", "Clay", "12019", True),
                                        ("far", "Far", "12086", False)]:
            c.execute(text("INSERT INTO area (key, name, kind, spec, is_target, enabled) VALUES "
                           "(:k, :n, 'county', CAST(:s AS jsonb), :t, true)"),
                      {"k": key, "n": name, "s": f'{{"geoid": "{fips}"}}', "t": target})


def test_benchmark_needs_eight_peers_of_the_same_niche(db, counties):
    me = _location(db, "Me", url="http://127.0.0.2:9/")
    for i in range(7):
        _peer(db, f"Peer {i}", 40 + 5 * i)
    _peer(db, "Other niche", 90, niche="cleaning")                 # never mixed in
    _peer(db, "Failed audit", 10, status="failed")                  # only completed / partial audits count
    with db.connect() as c:
        b = benchmark.compute(c, me, 60)
    assert b["scope"] == "none" and b["peers"] == 7 and "Not enough comparable data" in b["message"]
    _peer(db, "Peer 7", 75)
    with db.connect() as c:
        b = benchmark.compute(c, me, 60)
    assert b["scope"] == "county" and b["peers"] == 8 and b["area"] == "Duval County"
    assert b["median"] == 57.5 and b["percentile"] == 62                  # 5 of 8 peers at or below 60
    assert b["criteria"]["https"] == {"passed": 6, "assessed": 8} and b["audited_from"] and b["computed_at"]
    assert "lawn care businesses in Duval County" in b["message"]


def test_benchmark_widens_to_the_target_region_but_never_beyond(db, counties):
    me = _location(db, "Me", url="http://127.0.0.2:9/")
    for i in range(4):
        _peer(db, f"Duval {i}", 50 + i)
    for i in range(4):
        _peer(db, f"Clay {i}", 60 + i, fips="12019")
    for i in range(5):
        _peer(db, f"Far {i}", 99, fips="12086")                     # outside the target counties
    with db.connect() as c:
        b = benchmark.compute(c, me, 70)
    assert b["scope"] == "region" and b["peers"] == 8 and b["percentile"] == 100


def test_benchmark_without_an_industry():
    class C:
        def execute(self, *a, **kw):
            return SimpleNamespace(first=lambda: (None, "12031", "Duval"))
    assert benchmark.compute(C(), 1, 50)["scope"] == "none"


# ---------------------------------------------------------------- BOS/LPS rescoring leaves the WQS alone
def test_rescoring_bos_and_lps_keeps_the_current_wqs(db):
    lid = _location(db, "Scored")
    with db.begin() as c:
        c.execute(text("INSERT INTO score (location_id, kind, stage, value, caps_applied, config_versions) "
                       "VALUES (:l, 'wqs', 'provisional', 66, '[]', '{}')"), {"l": lid})
    from datetime import date

    from app.appconfig.store import ConfigService
    from app.config import Settings
    from app.scoring.runner import score_all
    cfg = ConfigService(Settings(config_dir=REPO_ROOT / "config")).effective(None)
    with Session(db) as s:
        score_all(s, cfg, ["12031"], today=date(2026, 11, 6))
        s.commit()
    kinds = {r["kind"]: r for r in _rows(db, "SELECT kind, value FROM score WHERE location_id = :l AND superseded_at IS NULL", l=lid)}
    assert kinds["wqs"]["value"] == 66 and {"bos", "lps"} <= set(kinds)


# ---------------------------------------------------------------- API
TOKEN = "m3-test-token-" + "x" * 30
ORIGIN = "http://127.0.0.1:8765"
H = {"X-LeadIntel-Token": TOKEN, "Origin": ORIGIN}


@pytest.fixture
def api(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import business
    from app.api.main import create_app
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", db.url.render_as_string(hide_password=False))
    monkeypatch.setenv("LEADINTEL_DATA_DIR", str(tmp_path / "data"))
    business._engine.cache_clear()
    monkeypatch.setattr(service, "JOBS", service.AuditJobs())
    from app.crm import access
    access.forget_writers()
    yield TestClient(create_app(SimpleNamespace(diagnostics=None), token=TOKEN), base_url=ORIGIN)
    business._engine.cache_clear()


def test_api_starting_an_audit_needs_the_token_and_a_website(api, db):
    no_site = _location(db, "No Site")
    with_site = _location(db, "Site", url="http://127.0.0.2:9/")
    assert api.post(f"/api/locations/{with_site}/audits", headers={"Origin": ORIGIN}).status_code == 403
    assert api.post(f"/api/locations/{with_site}/audits",
                    headers={"X-LeadIntel-Token": TOKEN, "Origin": "https://evil.example"}).status_code == 403
    assert api.post(f"/api/locations/{no_site}/audits", headers=H).status_code == 409
    info = api.get(f"/api/locations/{no_site}/audits").json()
    assert info == {"website": None, "can_audit": False, "crawler_available": True, "audits": []}
    assert api.get("/api/audits/status").json()["state"] == "idle"
    assert api.get("/api/audits/12345").status_code == 404
    assert api.get("/api/audits/artifacts/12345").status_code == 404
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM website_audit")).scalar() == 0


def test_api_full_audit_flow_through_the_real_app_engine(api, db, tmp_path, good_site, monkeypatch):
    lid = _location(db, "Acme API", url=good_site.origin + "/")
    monkeypatch.setattr(service, "default_deps", lambda settings: _deps(tmp_path, {good_site.port}))
    r = api.post(f"/api/locations/{lid}/audits", headers=H)
    assert r.status_code == 202
    for _ in range(600):
        st = api.get("/api/audits/status").json()
        if not st["running"]:
            break
        time.sleep(0.1)
    assert st["state"] == "done", st
    info = api.get(f"/api/locations/{lid}/audits").json()
    assert info["can_audit"] and info["audits"][0]["status"] == "completed"
    d = api.get(f"/api/audits/{st['audit_id']}").json()
    assert d["wqs"] and d["findings"] and d["benchmark"]["scope"] == "none"
    art = d["pages"][0]["artifacts"][0]["id"]
    img = api.get(f"/api/audits/artifacts/{art}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content[:2] == b"\xff\xd8"
    assert img.headers["x-content-type-options"] == "nosniff"
    prof = api.get(f"/api/prospects/{lid}").json()
    assert any(s["kind"] == "wqs" for s in prof["scores"])


def test_api_without_the_crawler_component_says_so(api, db, monkeypatch):
    lid = _location(db, "Site", url="http://127.0.0.2:9/")
    monkeypatch.setattr(service, "crawler_available", lambda: False)
    r = api.post(f"/api/locations/{lid}/audits", headers=H)
    assert r.status_code == 503 and "crawler component" in r.json()["detail"]
    assert api.get(f"/api/locations/{lid}/audits").json()["can_audit"] is False


# ---------------------------------------------------------------- saved searches
def test_saved_searches_round_trip_validation_and_api(api, db):
    lid = _location(db, "Any")
    assert lid
    assert api.get("/api/crm/saved-searches").json() == []
    params = {"county": "duval,clay", "niche": "lawn_care", "qualification": "provisionally_qualified",
              "website": "none_recorded", "phone": "true", "lps_min": "40", "sort": "name", "sales": "contacted",
              "claim": "mine", "near": "30.3322,-81.6557", "r": "16093.44", "ru": "mi", "nl": "Downtown", "view": "map"}
    assert api.post("/api/crm/saved-searches", json={"name": "Hot", "params": params}, headers={"Origin": ORIGIN}).status_code == 403
    assert api.post("/api/crm/saved-searches", json={"name": "Hot", "params": params}, headers=H).status_code == 409  # profile first
    assert api.post("/api/crm/me", json={"display_name": "Test Seller", "initials": "TS"}, headers=H).status_code == 200
    saved = api.post("/api/crm/saved-searches", json={"name": "Hot", "params": params}, headers=H).json()
    assert saved["version"] == 1
    again = api.post("/api/crm/saved-searches", json={"name": "Hot", "params": {**params, "view": "list"}}, headers=H).json()
    assert again["id"] == saved["id"] and again["version"] == 2
    listed = api.get("/api/crm/saved-searches").json()
    assert len(listed) == 1 and listed[0]["params"] == {**params, "view": "list"}      # restored exactly
    for bad in [{"evil": "1"}, {"r": "5000"}, {"near": "30,-81", "r": "10", "ru": "mi"}, {"near": "x", "r": "5000"},
                {"near": "30,-81", "r": "5000", "ru": "ft"}, {"view": "3d"}, {"phone": "yes"}, {"lps_min": "lots"},
                {"sort": "random"}, {"q": "x" * 1001}, {"qualification": "wonderful"}, {}]:
        r = api.post("/api/crm/saved-searches", json={"name": "Bad", "params": bad}, headers=H)
        assert r.status_code in (400, 422), (bad, r.status_code)
    assert api.delete(f"/api/crm/saved-searches/{saved['id']}", headers={"Origin": ORIGIN}).status_code == 403
    assert api.delete(f"/api/crm/saved-searches/{saved['id']}", headers=H).status_code == 200
    assert api.delete(f"/api/crm/saved-searches/{saved['id']}", headers=H).status_code == 404
    assert api.get("/api/crm/saved-searches").json() == []


def test_saved_search_params_are_validated_like_a_live_search():
    ok = crm._check_saved_params({"near": "30.33,-81.66", "r": "400", "ru": "km", "all": "1"})
    assert ok["r"] == "400"
    with pytest.raises(crm.CrmError):
        crm._check_saved_params({"near": "30.33,-81.66", "r": "399", "ru": "km"})        # below the canonical minimum
    with pytest.raises(crm.CrmError):
        crm._check_saved_params({"near": "30.33,-81.66", "r": "160935", "ru": "mi"})     # above 100 miles
