"""Opt-in live checks (LEADINTEL_LIVE_TESTS=1). They make small real requests and never touch paid hosts."""
import httpx
import pytest

from app.config import get_settings
from app.costs.guard import CostGuard
from app.net.diagnostics import Diagnostics
from app.net.http import InfraClient
from app.net.registry import load_registry
from app.net.store import NetStore
from app.sources.overture.reader import BBox, OvertureReader

pytestmark = pytest.mark.live
SETTINGS = get_settings()
REG = load_registry(SETTINGS.network_registry_path)
TINY = BBox(-81.3975, 30.2840, -81.3935, 30.2880)  # a few blocks of Jacksonville Beach


@pytest.mark.parametrize("mirror", ["overture_s3", "overture_azure_blob", "overture_azure_dfs"])
def test_overture_mirror_serves_real_places(mirror):
    with InfraClient(REG, SETTINGS, CostGuard(REG, SETTINGS)) as c:
        r = OvertureReader(REG, c)
        r.mirrors.services = [REG.service(mirror)]
        table, stats = r.read_bbox(TINY, columns=["id", "names", "bbox"])
    assert table.num_rows > 0 and stats.services_used == {mirror}


def test_overture_live_failover_when_primary_breaks():
    real = httpx.HTTPTransport(verify=SETTINGS.ssl_context, proxy=SETTINGS.proxy_url)

    class BreakS3(httpx.BaseTransport):
        def handle_request(self, request):
            if request.url.host == REG.service("overture_s3").host:
                raise httpx.ConnectError("simulated S3 outage")
            return real.handle_request(request)

    with InfraClient(REG, SETTINGS, CostGuard(REG, SETTINGS), transport=BreakS3()) as c:
        table, stats = OvertureReader(REG, c).read_bbox(TINY, columns=["id", "bbox"])
    assert table.num_rows > 0 and "overture_s3" not in stats.services_used


def test_live_diagnostics_are_consistent_and_free():
    s = SETTINGS
    g = CostGuard(REG, s)
    d = Diagnostics(REG, s, NetStore("sqlite://"), g)
    d.run()
    rep = d.full_report()
    assert rep["paid_requests_made"] == []
    for sv in rep["services"]:
        if sv["status"] == "operational":
            assert sv["probes"] and all(p["outcome"] == "success" for p in sv["probes"])
