"""Wiring helpers: build registry/store/guard/diagnostics from settings."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.costs.guard import CostGuard
from app.net.diagnostics import Diagnostics
from app.net.registry import Registry, load_registry
from app.net.store import NetStore


@dataclass
class NetContext:
    settings: Settings
    registry: Registry
    store: NetStore
    guard: CostGuard
    diagnostics: Diagnostics


def build_context(settings: Settings | None = None) -> NetContext:
    settings = settings or get_settings()
    registry = load_registry(settings.network_registry_path)
    store = NetStore(settings.db_url)
    guard = CostGuard(registry, settings)
    return NetContext(settings, registry, store, guard, Diagnostics(registry, settings, store, guard))
