import hashlib

import httpx
import pytest

from app.costs.guard import CostGuard
from app.net.http import InfraClient
from app.net.mirrors import AllMirrorsFailed, MirrorSet
from app.net.probe import ProbeResult
from app.net.store import NetStore
from app.sources.osm.extracts import GeofabrikExtracts, plan_osm_access
from helpers import HTTPTestServer, make_settings, ok, registry, status, svc

REGION = "north-america/us/testland"
PBF = b"\x00\x00\x00\x0e\n\tOSMHeader" + bytes(range(256)) * 400
MD5 = hashlib.md5(PBF).hexdigest()


def mirror_routes(pbf=PBF, md5=MD5):
    return {f"/{REGION}-latest.osm.pbf.md5": ok(f"{md5}  testland-latest.osm.pbf\n".encode()),
            f"/{REGION}-latest.osm.pbf": ok(pbf, "application/octet-stream")}


@pytest.fixture
def mirrors():
    broken = HTTPTestServer({"*": status(503, b"overloaded")})
    good = HTTPTestServer(mirror_routes())
    yield broken, good
    broken.close()
    good.close()


def setup(tmp_path, broken, good, store=None):
    reg = registry([
        svc("geofabrik_main", broken.port, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{broken.port}/"}],
            capability="osm_extract", priority=1),
        svc("geofabrik_ext2", good.port, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{good.port}/"}],
            capability="osm_extract", priority=2),
        svc("overpass", 9, [{"key": "p", "kind": "http", "url": "http://127.0.0.1:9/"}], capability="osm_overpass"),
    ], capabilities={"osm_extract": True, "osm_overpass": False})
    s = make_settings(tmp_path)
    store = store or NetStore("sqlite://")
    client = InfraClient(reg, s, CostGuard(reg, s))
    return reg, s, store, client, GeofabrikExtracts(reg, client, store, s.data_dir)


def test_falls_back_to_second_mirror_verifies_md5_and_is_incremental(tmp_path, mirrors):
    broken, good = mirrors
    reg, s, store, client, gx = setup(tmp_path, broken, good)
    r1 = gx.ensure(REGION)
    assert r1.downloaded and r1.service_key == "geofabrik_ext2" and r1.path.read_bytes() == PBF
    assert store.last_successful_download("geofabrik_ext2")[1] == len(PBF)
    assert any(not e.ok for e in store.recent_events("geofabrik_main"))  # the failure was recorded
    n_before = len(good.requests)
    r2 = gx.ensure(REGION)
    assert not r2.downloaded  # md5 unchanged -> no re-download
    assert len(good.requests) == n_before + 1  # only the .md5 was fetched


def test_md5_mismatch_never_installs_file(tmp_path, mirrors):
    broken, good = mirrors
    good.routes = mirror_routes(md5="0" * 32)
    reg, s, store, client, gx = setup(tmp_path, broken, good)
    with pytest.raises(AllMirrorsFailed) as ei:
        gx.ensure(REGION)
    assert "MD5 mismatch" in str(ei.value)
    assert not list((s.data_dir / "osm").glob("*.osm.pbf"))


def test_resumes_partial_download(tmp_path, mirrors):
    broken, good = mirrors
    reg, s, store, client, gx = setup(tmp_path, broken, good)
    target = s.data_dir / "osm" / (REGION.replace("/", "__") + "-latest.osm.pbf")
    part = target.with_suffix(target.suffix + ".part")
    part.write_bytes(PBF[:5000])
    part.with_suffix(".expect").write_text(MD5)
    r = gx.ensure(REGION)
    assert r.path.read_bytes() == PBF
    ranges = [h.get("Range") for path, h in good.requests if path.endswith(".osm.pbf")]
    assert ranges == ["bytes=5000-"]


def _store_with(statuses: dict[str, str]):
    from datetime import datetime, timezone
    store = NetStore("sqlite://")
    rows = []
    for key, outcome in statuses.items():
        r = ProbeResult(key, "p", "http", "h", 443, None, datetime.now(timezone.utc))
        r.outcome = outcome
        r.failure_kind = None if outcome == "success" else "connection_reset"
        r.network_reached = True
        rows.append(r)
    store.save_probe_results("r1", rows)
    return store


def test_mirror_order_uses_health_then_priority(tmp_path, mirrors):
    broken, good = mirrors
    reg, *_ = setup(tmp_path, broken, good)
    ms = MirrorSet(reg, _store_with({"geofabrik_main": "failure", "geofabrik_ext2": "success"}), "osm_extract")
    assert [s.key for s in ms.services] == ["geofabrik_ext2", "geofabrik_main"]
    calls = []

    def op(s):
        calls.append(s.key)
        if s.key == "geofabrik_ext2":
            raise httpx.ConnectError("boom")
        return "ok"
    value, used, attempts = ms.run(op)
    assert value == "ok" and used.key == "geofabrik_main" and [a.ok for a in attempts] == [False, True]


def test_osm_plan_prefers_extracts_and_never_hides_unavailability(tmp_path, mirrors):
    broken, good = mirrors
    reg, *_ = setup(tmp_path, broken, good)
    assert plan_osm_access(reg, NetStore("sqlite://"), 5000).strategy == "extract"  # untested -> try extract
    dead = _store_with({"geofabrik_main": "failure", "geofabrik_ext2": "failure"})
    assert plan_osm_access(reg, dead, 50).strategy == "overpass"
    big = plan_osm_access(reg, dead, 5000)
    assert big.strategy == "unavailable" and "too large" in big.reason
    all_dead = _store_with({"geofabrik_main": "failure", "geofabrik_ext2": "failure", "overpass": "failure"})
    assert plan_osm_access(reg, all_dead, 10).strategy == "unavailable"
