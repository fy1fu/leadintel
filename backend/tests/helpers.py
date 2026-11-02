"""Local test servers that reproduce specific network behaviours."""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket
import ssl
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.net.registry import Capability, ProbeSpec, Registry, Service


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- HTTP
Route = Callable[[BaseHTTPRequestHandler], tuple[int, dict, bytes]]


class HTTPTestServer:
    def __init__(self, routes: dict[str, Route] | None = None, tls_context: ssl.SSLContext | None = None):
        self.routes = routes or {}
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, dict(self.headers)))
                fn = outer.routes.get(self.path.split("?")[0]) or outer.routes.get("*")
                if fn is None:
                    status, headers, body = 404, {}, b"not found"
                else:
                    status, headers, body = fn(self)
                rng = self.headers.get("Range")
                if rng and status == 200 and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    start = int(a)
                    end = int(b) if b else len(body) - 1
                    total = len(body)
                    if start >= total:
                        status, body = 416, b""
                        headers = {**headers, "Content-Range": f"bytes */{total}"}
                    else:
                        body = body[start:end + 1]
                        status = 206
                        headers = {**headers, "Content-Range": f"bytes {start}-{start + len(body) - 1}/{total}"}
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        if tls_context:
            self.httpd.socket = tls_context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(body: bytes = b"ok", ctype: str = "text/plain", extra: dict | None = None) -> Route:
    return lambda h: (200, {"Content-Type": ctype, **(extra or {})}, body)


def status(code: int, body: bytes = b"", extra: dict | None = None) -> Route:
    return lambda h: (code, extra or {}, body)


def redirect(location: str) -> Route:
    return lambda h: (302, {"Location": location}, b"")


# --------------------------------------------------------------------------- raw TCP behaviours
class RawServer:
    """Accepts TCP connections and applies `behaviour(conn)`."""

    def __init__(self, behaviour: Callable[[socket.socket], None]):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = False
        self.behaviour = behaviour
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            self.behaviour(conn)
        except OSError:
            pass

    def close(self):
        self._stop = True
        self.sock.close()


def reset_immediately(conn: socket.socket) -> None:
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.recv(1)  # wait for the ClientHello so the reset happens mid-handshake
    conn.close()


def never_respond(conn: socket.socket) -> None:
    threading.Event().wait(30)


def send_banner(banner: bytes) -> Callable[[socket.socket], None]:
    def b(conn: socket.socket) -> None:
        conn.sendall(banner)
        threading.Event().wait(2)
        conn.close()
    return b


class FakeConnectProxy(RawServer):
    """Minimal HTTP CONNECT proxy: either refuses with `deny_status` or tunnels to `target_port` on 127.0.0.1."""

    def __init__(self, target_port: int | None = None, deny_status: int | None = None):
        self.targets: list[str] = []
        self.target_port = target_port
        self.deny_status = deny_status
        super().__init__(self._serve)

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        self.targets.append(buf.split(b" ")[1].decode())
        if self.deny_status:
            conn.sendall(f"HTTP/1.1 {self.deny_status} Forbidden\r\nContent-Length: 0\r\n\r\n".encode())
            conn.close()
            return
        up = socket.create_connection(("127.0.0.1", self.target_port))
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        def pipe(a, b):
            try:
                while (d := a.recv(65536)):
                    b.sendall(d)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=pipe, args=(up, conn), daemon=True)
        t.start()
        pipe(conn, up)
        t.join(5)


# --------------------------------------------------------------------------- TLS material
def make_cert(tmp: Path, hostnames=("localhost",), ips=("127.0.0.1",)) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])])
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(h) for h in hostnames] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_p, key_p = tmp / "cert.pem", tmp / "key.pem"
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    return cert_p, key_p


def server_tls_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


# --------------------------------------------------------------------------- registries / settings
def make_settings(tmp: Path, **kw) -> Settings:
    s = Settings(config_dir=tmp, data_dir=tmp / "data", database_url="sqlite://", proxy_url=None, no_proxy=[],
                 ca_bundle=None, allow_paid=False, enabled_paid_providers=[], enabled_optional=[])
    s.connect_timeout_s = 3.0
    s.read_timeout_s = 3.0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def svc(key: str, port: int, probes: list[dict], *, capability: str = "cap", priority: int = 1,
        host: str = "127.0.0.1", scheme: str = "http", enabled: bool = True) -> Service:
    return Service(key=key, capability=capability, priority=priority, host=host, port=port, scheme=scheme,
                   local=True, purpose=f"test {key}", enabled=enabled,
                   probes=[ProbeSpec(**p) for p in probes])


def registry(services: list[Service], capabilities: dict[str, bool] | None = None,
             paid: list[dict] | None = None) -> Registry:
    caps = capabilities or {s.capability: True for s in services}
    return Registry(version=1, user_agent="LeadIntel-test",
                    capabilities={k: Capability(required=v, description=k) for k, v in caps.items()},
                    services=services,
                    paid_providers=paid if paid is not None else [
                        {"key": "google_places", "hosts": ["places.googleapis.com", "maps.googleapis.com"],
                         "purpose": "paid"}],
                    not_required=[])
