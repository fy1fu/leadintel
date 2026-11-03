"""SSRF-protected HTTP client for fetching *discovered business websites*.

This is deliberately separate from the fixed infrastructure allowlist: any public
HTTP(S) website may be fetched, but never:

* non-http(s) schemes, URLs with embedded credentials, or non-allowlisted ports;
* loopback, private (RFC 1918 / ULA), link-local, CGNAT, multicast, reserved or
  unspecified addresses, including IPv4-mapped / 6to4 / Teredo encodings;
* cloud metadata endpoints (169.254.169.254, fd00:ec2::254, 168.63.129.16, 100.100.100.200, ...);
* internal-looking hostnames (localhost, *.local, *.internal, metadata.google.internal, ...);
* redirects to any of the above, scheme downgrades (https -> http), or redirect loops;
* paid-provider hosts (cost guard).

DNS-rebinding defence (direct mode): the hostname is resolved exactly once inside the
connection backend, *every* returned address must be public, and the socket connects to
that validated address (TLS SNI and Host header still use the hostname). There is no
window between check and use.

Egress-proxy mode: the proxy resolves DNS. We validate the hostname, validate any local
resolution we can obtain, and only ever connect to the configured proxy - never directly
to a target. Residual risk (proxy-side resolution) is documented in docs/NETWORK.md.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from app.costs.guard import CostGuard, PaidCallBlocked

Resolver = Callable[[str, int], list[str]]

METADATA_IPS = {
    ipaddress.ip_address(a) for a in (
        "169.254.169.254", "169.254.170.2", "169.254.169.123", "fd00:ec2::254", "168.63.129.16",
        "100.100.100.200", "192.0.0.192")
}
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".lan", ".intranet",
                         ".corp", ".private", ".onion")
BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data", "kubernetes.default",
                 "ip6-localhost", "ip6-loopback"}


class BlockedDestination(Exception):
    def __init__(self, reason: str, url: str | None = None):
        super().__init__(reason if not url else f"{reason} ({url})")
        self.reason = reason
        self.url = url


def ip_block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour, (ip.teredo or (None, None))[1]):
            if embedded is not None:
                r = ip_block_reason(embedded)
                if r:
                    return f"embedded IPv4 {embedded}: {r}"
        if ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 well-known prefix
            r = ip_block_reason(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
            if r:
                return f"NAT64-embedded address: {r}"
    if ip in METADATA_IPS:
        return "cloud metadata endpoint"
    for attr, label in (("is_loopback", "loopback"), ("is_link_local", "link-local"), ("is_multicast", "multicast"),
                        ("is_unspecified", "unspecified"), ("is_reserved", "reserved"), ("is_private", "private")):
        if getattr(ip, attr):
            return f"{label} address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "carrier-grade NAT address"
    if not ip.is_global:
        return "non-global address"
    return None


def parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse IP literals, including legacy forms such as 2130706433, 0x7f.1 or 017700000001."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h.split("%")[0])
    except ValueError:
        pass
    # Legacy numeric forms accepted by inet_aton: decimal, octal, hex and shortened dotted forms.
    if h and h[0].isdigit() and set(h.lower()) <= set("0123456789abcdefx."):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def hostname_block_reason(host: str) -> str | None:
    h = host.lower().rstrip(".")
    if not h:
        return "empty hostname"
    if h in BLOCKED_HOSTS or h.endswith(BLOCKED_HOST_SUFFIXES):
        return "internal hostname"
    if "." not in h and parse_ip_literal(h) is None:
        return "single-label hostname"
    ip = parse_ip_literal(h)
    if ip is not None:
        return ip_block_reason(ip)
    return None


@dataclass
class EgressPolicy:
    allowed_ports: frozenset[int] = frozenset({80, 443})
    allow_https_to_http_downgrade: bool = False
    max_redirects: int = 5
    max_body_bytes: int = 5_000_000
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 20.0
    # Test hook only: networks treated as public. Never set in production configuration.
    _test_allow_networks: tuple[ipaddress._BaseNetwork, ...] = ()

    def ip_reason(self, ip) -> str | None:
        if any(ip in n for n in self._test_allow_networks):
            return None
        return ip_block_reason(ip)


def default_resolver(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(i[4][0] for i in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)))


class SafeBackend(httpcore.NetworkBackend):
    """httpcore network backend that validates and pins the destination address."""

    def __init__(self, policy: EgressPolicy, resolver: Resolver, proxy_address: tuple[str, int] | None):
        self.policy = policy
        self.resolver = resolver
        self.proxy_address = proxy_address
        self._inner = httpcore.SyncBackend()
        self.connections: list[tuple[str, str, int]] = []   # (hostname, ip, port) for auditing/tests

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                    socket_options: Iterable | None = None) -> httpcore.NetworkStream:
        if self.proxy_address is not None:
            if (host, port) != self.proxy_address:
                raise BlockedDestination(f"direct connection to {host}:{port} refused in proxy mode")
            self.connections.append((host, host, port))
            return self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        reason = hostname_block_reason(host) if parse_ip_literal(host) is None else None
        if reason:
            raise BlockedDestination(reason, host)
        if port not in self.policy.allowed_ports:
            raise BlockedDestination(f"port {port} not allowed", host)
        try:
            ips = [host] if parse_ip_literal(host) else self.resolver(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"DNS resolution failed for {host}: {exc}") from exc
        if not ips:
            raise httpcore.ConnectError(f"no addresses for {host}")
        for ip in ips:  # every answer must be public: mixed answers are a rebinding signal
            r = self.policy.ip_reason(ipaddress.ip_address(ip.split("%")[0]))
            if r:
                raise BlockedDestination(f"{host} resolves to {ip}: {r}", host)
        last: Exception | None = None
        for ip in ips:
            try:
                stream = self._inner.connect_tcp(ip, port, timeout, local_address, socket_options)
                self.connections.append((host, ip, port))
                return stream
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError(f"could not connect to {host}")

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise BlockedDestination("unix sockets are not allowed")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


@dataclass
class FetchResult:
    url: str
    final_url: str | None
    status: int | None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    error: str | None = None
    validation: str = "resolved-and-pinned"


class SafeFetcher:
    def __init__(self, guard: CostGuard, *, ssl_context: ssl.SSLContext, proxy_url: str | None = None,
                 policy: EgressPolicy | None = None, resolver: Resolver | None = None, user_agent: str = "LeadIntel"):
        self.guard = guard
        self.policy = policy or EgressPolicy()
        self.resolver = resolver or default_resolver
        self.proxy_url = proxy_url
        proxy_addr = None
        if proxy_url:
            pu = urlsplit(proxy_url)
            proxy_addr = (pu.hostname or "", pu.port or 80)
        self.backend = SafeBackend(self.policy, self.resolver, proxy_addr)
        if proxy_url:
            pool = httpcore.HTTPProxy(proxy_url=proxy_url, ssl_context=ssl_context, network_backend=self.backend,
                                      max_connections=10)
        else:
            pool = httpcore.ConnectionPool(ssl_context=ssl_context, network_backend=self.backend, max_connections=10)
        transport = httpx.HTTPTransport(verify=ssl_context)
        transport._pool = pool  # httpx exposes no public hook for a custom network backend
        self._client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                                    headers={"User-Agent": user_agent},
                                    timeout=httpx.Timeout(self.policy.read_timeout_s, connect=self.policy.connect_timeout_s))

    # ---- validation -------------------------------------------------------
    def validate_url(self, url: str) -> str:
        """Raise BlockedDestination for an unsafe URL; return the validation mode used."""
        u = urlsplit(url)
        if u.scheme not in ("http", "https"):
            raise BlockedDestination(f"scheme '{u.scheme}' not allowed", url)
        if u.username or u.password or "@" in u.netloc:
            raise BlockedDestination("credentials in URL not allowed", url)
        host = (u.hostname or "").lower()
        try:
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            raise BlockedDestination("invalid port", url)
        if port not in self.policy.allowed_ports:
            raise BlockedDestination(f"port {port} not allowed", url)
        try:
            self.guard.check(host)
        except PaidCallBlocked as exc:
            raise BlockedDestination(str(exc), url)
        ip = parse_ip_literal(host)
        if ip is not None:
            r = self.policy.ip_reason(ip)
            if r:
                raise BlockedDestination(r, url)
            return "ip-literal"
        r = hostname_block_reason(host)
        if r:
            raise BlockedDestination(r, url)
        if self.proxy_url:
            # Proxy resolves DNS; additionally reject names whose local resolution is non-public.
            try:
                ips = self.resolver(host, port)
            except OSError:
                return "hostname-only (resolved by egress proxy)"
            for a in ips:
                rr = self.policy.ip_reason(ipaddress.ip_address(a.split("%")[0]))
                if rr:
                    raise BlockedDestination(f"{host} resolves to {a}: {rr}", url)
            return "hostname + local resolution (proxy connects)"
        return "resolved-and-pinned"

    # ---- fetch --------------------------------------------------------------
    def fetch(self, url: str, method: str = "GET") -> FetchResult:
        res = FetchResult(url=url, final_url=None, status=None)
        current = url
        seen = {url}
        for hop in range(self.policy.max_redirects + 1):
            try:
                res.validation = self.validate_url(current)
                with self._client.stream(method, current) as resp:
                    res.status = resp.status_code
                    res.headers = {k.lower(): v for k, v in resp.headers.items()}
                    if resp.is_redirect and "location" in resp.headers:
                        nxt = urljoin(current, resp.headers["location"])
                        if urlsplit(current).scheme == "https" and urlsplit(nxt).scheme == "http" \
                                and not self.policy.allow_https_to_http_downgrade:
                            raise BlockedDestination("redirect downgrades https to http", nxt)
                        if nxt in seen:
                            raise BlockedDestination("redirect loop", nxt)
                        seen.add(nxt)
                        res.redirects.append(nxt)
                        current = nxt
                        continue
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > self.policy.max_body_bytes:
                            body = body[: self.policy.max_body_bytes]
                            res.truncated = True
                            break
                    res.body = body
                    res.final_url = current
                    return res
            except BlockedDestination as exc:
                res.blocked_reason = exc.reason
                res.final_url = current
                return res
            except httpx.HTTPError as exc:
                cause = exc.__cause__ or exc.__context__
                if isinstance(cause, BlockedDestination):
                    res.blocked_reason = cause.reason
                else:
                    res.error = f"{type(exc).__name__}: {exc}"
                res.final_url = current
                return res
        res.blocked_reason = f"more than {self.policy.max_redirects} redirects"
        return res

    def close(self) -> None:
        self._client.close()
