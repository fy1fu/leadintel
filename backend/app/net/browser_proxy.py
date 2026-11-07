"""Egress proxy for the website crawler's browser (M3 step 2; research/M3_PREP.md §1).

The crawler's browser (Edge/Chromium via Playwright) is configured to send ALL traffic through this proxy. For
every connection the proxy applies the same rules as ``app.net.safe_egress`` (the fetcher for business websites):

* only ``CONNECT host:443`` tunnels and absolute-form ``http://`` requests to port 80 (``EgressPolicy.allowed_ports``);
* internal-looking hostnames and non-public IP literals are refused before any DNS lookup;
* the hostname is resolved ONCE here; EVERY answer must be public (mixed answers are a rebinding signal); the
  proxy then connects to that checked address itself - the browser never resolves or connects on its own, so a
  DNS answer that changes later (rebinding) cannot reach a private address;
* paid-provider hosts are refused (cost guard), like everywhere else.

It listens on 127.0.0.1 only, on a random port, and requires per-run random credentials (``Proxy-Authorization``),
so other local programs cannot use it. Each connection has an idle timeout and a byte limit; the number of
simultaneous connections is capped. Every decision is recorded in ``audit`` for the crawl evidence.

The browser must also be started with ``--proxy-bypass-list=<-loopback>``: Chromium otherwise sends loopback
addresses around the proxy. See ``chromium_args()``.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import secrets
import select
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.costs.guard import CostGuard, PaidCallBlocked
from app.net.safe_egress import EgressPolicy, Resolver, default_resolver, hostname_block_reason, parse_ip_literal

MAX_HEADER_BYTES = 32 * 1024
DEFAULT_MAX_BYTES = 60 * 1024 * 1024        # per connection, both directions together
DEFAULT_IDLE_S = 20.0
DEFAULT_MAX_CONNECTIONS = 24
_HOP_HEADERS = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer", "upgrade"}


@dataclass
class AuditEntry:
    at: float
    method: str
    host: str
    port: int
    allowed: bool
    reason: str = ""
    ip: str = ""
    bytes: int = 0


class _Refused(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass
class BrowserEgressProxy:
    policy: EgressPolicy = field(default_factory=EgressPolicy)
    resolver: Resolver = default_resolver
    guard: CostGuard | None = None
    max_bytes: int = DEFAULT_MAX_BYTES
    idle_s: float = DEFAULT_IDLE_S
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    audit: list[AuditEntry] = field(default_factory=list)

    def __post_init__(self):
        self.username = "li-" + secrets.token_hex(4)
        self.password = secrets.token_urlsafe(24)
        self._expected = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "BrowserEgressProxy":
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="browser-egress-proxy", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def address(self) -> tuple[str, int]:
        assert self._server is not None, "proxy not started"
        return self._server.server_address[:2]

    def playwright_proxy(self) -> dict:
        host, port = self.address
        return {"server": f"http://{host}:{port}", "username": self.username, "password": self.password}

    @staticmethod
    def chromium_args() -> list[str]:
        """Launch arguments the crawler browser needs together with this proxy."""
        return ["--proxy-bypass-list=<-loopback>",                       # loopback goes through the proxy too
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",  # no WebRTC UDP around the proxy
                "--disable-quic"]                                         # no QUIC/UDP; HTTP/1.1 and HTTP/2 via proxy

    # ------------------------------------------------------------------ decisions
    def _record(self, **kw) -> AuditEntry:
        e = AuditEntry(at=time.time(), **kw)
        with self._lock:
            self.audit.append(e)
        return e

    def check_destination(self, host: str, port: int) -> str:
        """Returns the checked IP to connect to, or raises _Refused. The only place a target is resolved."""
        if port not in self.policy.allowed_ports:
            raise _Refused(403, f"port {port} not allowed")
        literal = parse_ip_literal(host)
        if literal is None:
            reason = hostname_block_reason(host)
            if reason:
                raise _Refused(403, reason)
        if self.guard is not None:
            try:
                self.guard.check(host)
            except PaidCallBlocked as exc:
                raise _Refused(403, f"paid provider host: {exc}") from exc
        try:
            ips = [str(literal)] if literal is not None else self.resolver(host, port)
        except OSError as exc:
            raise _Refused(502, f"DNS resolution failed: {exc}") from exc
        if not ips:
            raise _Refused(502, "no addresses")
        for ip in ips:
            r = self.policy.ip_reason(ipaddress.ip_address(ip.split("%")[0]))
            if r:
                raise _Refused(403, f"{host} resolves to {ip}: {r}")
        return ips[0]

    # ------------------------------------------------------------------ connection handling
    def _handle(self, client: socket.socket) -> None:
        if not self._slots.acquire(blocking=False):
            self._reply(client, 503, "too many connections")
            self._close_gracefully(client)
            return
        try:
            client.settimeout(self.idle_s)
            head, rest = self._read_head(client)
            if head is None:
                return
            lines = head.split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
                raise _Refused(400, "malformed request line")
            method, target, version = parts
            headers = [(k.strip(), v.strip()) for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)]
            auth = next((v for k, v in headers if k.lower() == "proxy-authorization"), "")
            if not hmac.compare_digest(auth.encode(), self._expected.encode()):
                self._reply(client, 407, "proxy authentication required",
                            extra="Proxy-Authenticate: Basic realm=\"leadintel-crawler\"\r\n")
                self._record(method=method, host="", port=0, allowed=False, reason="bad credentials")
                return
            if method == "CONNECT":
                self._connect(client, target, rest)
            elif target.lower().startswith("http://"):
                self._forward_http(client, method, target, version, headers, rest)
            else:
                raise _Refused(405, "only CONNECT tunnels and absolute http:// requests are proxied")
        except _Refused as r:
            self._reply(client, r.status, r.reason)
        except (OSError, ValueError):
            pass
        finally:
            self._slots.release()
            self._close_gracefully(client)

    @staticmethod
    def _close_gracefully(sock: socket.socket, drain_bytes: int = 256 * 1024, drain_s: float = 0.5) -> None:
        """Closes so the client reads our reply: closing with unread input would reset the connection instead."""
        try:
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(drain_s)
            got, deadline = 0, time.monotonic() + drain_s
            while got < drain_bytes and time.monotonic() < deadline:
                d = sock.recv(65536)
                if not d:
                    break
                got += len(d)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _read_head(self, sock: socket.socket) -> tuple[str | None, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None, b""
            buf += chunk
            if len(buf) > MAX_HEADER_BYTES:
                raise _Refused(431, "request header too large")
        head, _, rest = buf.partition(b"\r\n\r\n")
        try:
            return head.decode("iso-8859-1"), rest
        except UnicodeDecodeError as exc:
            raise _Refused(400, "undecodable header") from exc

    def _reply(self, sock: socket.socket, status: int, reason: str, extra: str = "") -> None:
        body = f"LeadIntel crawler proxy: {reason}\n".encode()
        msg = (f"HTTP/1.1 {status} {'OK' if status < 400 else 'Refused'}\r\nContent-Type: text/plain\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n").encode() + body
        try:
            sock.sendall(msg)
        except OSError:
            pass

    def _open(self, method: str, host: str, port: int) -> tuple[socket.socket, AuditEntry]:
        try:
            ip = self.check_destination(host, port)
        except _Refused as r:
            self._record(method=method, host=host, port=port, allowed=False, reason=r.reason)
            raise
        try:
            upstream = socket.create_connection((ip, port), timeout=self.policy.connect_timeout_s)
        except OSError as exc:
            self._record(method=method, host=host, port=port, allowed=False, reason=f"connect failed: {exc}", ip=ip)
            raise _Refused(502, "could not connect") from exc
        upstream.settimeout(self.idle_s)
        return upstream, self._record(method=method, host=host, port=port, allowed=True, ip=ip)

    def _connect(self, client: socket.socket, target: str, rest: bytes) -> None:
        host, sep, port_s = target.rpartition(":")
        if not sep or not port_s.isdigit():
            raise _Refused(400, "CONNECT target must be host:port")
        host = host.strip("[]")
        upstream, entry = self._open("CONNECT", host, int(port_s))
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            entry.bytes = self._pipe(client, upstream, len(rest))

    def _forward_http(self, client, method, target, version, headers, rest) -> None:
        u = urlsplit(target)
        if u.username or u.password:
            raise _Refused(403, "URLs with credentials are not allowed")
        host, port = u.hostname or "", u.port or 80
        upstream, entry = self._open(method, host, port)
        with upstream:
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            out = [f"{method} {path} {version}", f"Host: {u.netloc.rsplit('@', 1)[-1]}"]
            out += [f"{k}: {v}" for k, v in headers if k.lower() not in _HOP_HEADERS and k.lower() != "host"]
            out.append("Connection: close")
            upstream.sendall(("\r\n".join(out) + "\r\n\r\n").encode("iso-8859-1") + rest)
            entry.bytes = self._pipe(client, upstream, len(rest))

    def _pipe(self, a: socket.socket, b: socket.socket, already: int = 0) -> int:
        total = already
        socks = [a, b]
        while True:
            readable, _, _ = select.select(socks, [], [], self.idle_s)
            if not readable:
                return total                                   # idle timeout
            for s in readable:
                data = s.recv(65536)
                if not data:
                    return total
                total += len(data)
                if total > self.max_bytes:
                    return total                               # byte limit: the connection is cut
                (b if s is a else a).sendall(data)
