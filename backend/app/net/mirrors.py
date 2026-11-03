"""Generic mirror failover for interchangeable services (same `capability` in network.yaml).

Mirrors are tried healthiest-first (latest diagnostics), then by configured priority.
A mirror is skipped on transport errors, timeouts and 5xx/429 responses; every attempt
is recorded so the diagnostics report reflects real runtime behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, TypeVar

import httpx

from app.net.diagnostics import STATUS_RANK, service_status
from app.net.registry import Registry, Service
from app.net.store import NetStore

T = TypeVar("T")


class RetryableServiceError(RuntimeError):
    """Raise from a mirror operation to request failover (e.g. HTTP 5xx, checksum mismatch)."""


@dataclass
class Attempt:
    service_key: str
    ok: bool
    error: str | None = None


@dataclass
class AllMirrorsFailed(RuntimeError):
    capability: str
    attempts: list[Attempt] = field(default_factory=list)

    def __str__(self) -> str:
        detail = "; ".join(f"{a.service_key}: {a.error}" for a in self.attempts) or "no enabled mirrors"
        return f"all mirrors failed for {self.capability}: {detail}"


RETRYABLE = (httpx.TransportError, RetryableServiceError, TimeoutError, ConnectionError)


class MirrorSet:
    def __init__(self, registry: Registry, store: NetStore | None, capability: str,
                 enabled_extra: list[str] | None = None):
        self.capability = capability
        self.store = store
        latest = store.latest_results() if store else {}
        members = [s for s in registry.services_for(capability)
                   if registry.effective_enabled(s, enabled_extra)]

        def rank(s: Service) -> tuple[int, int]:
            rows = latest.get(s.key)
            status = service_status(True, rows)[0] if rows else "not_tested"
            return (STATUS_RANK.get(status, 9), s.priority)

        self.services = sorted(members, key=rank)

    def run(self, operation: Callable[[Service], T], *, op_name: str = "operation") -> tuple[T, Service, list[Attempt]]:
        attempts: list[Attempt] = []
        for svc in self.services:
            try:
                value = operation(svc)
            except RETRYABLE as exc:
                attempts.append(Attempt(svc.key, False, f"{type(exc).__name__}: {exc}"))
                if self.store:
                    self.store.record_event(svc.key, op_name, False, error=str(exc)[:500])
                continue
            attempts.append(Attempt(svc.key, True))
            return value, svc, attempts
        raise AllMirrorsFailed(self.capability, attempts)


def raise_for_retryable(resp: httpx.Response, *, retry_404: bool = True) -> httpx.Response:
    """5xx/429 always fail over; 404 fails over by default because mirrors can lag behind each other."""
    if resp.status_code >= 500 or resp.status_code == 429 or (retry_404 and resp.status_code == 404):
        raise RetryableServiceError(f"HTTP {resp.status_code} from {resp.url.host}")
    resp.raise_for_status()
    return resp
