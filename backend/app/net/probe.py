"""Staged network probes.

Each probe walks the connection stage by stage so a failure is attributed to the
exact layer that failed:

    policy  -> paid-provider guard (no network activity at all)
    dns     -> local resolution (informational when an egress proxy resolves names)
    proxy   -> HTTP CONNECT through the configured egress proxy
    tcp     -> direct TCP connect
    tls     -> TLS handshake + certificate verification
    http    -> request/response, status, redirects
    download-> body bytes, size, content/magic checks
    ssh     -> SSH banner / SFTP session

A probe is only reported as `success` after a real request completed and every
expectation passed. Nothing here weakens TLS verification or bypasses the proxy.
"""
from __future__ import annotations

import concurrent.futures
import http.client
import os
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin, urlsplit

from app.config import Settings
from app.costs.guard import CostGuard, PaidCallBlocked
from app.net.registry import ProbeSpec, Registry, Service

_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="dns")


# --------------------------------------------------------------------------- results
@dataclass
class Stage:
    stage: str
    status: str            # ok | fail | info | skip
    ms: float
    detail: dict = field(default_factory=dict)


@dataclass
class ProbeResult:
    service_key: str
    probe_key: str
    kind: str
    host: str
    port: int
    url: str | None
    started_at: datetime
    duration_ms: float = 0.0
    outcome: str = "failure"            # success | failure | skipped | blocked
    failure_stage: str | None = None
    failure_kind: str | None = None
    error: str | None = None
    http_status: int | None = None
    bytes_downloaded: int = 0
    via_proxy: bool = False
    network_reached: bool | None = None  # tunnel/TCP established to the host (allowed at network level)
    stages: list[Stage] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat()
        return d


