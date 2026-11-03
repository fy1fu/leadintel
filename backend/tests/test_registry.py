import pytest
from pydantic import ValidationError

from app.config import REPO_ROOT
from app.net.registry import Registry, load_registry
from helpers import registry, svc

REAL = load_registry(REPO_ROOT / "config" / "network.yaml")

ESSENTIAL = ["sftp.floridados.gov", "overturemapswestus2.blob.core.windows.net",
             "overturemapswestus2.dfs.core.windows.net", "data.commoncrawl.org",
             "ds5q9oxwqwsfj.cloudfront.net", "download-ext2.geofabrik.de"]


def test_real_registry_lists_essential_hosts():
    hosts = {s.host for s in REAL.services}
    for h in ESSENTIAL:
        assert h in hosts, h


def test_every_service_has_a_concrete_probe():
    for s in REAL.services:
        assert s.probes, s.key
        for p in s.probes:
            if p.kind in ("http", "download"):
                assert p.url or p.resolver, f"{s.key}/{p.key}"


def test_required_capabilities_have_enabled_services():
    for cap, meta in REAL.capabilities.items():
        if meta.required:
            assert any(s.enabled for s in REAL.services_for(cap)), cap


def test_optional_integrations_disabled_by_default():
    assert REAL.service("huggingface").enabled is False
    assert REAL.service("ollama_local").enabled is False


def test_no_paid_host_in_services_and_paid_list_complete():
    paid = REAL.paid_hosts()
    assert not ({s.host for s in REAL.services} & set(paid))
    for h in ["places.googleapis.com", "maps.googleapis.com", "api.anthropic.com", "api.search.brave.com",
              "google.serper.dev", "pagespeedonline.googleapis.com"]:
        assert h in paid


def test_overture_has_three_mirrors_and_osm_prefers_extracts():
    assert [s.key for s in REAL.services_for("overture_places")] == [
        "overture_s3", "overture_azure_blob", "overture_azure_dfs"]
    assert REAL.capabilities["osm_extract"].required is True
    assert REAL.capabilities["osm_overpass"].required is False


def test_validation_rejects_duplicates_and_paid_hosts():
    a = svc("a", 1, [{"key": "p", "kind": "http", "url": "http://127.0.0.1:1/"}])
    with pytest.raises(ValidationError):
        registry([a, a])
    bad = svc("b", 443, [{"key": "p", "kind": "http", "url": "https://maps.googleapis.com/"}], host="maps.googleapis.com")
    with pytest.raises(ValidationError):
        registry([bad])
