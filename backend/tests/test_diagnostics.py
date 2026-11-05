from fastapi.testclient import TestClient

from app.api.main import create_app
from app.costs.guard import CostGuard
from app.net.diagnostics import Diagnostics, render_markdown, render_text, service_status
from app.net.probe import ProbeResult, Prober
from app.net.service import NetContext
from app.net.store import NetStore
from helpers import HTTPTestServer, RawServer, free_port, make_settings, ok, registry, reset_immediately, svc


def pr(outcome, kind=None, reached=None):
    from datetime import datetime, timezone
    r = ProbeResult("s", "p", "http", "h", 443, None, datetime.now(timezone.utc))
    r.outcome, r.failure_kind, r.network_reached = outcome, kind, reached
    return r


def test_status_rollup_rules():
    assert service_status(False, []) == ("disabled", None)
    assert service_status(True, [pr("skipped")])[0] == "not_tested"
    assert service_status(True, [pr("success", reached=True)])[0] == "operational"
    assert service_status(True, [pr("success", reached=True), pr("failure", "http_status", True)])[0] == "degraded"
    assert service_status(True, [pr("failure", "connection_reset", True)]) == ("reachable_not_operational", True)
    assert service_status(True, [pr("failure", "proxy_denied", False)]) == ("blocked_by_network_policy", False)
    assert service_status(True, [pr("failure", "dns_resolution_failed")])[0] == "dns_failure"
    assert service_status(True, [pr("failure", "tcp_timeout")])[0] == "unreachable_timeout"


def build(tmp_path):
    good = HTTPTestServer({"/f": ok(b"PK\x03\x04data")})
    bad = RawServer(reset_immediately)
    dead = free_port()
    reg = registry([
        svc("primary", bad.port, [{"key": "p", "kind": "http", "url": f"https://localhost:{bad.port}/"}],
            capability="files", host="localhost", scheme="https"),
        svc("mirror", good.port, [{"key": "d", "kind": "download", "url": f"http://127.0.0.1:{good.port}/f",
                                   "range_bytes": 4, "expect_magic": "PK"}], capability="files", priority=2),
        svc("needed", dead, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{dead}/"}], capability="core"),
        svc("optional_off", dead, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{dead}/"}],
            capability="extra", enabled=False),
    ], capabilities={"files": True, "core": True, "extra": False})
    s = make_settings(tmp_path)
    g = CostGuard(reg, s)
    store = NetStore("sqlite://")
    d = Diagnostics(reg, s, store, g, Prober(reg, s, g, resolvers={}))
    return reg, s, store, g, d, (good, bad)


def test_diagnostics_fallback_criticality_and_reports(tmp_path):
    reg, s, store, g, d, servers = build(tmp_path)
    try:
        d.run()
        rep = d.full_report()
        caps = {c["key"]: c for c in rep["capabilities"]}
        assert caps["files"]["status"] == "operational_via_fallback" and caps["files"]["active_service"] == "mirror"
        assert caps["core"]["status"] == "unavailable" and caps["core"]["critical"] is True
        assert caps["extra"]["status"] == "disabled"
        sv = {x["key"]: x for x in rep["services"]}
        assert sv["primary"]["status"] == "reachable_not_operational" and sv["primary"]["network_allowed"] is True
        assert sv["mirror"]["last_successful_download"] and sv["mirror"]["last_download_bytes"] == 4
        assert sv["optional_off"]["status"] == "disabled" and sv["optional_off"]["probes"] == []
        assert "operational_via_fallback" in render_text(rep) and "| files |" in render_markdown(rep)

        app = create_app(NetContext(s, reg, store, g, d), token="test-token")
        c = TestClient(app, base_url="http://127.0.0.1:8765")
        assert c.get("/api/network/diagnostics").json()["latest_run_id"] == rep["latest_run_id"]
        page = c.get("/diagnostics/network").text
        assert "Network diagnostics" in page and "primary" in page and "required capability unavailable" in page
        rerun = c.post("/api/network/diagnostics/run", params={"only": ["mirror"]},
                       headers={"X-LeadIntel-Token": "test-token", "Origin": "http://127.0.0.1:8765"}).json()
        assert rerun["run_id"] != rep["latest_run_id"]
    finally:
        for x in servers:
            x.close()


def test_runtime_download_event_counts_as_last_success(tmp_path):
    reg, s, store, g, d, servers = build(tmp_path)
    try:
        store.record_event("needed", "download", True, url="http://x/", nbytes=123)
        sv = {x.key: x for x in d.service_reports()}
        assert sv["needed"].last_successful_download and sv["needed"].last_download_bytes == 123
        assert sv["needed"].status == "not_tested"  # a past download does not make it "operational" now
    finally:
        for x in servers:
            x.close()


def test_removed_probe_definitions_do_not_linger(tmp_path):
    reg, s, store, g, d, servers = build(tmp_path)
    try:
        from datetime import datetime, timezone
        stale = ProbeResult("mirror", "old_probe", "http", "h", 1, None, datetime.now(timezone.utc))
        stale.outcome = "failure"
        stale.failure_kind = "http_status"
        store.save_probe_results("old", [stale])
        d.run(only=["mirror"])
        sv = {x.key: x for x in d.service_reports()}
        assert [p.probe for p in sv["mirror"].probes] == ["d"] and sv["mirror"].status == "operational"
    finally:
        for x in servers:
            x.close()


def test_skipped_probes_prevent_operational_claim():
    assert service_status(True, [pr("success", reached=True), pr("skipped")]) == ("partially_verified", True)


def test_diagnostics_do_not_depend_on_main_database(tmp_path, monkeypatch):
    from app.config import Settings
    from app.net.service import build_context
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/unreachable")
    monkeypatch.delenv("LEADINTEL_NETDIAG_DB_URL", raising=False)
    monkeypatch.setenv("LEADINTEL_DATA_DIR", str(tmp_path / "data"))
    ctx = build_context(Settings())
    assert ctx.store.mode == "persistent"
    assert str(ctx.store.engine.url).endswith("netdiag.db") and "postgresql" not in str(ctx.store.engine.url)


def test_unopenable_store_falls_back_to_memory_and_diagnostics_still_run(tmp_path, monkeypatch):
    from app.config import Settings
    from app.net.service import build_context
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setenv("LEADINTEL_NETDIAG_DB_URL", f"sqlite:///{blocker}/sub/netdiag.db")
    ctx = build_context(Settings())
    assert ctx.store.mode.startswith("memory (fallback")
    report = ctx.diagnostics.full_report()
    assert report["store"]["mode"].startswith("memory") and report["paid_requests_made"] == []


def test_cli_runs_without_any_store(tmp_path, monkeypatch, capsys):
    from app import cli
    monkeypatch.setenv("LEADINTEL_DATA_DIR", str(tmp_path / "data"))
    rc = cli.main(["net-check", "--no-run", "--no-store", "--json"])
    out = capsys.readouterr()
    assert rc in (0, 2) and '"mode": "memory (persistence disabled)"' in out.out
    assert not (tmp_path / "data" / "netdiag.db").exists()
