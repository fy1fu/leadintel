"""Crawler egress proxy (M3 step 2): SSRF and DNS-rebinding protection for the crawler's browser.

Offline: synthetic servers on 127.0.0.2 (treated as "public" only through the test-only EgressPolicy hook), a stub
resolver instead of DNS, raw sockets as the client. Nothing leaves this machine.
"""
import base64
import ipaddress
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import REPO_ROOT
from app.costs.guard import CostGuard
from app.net.browser_proxy import BrowserEgressProxy
from app.net.registry import load_registry
from app.net.safe_egress import EgressPolicy
from helpers import make_settings

TEST_NET = (ipaddress.ip_network("127.0.0.2/32"),)


class _Upstream(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _Upstream.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        size = int(self.headers.get("X-Size", "0"))
        body = b"x" * size if size else b"hello from the synthetic website"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def web():
    """A synthetic HTTP website on 127.0.0.2."""
    srv = HTTPServer(("127.0.0.2", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Upstream.seen.clear()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def echo():
    """A synthetic TCP service on 127.0.0.2 that echoes (stands in for a TLS website behind CONNECT)."""
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                d = self.request.recv(4096)
                if not d:
                    return
                self.request.sendall(d)
    srv = socketserver.ThreadingTCPServer(("127.0.0.2", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


class StubDNS:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, host, port):
        self.calls.append(host)
        a = self.answers.get(host)
        if isinstance(a, Exception):
            raise a
        return list(a or [])


def _proxy(ports, dns=None, **kw):
    pol = EgressPolicy(allowed_ports=frozenset(ports), _test_allow_networks=TEST_NET)
    return BrowserEgressProxy(policy=pol, resolver=dns or StubDNS({}), idle_s=3, **kw).start()


def _send(proxy, raw: bytes, auth=True, read=True, timeout=5) -> bytes:
    s = socket.create_connection(proxy.address, timeout=timeout)
    if auth:
        cred = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
        head, sep, rest = raw.partition(b"\r\n")
        raw = head + b"\r\nProxy-Authorization: Basic " + cred.encode() + sep + rest
    s.sendall(raw)
    if not read:
        return s
    out = b""
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except socket.timeout:
        pass
    s.close()
    return out


def _status(resp: bytes) -> int:
    return int(resp.split(b" ", 2)[1]) if resp.startswith(b"HTTP/1.") else 0


# ---------------------------------------------------------------- basics
def test_listens_on_loopback_only_with_random_credentials():
    a, b = _proxy({80}), _proxy({80})
    try:
        assert a.address[0] == "127.0.0.1" and a.address[1] != 0
        assert (a.username, a.password) != (b.username, b.password) and len(a.password) >= 24
        assert a.playwright_proxy()["server"] == f"http://127.0.0.1:{a.address[1]}"
        assert "--proxy-bypass-list=<-loopback>" in BrowserEgressProxy.chromium_args()
    finally:
        a.stop(), b.stop()


@pytest.mark.parametrize("auth", [None, "Basic " + base64.b64encode(b"li-x:wrong").decode(), "Bearer abc"])
def test_credentials_are_required(web, auth):
    p = _proxy({web}, StubDNS({"site.test": ["127.0.0.2"]}))
    try:
        raw = f"GET http://site.test:{web}/ HTTP/1.1\r\nHost: site.test\r\n".encode()
        if auth:
            raw += f"Proxy-Authorization: {auth}\r\n".encode()
        resp = _send(p, raw + b"\r\n", auth=False)
        assert _status(resp) == 407 and b"Proxy-Authenticate" in resp
        assert _Upstream.seen == [] and p.resolver.calls == []                  # nothing resolved or contacted
    finally:
        p.stop()


# ---------------------------------------------------------------- allowed traffic
def test_http_is_forwarded_to_the_checked_address_without_proxy_credentials(web):
    dns = StubDNS({"site.test": ["127.0.0.2"]})
    p = _proxy({web}, dns)
    try:
        resp = _send(p, f"GET http://site.test:{web}/page?a=1 HTTP/1.1\r\nHost: site.test\r\nUser-Agent: t\r\n\r\n".encode())
        assert _status(resp) == 200 and resp.endswith(b"hello from the synthetic website")
        seen = _Upstream.seen[0]
        assert seen["path"] == "/page?a=1" and seen["headers"]["host"] == f"site.test:{web}"
        assert "proxy-authorization" not in seen["headers"] and seen["headers"]["connection"] == "close"
        assert dns.calls == ["site.test"]                                      # resolved exactly once, by the proxy
        assert [(e.allowed, e.ip) for e in p.audit] == [(True, "127.0.0.2")]
    finally:
        p.stop()


def test_connect_tunnel_carries_bytes_to_the_checked_address(echo):
    p = _proxy({echo}, StubDNS({"secure.test": ["127.0.0.2"]}))
    try:
        s = _send(p, f"CONNECT secure.test:{echo} HTTP/1.1\r\nHost: secure.test:{echo}\r\n\r\n".encode(), read=False)
        assert s.recv(1024).startswith(b"HTTP/1.1 200")
        s.sendall(b"tls bytes would flow here")
        assert s.recv(1024) == b"tls bytes would flow here"
        s.close()
        time.sleep(0.2)
        assert p.audit[0].allowed and p.audit[0].ip == "127.0.0.2" and p.audit[0].bytes >= 26
    finally:
        p.stop()


# ---------------------------------------------------------------- refused destinations (SSRF)
@pytest.mark.parametrize("target", ["127.0.0.1", "10.0.0.1", "192.168.1.10", "169.254.169.254", "100.64.0.1", "[::1]",
                                    "[::ffff:127.0.0.1]", "2130706433", "0x7f.1", "[fd00:ec2::254]", "0.0.0.0"])
@pytest.mark.parametrize("method", ["CONNECT", "GET"])
def test_private_and_special_addresses_are_refused(web, target, method):
    p = _proxy({web})
    try:
        raw = (f"CONNECT {target}:{web} HTTP/1.1\r\n\r\n" if method == "CONNECT"
               else f"GET http://{target}:{web}/ HTTP/1.1\r\nHost: x\r\n\r\n").encode()
        resp = _send(p, raw)
        assert _status(resp) == 403, resp[:120]
        assert _Upstream.seen == [] and not p.audit[0].allowed
    finally:
        p.stop()


@pytest.mark.parametrize("host", ["localhost", "metadata.google.internal", "printer.local", "router.lan", "intranet"])
def test_internal_hostnames_are_refused_without_dns(web, host):
    dns = StubDNS({host: ["127.0.0.2"]})
    p = _proxy({web}, dns)
    try:
        assert _status(_send(p, f"CONNECT {host}:{web} HTTP/1.1\r\n\r\n".encode())) == 403
        assert dns.calls == []
    finally:
        p.stop()


@pytest.mark.parametrize("answers", [["127.0.0.1"], ["10.1.2.3"], ["127.0.0.2", "10.1.2.3"], ["::1"],
                                     ["169.254.169.254"]])
def test_dns_answers_pointing_inside_are_refused(web, answers):
    """DNS rebinding: a public-looking name whose answer (or any one of its answers) is private."""
    p = _proxy({web}, StubDNS({"rebind.test": answers}))
    try:
        assert _status(_send(p, f"GET http://rebind.test:{web}/ HTTP/1.1\r\nHost: rebind.test\r\n\r\n".encode())) == 403
        assert _Upstream.seen == []
    finally:
        p.stop()


def test_a_name_is_resolved_once_so_a_changing_answer_cannot_redirect_the_connection(web):
    class Flipping(StubDNS):                    # first answer public, any later answer private
        def __call__(self, host, port):
            self.calls.append(host)
            return ["127.0.0.2"] if len(self.calls) == 1 else ["127.0.0.1"]
    dns = Flipping({})
    p = _proxy({web}, dns)
    try:
        assert _status(_send(p, f"GET http://flip.test:{web}/ HTTP/1.1\r\nHost: flip.test\r\n\r\n".encode())) == 200
        assert dns.calls == ["flip.test"] and p.audit[0].ip == "127.0.0.2"
    finally:
        p.stop()


def test_only_allowed_ports(web):
    p = _proxy({443}, StubDNS({"site.test": ["127.0.0.2"]}))
    try:
        for port in (web, 22, 25, 6379, 8080):
            assert _status(_send(p, f"CONNECT site.test:{port} HTTP/1.1\r\n\r\n".encode())) == 403
        assert _Upstream.seen == []
    finally:
        p.stop()


def test_paid_provider_hosts_are_refused():
    reg = load_registry(REPO_ROOT / "config" / "network.yaml")
    settings = make_settings(REPO_ROOT / "config")
    dns = StubDNS({"api.anthropic.com": ["127.0.0.2"]})
    p = _proxy({443}, dns, guard=CostGuard(reg, settings))
    try:
        resp = _send(p, b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
        assert _status(resp) == 403 and b"paid provider" in resp and dns.calls == []
    finally:
        p.stop()


# ---------------------------------------------------------------- malformed and abusive requests
@pytest.mark.parametrize("raw,status", [
    (b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n", 405),
    (b"GARBAGE\r\n\r\n", 400),
    (b"GET ftp://site.test/ HTTP/1.1\r\n\r\n", 405),
    (b"CONNECT nohostport HTTP/1.1\r\n\r\n", 400),
    (b"GET http://user:pw@site.test/ HTTP/1.1\r\n\r\n", 403),
])
def test_malformed_requests_are_refused(raw, status):
    p = _proxy({80}, StubDNS({"site.test": ["127.0.0.2"]}))
    try:
        assert _status(_send(p, raw)) == status
    finally:
        p.stop()


def test_oversized_headers_are_refused():
    p = _proxy({80})
    try:
        resp = _send(p, b"GET http://site.test/ HTTP/1.1\r\nX: " + b"a" * 40000 + b"\r\n\r\n")
        assert _status(resp) == 431
    finally:
        p.stop()


def test_dns_failure_is_reported_not_retried_elsewhere(web):
    p = _proxy({web}, StubDNS({"gone.test": OSError("NXDOMAIN")}))
    try:
        assert _status(_send(p, f"GET http://gone.test:{web}/ HTTP/1.1\r\nHost: gone.test\r\n\r\n".encode())) == 502
    finally:
        p.stop()


def test_byte_limit_cuts_oversized_responses(web):
    p = _proxy({web}, StubDNS({"big.test": ["127.0.0.2"]}), max_bytes=64 * 1024)
    try:
        resp = _send(p, f"GET http://big.test:{web}/ HTTP/1.1\r\nHost: big.test\r\nX-Size: 2000000\r\n\r\n".encode())
        assert 0 < len(resp) <= 64 * 1024 + 65536
    finally:
        p.stop()


def test_simultaneous_connections_are_capped(echo):
    p = _proxy({echo}, StubDNS({"secure.test": ["127.0.0.2"]}), max_connections=1)
    try:
        first = _send(p, f"CONNECT secure.test:{echo} HTTP/1.1\r\n\r\n".encode(), read=False)
        assert first.recv(1024).startswith(b"HTTP/1.1 200")
        assert _status(_send(p, f"CONNECT secure.test:{echo} HTTP/1.1\r\n\r\n".encode())) == 503
        first.close()
    finally:
        p.stop()
