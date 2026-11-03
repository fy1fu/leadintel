import httpx
import pytest

from app.config import REPO_ROOT
from app.costs.guard import CostGuard, PaidCallBlocked
from app.net.diagnostics import Diagnostics
from app.net.http import InfraClient, UnregisteredHostError
from app.net.probe import Connector, ProbeFailure, Prober
from app.net.registry import load_registry
from app.net.store import NetStore
from helpers import make_settings

REAL = load_registry(REPO_ROOT / "config" / "network.yaml")


def test_paid_blocked_by_default_and_needs_both_switches(tmp_path):
    g = CostGuard(REAL, make_settings(tmp_path))
    with pytest.raises(PaidCallBlocked):
        g.check("places.googleapis.com")
    g.check("overturemaps-us-west-2.s3.us-west-2.amazonaws.com")  # free host: no-op
    assert not CostGuard(REAL, make_settings(tmp_path, allow_paid=True)).is_allowed("google_places")
    assert not CostGuard(REAL, make_settings(tmp_path, enabled_paid_providers=["google_places"])).is_allowed("google_places")
    assert CostGuard(REAL, make_settings(tmp_path, allow_paid=True, enabled_paid_providers=["google_places"])).is_allowed("google_places")


def test_infra_client_blocks_paid_and_unregistered_before_network(tmp_path):
    s = make_settings(tmp_path)
    g = CostGuard(REAL, s)

    def handler(request):
        raise AssertionError(f"network reached for {request.url}")
    with InfraClient(REAL, s, g, transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(PaidCallBlocked):
            c.get("https://places.googleapis.com/v1/places:searchText")
        with pytest.raises(UnregisteredHostError):
            c.get("https://random-site.example/")
    assert g.paid_requests_made() == []
    assert [a.allowed for a in g.attempts] == [False]


class RecordingConnector(Connector):
    """Stands in for the network: records every host the diagnostics try to contact."""

    def __init__(self, settings):
        super().__init__(settings)
        self.hosts: list[str] = []

    def open(self, host, port, result, timeout):
        self.hosts.append(host)
        raise ProbeFailure("tcp", "connection_refused", "offline test")


def test_full_diagnostics_run_makes_no_paid_requests(tmp_path):
    s = make_settings(tmp_path)
    g = CostGuard(REAL, s)
    conn = RecordingConnector(s)
    d = Diagnostics(REAL, s, NetStore("sqlite://"), g, Prober(REAL, s, g, connector=conn))
    d.run()
    report = d.full_report()
    paid = set(REAL.paid_hosts())
    assert conn.hosts and not (set(conn.hosts) & paid)
    assert "huggingface.co" not in conn.hosts and "localhost" not in conn.hosts  # disabled optionals untouched
    assert report["paid_requests_made"] == []
    assert all(p["status"] == "blocked_by_policy" and p["contacted"] is False for p in report["paid_providers"])
    # nothing is claimed to work when nothing succeeded
    assert all(sv["status"] != "operational" for sv in report["services"])
