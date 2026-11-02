"""Each failure mode must be attributed to the right stage - using real local sockets."""
import socket

import pytest

from app.costs.guard import CostGuard
from app.net.probe import Connector, Prober
from helpers import (FakeConnectProxy, HTTPTestServer, RawServer, free_port, make_cert, make_settings,
                     never_respond, ok, redirect, registry, reset_immediately, send_banner,
                     server_tls_context, status, svc)


def run(reg, settings, key, connector=None):
    p = Prober(reg, settings, CostGuard(reg, settings), connector=connector, resolvers={})
    s = reg.service(key)
    return [p.run(s, spec) for spec in s.probes]


@pytest.fixture
def http_server():
    srv = HTTPTestServer({"/ok": ok(b"hello PAR1"), "/par": ok(b"PAR1xxxx"), "/503": status(503, b"down"),
                          "/redir": redirect("http://elsewhere.example/"), "/tiny": ok(b"x")})
    yield srv
    srv.close()


def test_http_success_records_all_stages(tmp_path, http_server):
    s = svc("x", http_server.port, [{"key": "p", "kind": "download", "url": f"http://127.0.0.1:{http_server.port}/par",
                                     "range_bytes": 4, "expect_magic": "PAR1"}])
    [r] = run(registry([s]), make_settings(tmp_path), "x")
    assert r.outcome == "success", r.error
    assert [st.stage for st in r.stages] == ["dns", "tcp", "http", "download"]
    assert r.network_reached is True and r.bytes_downloaded == 4 and r.http_status == 206


def test_dns_failure(tmp_path):
    s = svc("x", 80, [{"key": "p", "kind": "http", "url": "http://does-not-exist.invalid/"}], host="does-not-exist.invalid")
    [r] = run(registry([s]), make_settings(tmp_path), "x")
    assert (r.outcome, r.failure_stage, r.failure_kind) == ("failure", "dns", "dns_resolution_failed")
    assert r.network_reached is None


