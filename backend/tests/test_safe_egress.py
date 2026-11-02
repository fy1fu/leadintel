import ipaddress

import pytest

from app.costs.guard import CostGuard
from app.net.safe_egress import (BlockedDestination, EgressPolicy, SafeBackend, SafeFetcher, hostname_block_reason,
                                 ip_block_reason, parse_ip_literal)
from helpers import HTTPTestServer, make_cert, make_settings, ok, redirect, registry, server_tls_context, svc

REG = registry([svc("dummy", 1, [{"key": "p", "kind": "http", "url": "http://127.0.0.1:1/"}])])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.1.1", "169.254.169.254",
                                "100.64.1.1", "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "fe80::1",
                                "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1",
                                "64:ff9b::a00:1", "168.63.129.16", "100.100.100.200", "fd00:ec2::254"])
def test_blocked_addresses(ip):
    assert ip_block_reason(ipaddress.ip_address(ip)) is not None


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.215.14", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert ip_block_reason(ipaddress.ip_address(ip)) is None


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "017700000001", "127.1", "0x7f000001", "[::1]"])
def test_legacy_ip_encodings_are_recognised_and_blocked(host):
    ip = parse_ip_literal(host)
    assert ip is not None and ip_block_reason(ip) is not None


@pytest.mark.parametrize("host,blocked", [("localhost", True), ("printer.local", True), ("db.internal", True),
                                          ("metadata.google.internal", True), ("intranet", True),
                                          ("example.com", False), ("1password.com", False), ("123.com", False)])
def test_hostname_rules(host, blocked):
    assert (hostname_block_reason(host) is not None) is blocked


def fetcher(tmp_path, resolver, *, allow_lo=False, ports=None, proxy=None, ca=None, **pol):
    s = make_settings(tmp_path, ca_bundle=ca)
    policy = EgressPolicy(allowed_ports=frozenset(ports or {80, 443}),
                          _test_allow_networks=(ipaddress.ip_network("127.0.0.1/32"),) if allow_lo else (), **pol)
    return SafeFetcher(CostGuard(REG, s), ssl_context=s.ssl_context, proxy_url=proxy, policy=policy, resolver=resolver)


@pytest.mark.parametrize("url,frag", [("file:///etc/passwd", "scheme"), ("ftp://example.com/", "scheme"),
                                      ("http://user:pw@example.com/", "credentials"), ("http://example.com:22/", "port"),
                                      ("http://169.254.169.254/latest/meta-data/", "metadata"),
                                      ("http://[::ffff:127.0.0.1]/", "loopback"), ("http://localhost/", "internal"),
                                      ("https://maps.googleapis.com/maps/api/place", "paid")])
def test_unsafe_urls_rejected_before_connecting(tmp_path, url, frag):
    calls = []
    f = fetcher(tmp_path, lambda h, p: calls.append(h) or ["8.8.8.8"])
    r = f.fetch(url)
    assert r.blocked_reason and frag in r.blocked_reason, r.blocked_reason
    assert f.backend.connections == []


@pytest.fixture
def site():
    srv = HTTPTestServer()
    yield srv
    srv.close()


def test_public_site_fetch_pins_resolved_address(tmp_path, site):
    site.routes = {"/": ok(b"<html>hi</html>", "text/html")}
    calls = []

    def resolver(h, p):
        calls.append(h)
        return ["127.0.0.1"]
    f = fetcher(tmp_path, resolver, allow_lo=True, ports={site.port})
    r = f.fetch(f"http://shop.example:{site.port}/")
    assert r.status == 200 and r.body == b"<html>hi</html>" and r.blocked_reason is None
    assert f.backend.connections == [("shop.example", "127.0.0.1", site.port)]
    assert calls == ["shop.example"]  # resolved exactly once, inside the connection backend


def test_loopback_site_blocked_without_test_override(tmp_path, site):
    site.routes = {"/": ok()}
    f = fetcher(tmp_path, lambda h, p: ["127.0.0.1"], ports={site.port})
    r = f.fetch(f"http://shop.example:{site.port}/")
    assert r.blocked_reason and "loopback" in r.blocked_reason
    assert site.requests == []


