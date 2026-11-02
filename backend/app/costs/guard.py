"""Cost guard: the single place that decides whether a paid provider may be contacted.

Default policy: every paid provider is blocked. A paid call is allowed only when
LEADINTEL_ALLOW_PAID=true AND the provider key is listed in LEADINTEL_PAID_PROVIDERS.
Every blocked attempt is recorded so tests and the UI can prove no paid request happened.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import Settings
from app.net.registry import Registry


class PaidCallBlocked(RuntimeError):
    def __init__(self, provider: str, host: str):
        super().__init__(f"blocked request to paid provider '{provider}' ({host}); paid providers are disabled")
        self.provider = provider
        self.host = host


@dataclass
class Attempt:
    provider: str
    host: str
    allowed: bool
    at: datetime


@dataclass
class CostGuard:
    registry: Registry
    settings: Settings
    attempts: list[Attempt] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def provider_for(self, host: str) -> str | None:
        return self.registry.paid_hosts().get((host or "").lower().rstrip("."))

    def is_allowed(self, provider: str) -> bool:
        return self.settings.allow_paid and provider in self.settings.enabled_paid_providers

    def check(self, host: str) -> None:
        """Raise PaidCallBlocked if `host` belongs to a paid provider that is not explicitly enabled."""
        provider = self.provider_for(host)
        if provider is None:
            return
        allowed = self.is_allowed(provider)
        with self._lock:
            self.attempts.append(Attempt(provider, host, allowed, datetime.now(timezone.utc)))
        if not allowed:
            raise PaidCallBlocked(provider, host)

    def paid_requests_made(self) -> list[Attempt]:
        return [a for a in self.attempts if a.allowed]
