"""HTTP client for fixed infrastructure hosts (data sources, tiles, geocoders).

* Only hosts registered in config/network.yaml (and enabled) may be contacted.
* Paid-provider hosts are rejected by the cost guard before any connection is made.
* TLS verification is always on; the standard egress proxy settings are honoured.
Business websites must NOT use this client - see app/net/safe_egress.py.
"""
from __future__ import annotations

import httpx

from app.config import Settings
from app.costs.guard import CostGuard
from app.net.registry import Registry


class UnregisteredHostError(RuntimeError):
    pass


class DisabledServiceError(RuntimeError):
    pass


class InfraClient:
    def __init__(self, registry: Registry, settings: Settings, guard: CostGuard,
                 transport: httpx.BaseTransport | None = None, timeout: float | None = None):
        self.registry = registry
        self.settings = settings
        self.guard = guard
        kwargs: dict = {}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.Client(
            headers={"User-Agent": registry.user_agent},
            timeout=httpx.Timeout(timeout or settings.read_timeout_s, connect=settings.connect_timeout_s),
            verify=settings.ssl_context,
            follow_redirects=False,
            trust_env=transport is None,  # standard HTTPS_PROXY/NO_PROXY handling
            event_hooks={"request": [self._check_request]},
            **kwargs,
        )

    def _check_request(self, request: httpx.Request) -> None:
        host = request.url.host
        self.guard.check(host)  # raises PaidCallBlocked
        svc = self.registry.is_registered_host(host, request.url.port or (443 if request.url.scheme == "https" else 80))
        if svc is None:
            raise UnregisteredHostError(f"{host} is not a registered infrastructure host (config/network.yaml)")
        if not self.registry.effective_enabled(svc, self.settings.enabled_optional):
            raise DisabledServiceError(f"service {svc.key} ({host}) is disabled")

    def get(self, url: str, **kw) -> httpx.Response:
        return self._request("GET", url, **kw)

    def head(self, url: str, **kw) -> httpx.Response:
        return self._request("HEAD", url, **kw)

    def stream(self, method: str, url: str, **kw):
        return self._client.stream(method, url, **kw)

    def _request(self, method: str, url: str, *, max_redirects: int = 3, **kw) -> httpx.Response:
        resp = self._client.request(method, url, **kw)
        hops = 0
        while resp.is_redirect and hops < max_redirects:  # each hop passes through _check_request again
            hops += 1
            resp = self._client.request(method, resp.headers["location"] if "://" in resp.headers["location"]
                                        else str(resp.url.join(resp.headers["location"])), **kw)
        return resp

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "InfraClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