class ProbeFailure(Exception):
    def __init__(self, stage: str, kind: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.stage, self.kind, self.detail = stage, kind, detail or {}


# --------------------------------------------------------------------------- helpers
def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _classify_os_error(stage: str, exc: BaseException) -> ProbeFailure:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ProbeFailure(stage, f"{stage}_timeout", f"{stage} timed out")
    if isinstance(exc, ConnectionRefusedError):
        return ProbeFailure(stage, "connection_refused", str(exc) or "connection refused")
    if isinstance(exc, ConnectionResetError):
        return ProbeFailure(stage, "connection_reset", str(exc) or "connection reset by peer")
    if isinstance(exc, ConnectionAbortedError):
        return ProbeFailure(stage, "connection_aborted", str(exc))
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ProbeFailure(stage, "tls_certificate_invalid", exc.verify_message or str(exc))
    if isinstance(exc, ssl.SSLEOFError):
        return ProbeFailure(stage, "connection_closed_during_tls", str(exc))
    if isinstance(exc, ssl.SSLError):
        return ProbeFailure(stage, "tls_handshake_failed", str(exc))
    if isinstance(exc, OSError) and exc.errno in (101, 113):
        return ProbeFailure(stage, "network_unreachable", str(exc))
    if isinstance(exc, (http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return ProbeFailure(stage, "connection_closed", str(exc) or type(exc).__name__)
    if isinstance(exc, http.client.HTTPException):
        return ProbeFailure(stage, "protocol_error", f"{type(exc).__name__}: {exc}")
    return ProbeFailure(stage, "error", f"{type(exc).__name__}: {exc}")


def resolve_local(host: str, port: int, timeout: float) -> list[str]:
    fut = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    infos = fut.result(timeout=timeout)
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


# --------------------------------------------------------------------------- connection
class Connector:
    """Opens a raw connection to host:port, through the egress proxy when configured."""

    def __init__(self, settings: Settings, socket_factory: Callable = socket.create_connection):
        self.settings = settings
        self.socket_factory = socket_factory

    def open(self, host: str, port: int, result: ProbeResult, timeout: float) -> socket.socket:
        use_proxy = self.settings.proxy_applies(host)
        result.via_proxy = use_proxy

        # DNS -------------------------------------------------------------
        t0 = time.perf_counter()
        ips: list[str] = []
        try:
            ips = resolve_local(host, port, timeout)
            result.stages.append(Stage("dns", "ok", _ms(t0), {"addresses": ips[:6]}))
        except (socket.gaierror, concurrent.futures.TimeoutError, OSError) as exc:
            kind = "dns_timeout" if isinstance(exc, concurrent.futures.TimeoutError) else "dns_resolution_failed"
            if use_proxy:
                # The proxy resolves names remotely; a local miss is not a failure.
                result.stages.append(Stage("dns", "info", _ms(t0), {
                    "local_resolution": kind, "note": "egress proxy resolves the hostname"}))
            else:
                result.stages.append(Stage("dns", "fail", _ms(t0), {"error": str(exc)}))
                raise ProbeFailure("dns", kind, f"cannot resolve {host}: {exc}")

        if use_proxy:
            return self._connect_via_proxy(host, port, result, timeout)

        # Direct TCP ------------------------------------------------------
        last: BaseException | None = None
        for ip in ips:
            t0 = time.perf_counter()
            try:
                sock = self.socket_factory((ip, port), timeout=timeout)
                result.stages.append(Stage("tcp", "ok", _ms(t0), {"address": ip}))
                if host == result.host:
                    result.network_reached = True
                return sock
            except OSError as exc:
                last = exc
                result.stages.append(Stage("tcp", "fail", _ms(t0), {"address": ip, "error": str(exc)}))
        raise _classify_os_error("tcp", last or OSError("no addresses"))

    def _connect_via_proxy(self, host: str, port: int, result: ProbeResult, timeout: float) -> socket.socket:
        phost, pport = self.settings.proxy_address  # type: ignore[misc]
        t0 = time.perf_counter()
        try:
            sock = self.socket_factory((phost, pport), timeout=timeout)
        except OSError as exc:
            f = _classify_os_error("proxy", exc)
            result.stages.append(Stage("proxy", "fail", _ms(t0), {"error": str(f), "proxy": f"{phost}:{pport}"}))
            raise ProbeFailure("proxy", "proxy_unreachable", f"egress proxy {phost}:{pport} unreachable: {exc}")
        try:
            sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 16384:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except OSError as exc:
            sock.close()
            f = _classify_os_error("proxy", exc)
            result.stages.append(Stage("proxy", "fail", _ms(t0), {"error": str(f)}))
            raise ProbeFailure("proxy", f.kind if f.kind.endswith("timeout") else "proxy_connect_error", str(f))
        status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        m = re.match(r"HTTP/\d\.\d\s+(\d{3})", status_line)
        code = int(m.group(1)) if m else None
        if code != 200:
            sock.close()
            if host == result.host:  # only the service's own host defines 'network allowed'
                result.network_reached = False
            result.stages.append(Stage("proxy", "fail", _ms(t0), {"connect_status": code, "status_line": status_line}))
            kind = "proxy_denied" if code in (403, 407) else ("proxy_upstream_error" if code and code >= 500 else "proxy_bad_response")
            raise ProbeFailure("proxy", kind, f"proxy refused CONNECT {host}:{port}: {status_line or 'no response'}",
                               {"connect_status": code})
        result.stages.append(Stage("proxy", "ok", _ms(t0), {"connect_status": 200}))
        if host == result.host:
            result.network_reached = True
        return sock


# --------------------------------------------------------------------------- prober
class Prober:
    def __init__(self, registry: Registry, settings: Settings, guard: CostGuard,
                 connector: Connector | None = None,
                 resolvers: dict[str, Callable[["Prober", Service, ProbeSpec], str]] | None = None):
        self.registry = registry
        self.settings = settings
        self.guard = guard
        self.connector = connector or Connector(settings)
        self.resolvers = resolvers if resolvers is not None else default_resolvers()

    # ---- public ----------------------------------------------------------
    def run(self, service: Service, spec: ProbeSpec) -> ProbeResult:
        res = ProbeResult(service.key, spec.key, spec.kind, service.host, service.port, spec.url,
                          started_at=datetime.now(timezone.utc))
        t0 = time.perf_counter()
        try:
            missing = [v for v in spec.needs_env if not os.environ.get(v)]
            if missing:
                res.outcome = "skipped"
                res.failure_kind = "missing_configuration"
                res.error = f"not run: environment variables not set: {', '.join(missing)}"
                return res
            if spec.kind in ("http", "download"):
                url = spec.url
                if spec.resolver:
                    url = self._resolve(service, spec, res)
                res.url = url
                self._http_probe(service, spec, url, res)
            elif spec.kind == "ssh_banner":
                self._ssh_banner(service, spec, res)
            elif spec.kind == "sftp_list":
                self._sftp_list(service, spec, res)
            res.outcome = "success"
        except PaidCallBlocked as exc:
            res.outcome, res.failure_stage, res.failure_kind, res.error = "blocked", "policy", "paid_provider_blocked", str(exc)
        except ProbeFailure as exc:
            res.outcome, res.failure_stage, res.failure_kind, res.error = "failure", exc.stage, exc.kind, str(exc)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
            res.outcome, res.failure_stage, res.failure_kind, res.error = "failure", "internal", "probe_error", f"{type(exc).__name__}: {exc}"
        finally:
            res.duration_ms = _ms(t0)
        return res

    def fetch(self, url: str, res: ProbeResult, *, max_bytes: int = 1_048_576, range_bytes: int = 0,
              timeout: float | None = None, follow_redirects: int = 3, expect_status: list[int] | None = None,
              method: str = "GET", extra_headers: dict[str, str] | None = None,
              secret_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        """Staged request used by probes and resolvers. Raises ProbeFailure with stage attribution.

        `secret_headers` (e.g. Authorization) are sent only to the original host, never recorded in
        stages/results, and dropped if a redirect leads to another host.
        """
        expect_status = expect_status or [200, 206]
        origin = urlsplit(url).hostname
        for _hop in range(follow_redirects + 1):
            secrets = secret_headers if urlsplit(url).hostname == origin else None
            status, headers, body = self._request_once(url, res, max_bytes=max_bytes, range_bytes=range_bytes,
                                                       timeout=timeout, method=method, extra_headers=extra_headers,
                                                       secret_headers=secrets)
            if status in expect_status:
                return status, headers, body
            if 300 <= status < 400 and "location" in headers:
                nxt = urljoin(url, headers["location"])
                nh = urlsplit(nxt).hostname or ""
                self.guard.check(nh)
                if not self.registry.is_registered_host(nh):
                    raise ProbeFailure("http", "redirect_to_unregistered_host",
                                       f"{url} redirected to unregistered host {nh}", {"location": nxt})
                res.stages.append(Stage("http", "info", 0, {"redirect": nxt}))
                url = nxt
                continue
            if status in (403, 429, 503) and (headers.get("cf-mitigated") == "challenge"
                                              or b"challenge-platform" in body or b"cf-chl" in body):
                raise ProbeFailure("http", "bot_challenge",
                                   f"HTTP {status} bot challenge from {url} (automated access refused; never bypassed)",
                                   {"status": status})
            raise ProbeFailure("http", "http_status", f"unexpected HTTP {status} from {url}",
                               {"status": status, "body_snippet": body[:300].decode("utf-8", "replace")})
        raise ProbeFailure("http", "too_many_redirects", f"more than {follow_redirects} redirects from {url}")

    # ---- internals ---------------------------------------------------------
    def _resolve(self, service: Service, spec: ProbeSpec, res: ProbeResult) -> str:
        fn = self.resolvers.get(spec.resolver or "")
        if fn is None:
            raise ProbeFailure("resolve", "unknown_resolver", f"no resolver named {spec.resolver}")
        t0 = time.perf_counter()
        url = fn(self, service, spec)
        res.stages.append(Stage("resolve", "ok", _ms(t0), {"url": url}))
        return url

    def _request_once(self, url: str, res: ProbeResult, *, max_bytes: int, range_bytes: int,
                      timeout: float | None, method: str = "GET", extra_headers: dict[str, str] | None = None,
                      secret_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        u = urlsplit(url)
        host = u.hostname or ""
        self.guard.check(host)  # paid hosts never reach the network
        port = u.port or (443 if u.scheme == "https" else 80)
        to = timeout or self.settings.connect_timeout_s
        sock = self.connector.open(host, port, res, to)
        try:
            if u.scheme == "https":
                t0 = time.perf_counter()
                try:
                    sock = self.settings.ssl_context.wrap_socket(sock, server_hostname=host)
                except (OSError, ssl.SSLError) as exc:
                    f = _classify_os_error("tls", exc)
                    note = ("tunnel was established by the egress proxy; the remote side or upstream path closed it"
                            if res.via_proxy and f.kind in ("connection_reset", "connection_closed_during_tls") else None)
                    res.stages.append(Stage("tls", "fail", _ms(t0), {"error": str(f), **({"note": note} if note else {})}))
                    raise f
                cert = sock.getpeercert() or {}
                res.stages.append(Stage("tls", "ok", _ms(t0), {
                    "version": sock.version(), "not_after": cert.get("notAfter"),
                    "issuer": dict(x[0] for x in cert.get("issuer", ()) if x).get("organizationName")}))
            sock.settimeout(timeout or self.settings.read_timeout_s)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            hdrs = [f"{method} {path} HTTP/1.1", f"Host: {u.netloc}", f"User-Agent: {self.registry.user_agent}",
                    "Accept: */*", "Accept-Encoding: identity", "Connection: close"]
            if range_bytes > 0:
                hdrs.append(f"Range: bytes=0-{range_bytes - 1}")
            for k, v in {**(extra_headers or {}), **(secret_headers or {})}.items():
                if "\r" in v or "\n" in v or "\r" in k or "\n" in k:
                    raise ProbeFailure("config", "invalid_header", f"header {k} contains a line break")
                hdrs.append(f"{k}: {v}")
            t0 = time.perf_counter()
            try:
                sock.sendall(("\r\n".join(hdrs) + "\r\n\r\n").encode())
                resp = http.client.HTTPResponse(sock, method=method)
                resp.begin()
            except (OSError, http.client.HTTPException) as exc:
                f = _classify_os_error("http", exc)
                res.stages.append(Stage("http", "fail", _ms(t0), {"error": str(f)}))
                raise f
            status = resp.status
            headers = {k.lower(): v for k, v in resp.getheaders()}
            res.http_status = status
            res.stages.append(Stage("http", "ok", _ms(t0), {"method": method, "status": status,
                                                           "content_type": headers.get("content-type"),
                                                           "content_length": headers.get("content-length"),
                                                           "authenticated": bool(secret_headers)}))
            limit = range_bytes if range_bytes > 0 else max_bytes
            t0 = time.perf_counter()
            body = b""
            try:
                while len(body) < limit:
                    chunk = resp.read(min(65536, limit - len(body)))
                    if not chunk:
                        break
                    body += chunk
            except (OSError, http.client.HTTPException) as exc:
                f = _classify_os_error("download", exc)
                res.stages.append(Stage("download", "fail", _ms(t0), {"error": str(f), "bytes": len(body)}))
                raise ProbeFailure("download", f.kind if f.kind != "error" else "download_interrupted", str(f))
            res.bytes_downloaded += len(body)
            res.stages.append(Stage("download", "ok", _ms(t0), {"bytes": len(body),
                                                               "range_honoured": status == 206 if range_bytes else None}))
            return status, headers, body
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _http_probe(self, service: Service, spec: ProbeSpec, url: str | None, res: ProbeResult) -> None:
        if not url:
            raise ProbeFailure("config", "missing_url", "probe has no URL")
        secrets = None
        if spec.basic_auth_env:
            import base64
            user, pw = (os.environ.get(v, "") for v in spec.basic_auth_env)
            secrets = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
        extra = dict(spec.headers)
        try:
            status, headers, body = self.fetch(url, res, max_bytes=spec.max_bytes, range_bytes=spec.range_bytes,
                                               timeout=spec.timeout_s, expect_status=spec.expect_status,
                                               method=spec.method, extra_headers=extra, secret_headers=secrets)
        except ProbeFailure as exc:
            if secrets is not None and exc.detail.get("status") == 401:
                raise ProbeFailure("http", "authentication_failed",
                                   f"credentials from {', '.join(spec.basic_auth_env)} were rejected by {service.host}")
            raise
        self._check_body(spec, body)

    @staticmethod
    def _check_body(spec: ProbeSpec, body: bytes) -> None:
        def fail(kind: str, msg: str) -> None:
            raise ProbeFailure("download", kind, msg, {"bytes": len(body), "head_hex": body[:16].hex()})
        if len(body) < spec.min_bytes:
            fail("short_download", f"received {len(body)} bytes, expected at least {spec.min_bytes}")
        if spec.expect_magic and not body.startswith(spec.expect_magic.encode()):
            fail("content_mismatch", f"file does not start with {spec.expect_magic!r}")
        if spec.expect_magic_hex and not body.startswith(bytes.fromhex(spec.expect_magic_hex)):
            fail("content_mismatch", f"file does not start with 0x{spec.expect_magic_hex}")
        if spec.expect_body_contains_bytes and spec.expect_body_contains_bytes.encode() not in body:
            fail("content_mismatch", f"body lacks {spec.expect_body_contains_bytes!r}")
        text = None
        if spec.expect_body_contains or spec.expect_body_regex:
            text = body.decode("utf-8", "replace")
        if spec.expect_body_contains and spec.expect_body_contains not in (text or ""):
            fail("content_mismatch", f"response does not contain {spec.expect_body_contains!r}")
        if spec.expect_body_regex and not re.search(spec.expect_body_regex, text or ""):
            fail("content_mismatch", f"response does not match /{spec.expect_body_regex}/")

    def _ssh_banner(self, service: Service, spec: ProbeSpec, res: ProbeResult) -> None:
        self.guard.check(service.host)
        to = spec.timeout_s or self.settings.connect_timeout_s
        sock = self.connector.open(service.host, service.port, res, to)
        t0 = time.perf_counter()
        try:
            sock.settimeout(to)
            data = b""
            while b"\n" not in data and len(data) < 512:
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk
        except OSError as exc:
            f = _classify_os_error("ssh", exc)
            res.stages.append(Stage("ssh", "fail", _ms(t0), {"error": str(f)}))
            raise ProbeFailure("ssh", "ssh_banner_timeout" if f.kind.endswith("timeout") else f.kind, str(f))
        finally:
            sock.close()
        line = data.split(b"\n", 1)[0].strip().decode("latin-1", "replace")
        if not line.startswith("SSH-"):
            res.stages.append(Stage("ssh", "fail", _ms(t0), {"received": line[:120]}))
            raise ProbeFailure("ssh", "not_ssh", f"no SSH banner (received {line[:60]!r})" if line else "connection closed without banner")
        res.stages.append(Stage("ssh", "ok", _ms(t0), {"banner": line[:120]}))

    def _sftp_list(self, service: Service, spec: ProbeSpec, res: ProbeResult) -> None:
        import logging

        import paramiko  # local import keeps startup light

        # paramiko logs full tracebacks from its transport thread; the probe records the error itself.
        logging.getLogger("paramiko").setLevel(logging.CRITICAL)

        self.guard.check(service.host)
        to = spec.timeout_s or self.settings.connect_timeout_s
        sock = self.connector.open(service.host, service.port, res, to)
        t0 = time.perf_counter()
        transport = paramiko.Transport(sock)
        try:
            transport.banner_timeout = to
            transport.connect(username=os.environ[spec.needs_env[0]], password=os.environ[spec.needs_env[1]])
            key = transport.get_remote_server_key()
            sftp = paramiko.SFTPClient.from_transport(transport)
            names = sftp.listdir(spec.path or "/")
            res.stages.append(Stage("ssh", "ok", _ms(t0), {
                "host_key": f"{key.get_name()} {key.get_fingerprint().hex()}", "entries": names[:20]}))
        except paramiko.AuthenticationException as exc:
            raise ProbeFailure("ssh", "sftp_auth_failed", str(exc))
        except (paramiko.SSHException, OSError) as exc:
            raise ProbeFailure("ssh", "sftp_error", f"{type(exc).__name__}: {exc}")
        finally:
            transport.close()


# --------------------------------------------------------------------------- resolvers
def default_resolvers() -> dict[str, Callable[[Prober, Service, ProbeSpec], str]]:
    from app.sources.overture.mirrors import resolve_sample_object_url

    def tilejson_first_tile(prober: Prober, service: Service, spec: ProbeSpec) -> str:
        import json
        scratch = ProbeResult(service.key, spec.key + ":tilejson", "http", service.host, service.port, spec.url,
                              datetime.now(timezone.utc))
        _, _, body = prober.fetch(spec.url or "", scratch, max_bytes=2_000_000)
        tiles = json.loads(body).get("tiles") or []
        if not tiles:
            raise ProbeFailure("resolve", "tilejson_without_tiles", "TileJSON lists no tile URLs")
        return tiles[0].replace("{z}", "0").replace("{x}", "0").replace("{y}", "0")

    return {"overture_sample_object": resolve_sample_object_url, "tilejson_first_tile": tilejson_first_tile}