def test_redirect_to_private_address_blocked(tmp_path, site):
    site.routes = {"/": redirect("http://evil.example/admin")}
    ips = {"shop.example": ["127.0.0.1"], "evil.example": ["10.0.0.5"]}
    f = fetcher(tmp_path, lambda h, p: ips[h], allow_lo=True, ports={site.port, 80})
    r = f.fetch(f"http://shop.example:{site.port}/")
    assert r.blocked_reason and "private" in r.blocked_reason
    assert r.redirects == ["http://evil.example/admin"]


def test_redirect_to_metadata_blocked(tmp_path, site):
    site.routes = {"/": redirect("http://169.254.169.254/latest/meta-data/")}
    f = fetcher(tmp_path, lambda h, p: ["127.0.0.1"], allow_lo=True, ports={site.port, 80})
    r = f.fetch(f"http://shop.example:{site.port}/")
    assert "metadata" in r.blocked_reason


def test_mixed_dns_answers_blocked(tmp_path, site):
    site.routes = {"/": ok()}
    f = fetcher(tmp_path, lambda h, p: ["127.0.0.1", "10.0.0.1"], allow_lo=True, ports={site.port})
    assert "private" in f.fetch(f"http://shop.example:{site.port}/").blocked_reason
    assert site.requests == []


def test_dns_rebinding_cannot_reuse_earlier_validation(tmp_path, site):
    site.routes = {"/": ok()}
    answers = iter([["127.0.0.1"], ["10.9.9.9"]])
    f = fetcher(tmp_path, lambda h, p: next(answers), allow_lo=True, ports={site.port})
    first = f.fetch(f"http://shop.example:{site.port}/")
    second = f.fetch(f"http://shop.example:{site.port}/")  # server closes connections -> new resolution
    assert first.status == 200
    assert second.blocked_reason and "private" in second.blocked_reason
    assert len(site.requests) == 1


def test_https_to_http_downgrade_blocked(tmp_path):
    cert, key = make_cert(tmp_path, hostnames=("shop.example",))
    tls = HTTPTestServer({"/": redirect("http://shop.example/")}, tls_context=server_tls_context(cert, key))
    try:
        f = fetcher(tmp_path, lambda h, p: ["127.0.0.1"], allow_lo=True, ports={tls.port, 80}, ca=str(cert))
        r = f.fetch(f"https://shop.example:{tls.port}/")
        assert r.blocked_reason == "redirect downgrades https to http"
    finally:
        tls.close()


def test_redirect_loop_and_size_cap(tmp_path, site):
    site.routes = {"/a": redirect("/b"), "/b": redirect("/a"), "/big": ok(b"x" * 5000)}
    f = fetcher(tmp_path, lambda h, p: ["127.0.0.1"], allow_lo=True, ports={site.port}, max_body_bytes=1000)
    assert f.fetch(f"http://shop.example:{site.port}/a").blocked_reason == "redirect loop"
    big = f.fetch(f"http://shop.example:{site.port}/big")
    assert big.truncated and len(big.body) == 1000


def test_proxy_mode_never_connects_directly(tmp_path):
    backend = SafeBackend(EgressPolicy(), lambda h, p: ["8.8.8.8"], proxy_address=("127.0.0.1", 3128))
    with pytest.raises(BlockedDestination):
        backend.connect_tcp("example.com", 80)


def test_proxy_mode_validation(tmp_path):
    def unresolvable(h, p):
        raise OSError("no local dns")
    f = fetcher(tmp_path, unresolvable, proxy="http://127.0.0.1:3128")
    assert f.validate_url("https://example.com/") == "hostname-only (resolved by egress proxy)"
    g = fetcher(tmp_path, lambda h, p: ["10.0.0.8"], proxy="http://127.0.0.1:3128")
    with pytest.raises(BlockedDestination):
        g.validate_url("https://sneaky.example/")