def test_connection_refused(tmp_path):
    port = free_port()
    s = svc("x", port, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{port}/"}])
    [r] = run(registry([s]), make_settings(tmp_path), "x")
    assert (r.failure_stage, r.failure_kind) == ("tcp", "connection_refused")


def test_untrusted_certificate_is_tls_failure(tmp_path):
    cert, key = make_cert(tmp_path)
    srv = HTTPTestServer({"/": ok()}, tls_context=server_tls_context(cert, key))
    try:
        s = svc("x", srv.port, [{"key": "p", "kind": "http", "url": f"https://localhost:{srv.port}/"}],
                host="localhost", scheme="https")
        [r] = run(registry([s]), make_settings(tmp_path), "x")
        assert (r.failure_stage, r.failure_kind) == ("tls", "tls_certificate_invalid")
        # trusting the CA explicitly makes it pass - verification is never disabled
        [r2] = run(registry([s]), make_settings(tmp_path, ca_bundle=str(cert)), "x")
        assert r2.outcome == "success", r2.error
        assert any(st.stage == "tls" and st.status == "ok" for st in r2.stages)
    finally:
        srv.close()


def test_reset_during_tls_handshake(tmp_path):
    srv = RawServer(reset_immediately)
    try:
        s = svc("x", srv.port, [{"key": "p", "kind": "http", "url": f"https://localhost:{srv.port}/"}],
                host="localhost", scheme="https")
        [r] = run(registry([s]), make_settings(tmp_path), "x")
        assert r.failure_stage == "tls"
        assert r.failure_kind in ("connection_reset", "connection_closed_during_tls")
        assert r.network_reached is True
    finally:
        srv.close()


def test_read_timeout(tmp_path):
    srv = RawServer(never_respond)
    try:
        s = svc("x", srv.port, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{srv.port}/", "timeout_s": 1}])
        [r] = run(registry([s]), make_settings(tmp_path), "x")
        assert (r.failure_stage, r.failure_kind) == ("http", "http_timeout")
    finally:
        srv.close()


def test_http_status_content_and_size_failures(tmp_path, http_server):
    p = http_server.port
    s = svc("x", p, [
        {"key": "s503", "kind": "http", "url": f"http://127.0.0.1:{p}/503"},
        {"key": "magic", "kind": "download", "url": f"http://127.0.0.1:{p}/ok", "expect_magic": "PAR1"},
        {"key": "small", "kind": "download", "url": f"http://127.0.0.1:{p}/tiny", "min_bytes": 100},
        {"key": "redir", "kind": "http", "url": f"http://127.0.0.1:{p}/redir"},
    ])
    r = {x.probe_key: x for x in run(registry([s]), make_settings(tmp_path), "x")}
    assert (r["s503"].failure_stage, r["s503"].failure_kind, r["s503"].http_status) == ("http", "http_status", 503)
    assert (r["magic"].failure_stage, r["magic"].failure_kind) == ("download", "content_mismatch")
    assert r["small"].failure_kind == "short_download"
    assert r["redir"].failure_kind == "redirect_to_unregistered_host"


def test_proxy_denied_vs_tunnel(tmp_path, http_server):
    deny = FakeConnectProxy(deny_status=403)
    tunnel = FakeConnectProxy(target_port=http_server.port)
    try:
        s = svc("x", 80, [{"key": "p", "kind": "http", "url": "http://service.invalid/ok"}], host="service.invalid")
        reg = registry([s])
        [r] = run(reg, make_settings(tmp_path, proxy_url=f"http://127.0.0.1:{deny.port}"), "x")
        assert (r.failure_stage, r.failure_kind, r.network_reached, r.via_proxy) == ("proxy", "proxy_denied", False, True)
        [r2] = run(reg, make_settings(tmp_path, proxy_url=f"http://127.0.0.1:{tunnel.port}"), "x")
        assert r2.outcome == "success", r2.error
        dns = next(st for st in r2.stages if st.stage == "dns")
        assert dns.status == "info"  # unresolvable locally, resolved by proxy - not a failure
        assert tunnel.targets == ["service.invalid:80"]
    finally:
        deny.close()
        tunnel.close()


def test_ssh_banner_probe(tmp_path):
    good, bad, silent = RawServer(send_banner(b"SSH-2.0-OpenSSH_9.6\r\n")), RawServer(send_banner(b"HTTP/1.1 400\r\n")), RawServer(never_respond)
    try:
        reg = registry([svc("g", good.port, [{"key": "b", "kind": "ssh_banner"}]),
                        svc("h", bad.port, [{"key": "b", "kind": "ssh_banner"}]),
                        svc("s", silent.port, [{"key": "b", "kind": "ssh_banner", "timeout_s": 1}])])
        st = make_settings(tmp_path)
        assert run(reg, st, "g")[0].outcome == "success"
        assert run(reg, st, "h")[0].failure_kind == "not_ssh"
        assert run(reg, st, "s")[0].failure_kind == "ssh_banner_timeout"
    finally:
        for x in (good, bad, silent):
            x.close()


def test_sftp_probe_skipped_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("SUNBIZ_SFTP_USER", raising=False)
    s = svc("x", 22, [{"key": "l", "kind": "sftp_list", "needs_env": ["SUNBIZ_SFTP_USER", "SUNBIZ_SFTP_PASSWORD"]}])
    [r] = run(registry([s]), make_settings(tmp_path), "x")
    assert r.outcome == "skipped" and r.failure_kind == "missing_configuration"


def test_redirect_to_paid_host_is_blocked_without_network(tmp_path):
    srv = HTTPTestServer({"/": redirect("https://maps.googleapis.com/maps/api/place/x")})
    try:
        s = svc("x", srv.port, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{srv.port}/"}])
        [r] = run(registry([s]), make_settings(tmp_path), "x")
        assert (r.outcome, r.failure_kind) == ("blocked", "paid_provider_blocked")
    finally:
        srv.close()


def test_bot_challenge_is_classified_not_bypassed(tmp_path):
    srv = HTTPTestServer({"/": status(403, b"<html>challenge-platform</html>", {"cf-mitigated": "challenge"})})
    try:
        s = svc("x", srv.port, [{"key": "p", "kind": "http", "url": f"http://127.0.0.1:{srv.port}/"}])
        [r] = run(registry([s]), make_settings(tmp_path), "x")
        assert (r.failure_stage, r.failure_kind, r.http_status) == ("http", "bot_challenge", 403)
        assert len(srv.requests) == 1  # one request, no retries or evasion
    finally:
        srv.close()


def test_network_allowed_refers_to_the_service_host_not_redirect_targets(tmp_path):
    from app.net.registry import Service, ProbeSpec
    srv = HTTPTestServer({"/": redirect("http://blocked.example/data")})
    proxy = FakeConnectProxy(target_port=srv.port)
    try:
        own = svc("own", 80, [{"key": "p", "kind": "http", "url": "http://own.example/"}], host="own.example")
        target = svc("target", 80, [{"key": "p", "kind": "http", "url": "http://blocked.example/"}], host="blocked.example")
        reg = registry([own, target])

        class DenyTarget(Connector):
            def open(self, host, port, result, timeout):
                if host == "blocked.example":
                    from app.net.probe import ProbeFailure
                    raise ProbeFailure("proxy", "proxy_denied", "denied")
                return super().open(host, port, result, timeout)
        st = make_settings(tmp_path, proxy_url=f"http://127.0.0.1:{proxy.port}")
        [r] = run(reg, st, "own", connector=DenyTarget(st))
        assert r.failure_kind == "proxy_denied" and r.network_reached is True
    finally:
        srv.close()
        proxy.close()


def _webdav_route(h):
    import base64
    if h.headers.get("Authorization") != "Basic " + base64.b64encode(b"Public:s3cr3t-value").decode():
        return 401, {"WWW-Authenticate": 'Basic realm="VShell"'}, b"<VanDyke-VShell><Title>Authorization Required</Title></VanDyke-VShell>"
    if h.command != "PROPFIND" or h.headers.get("Depth") != "1":
        return 405, {}, b""
    return 207, {"Content-Type": "text/xml"}, b"<D:multistatus><D:href>/Public/doc/fic/</D:href></D:multistatus>"


def _webdav_svc(port):
    return svc("dav", port, [
        {"key": "challenge", "kind": "http", "url": f"http://127.0.0.1:{port}/", "expect_status": [401], "expect_body_contains": "VShell"},
        {"key": "list", "kind": "http", "method": "PROPFIND", "headers": {"Depth": "1"}, "url": f"http://127.0.0.1:{port}/Public/doc/",
         "expect_status": [207], "expect_body_contains": "/Public/doc/fic/", "needs_env": ["T_USER", "T_PASS"],
         "basic_auth_env": ["T_USER", "T_PASS"]}])


def test_webdav_probe_authenticates_without_leaking_credentials(tmp_path, monkeypatch):
    import json
    srv = HTTPTestServer({"*": _webdav_route})
    try:
        monkeypatch.setenv("T_USER", "Public")
        monkeypatch.setenv("T_PASS", "s3cr3t-value")
        results = {r.probe_key: r for r in run(registry([_webdav_svc(srv.port)]), make_settings(tmp_path), "dav")}
        assert results["challenge"].outcome == "success"
        assert results["list"].outcome == "success", results["list"].error
        dumped = json.dumps([r.to_dict() for r in results.values()])
        assert "s3cr3t-value" not in dumped and "Basic " not in dumped and "UHVibGlj" not in dumped
        assert any(st.detail.get("authenticated") for st in results["list"].stages)
    finally:
        srv.close()


def test_webdav_probe_reports_rejected_credentials(tmp_path, monkeypatch):
    srv = HTTPTestServer({"*": _webdav_route})
    try:
        monkeypatch.setenv("T_USER", "Public")
        monkeypatch.setenv("T_PASS", "wrong-password")
        r = {x.probe_key: x for x in run(registry([_webdav_svc(srv.port)]), make_settings(tmp_path), "dav")}["list"]
        assert (r.failure_stage, r.failure_kind) == ("http", "authentication_failed")
        assert "wrong-password" not in (r.error or "")
    finally:
        srv.close()


def test_credentials_never_follow_a_redirect_to_another_host(tmp_path, monkeypatch):
    target = HTTPTestServer({"*": ok(b"/Public/doc/fic/")})
    origin = HTTPTestServer({"*": redirect(f"http://localhost:{target.port}/x")})
    try:
        monkeypatch.setenv("T_USER", "u")
        monkeypatch.setenv("T_PASS", "p")
        s1 = svc("o", origin.port, [{"key": "l", "kind": "http", "url": f"http://127.0.0.1:{origin.port}/",
                                     "expect_status": [200], "basic_auth_env": ["T_USER", "T_PASS"]}])
        s2 = svc("t", target.port, [{"key": "x", "kind": "http", "url": f"http://localhost:{target.port}/"}], host="localhost")
        [r] = run(registry([s1, s2]), make_settings(tmp_path), "o")
        assert r.outcome == "success", r.error
        assert "Authorization" in origin.requests[0][1]
        assert all("Authorization" not in hdrs for _, hdrs in target.requests)
    finally:
        target.close()
        origin.close()
