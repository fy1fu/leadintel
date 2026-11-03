"""Wiring helpers: build registry/store/guard/diagnostics from settings.

Network diagnostics are deliberately independent of the main business database:
they use their own small store (LEADINTEL_NETDIAG_DB_URL, default data/netdiag.db)
and fall back to an in-memory store if that cannot be opened, so connectivity can
always be diagnosed.
"""
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


def open_netdiag_store(settings: Settings, *, persist: bool = True) -> NetStore:
    if not persist:
        return NetStore("sqlite://", mode="memory (persistence disabled)")
    url = settings.netdiag_url
    try:
        if url.startswith("sqlite:///") and not url.startswith("sqlite:////:memory"):
            from pathlib import Path
            Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        return NetStore(url, mode="persistent")
    except Exception as exc:  # noqa: BLE001 - diagnostics must run even if the store cannot be opened
        return NetStore("sqlite://", mode=f"memory (fallback: could not open diagnostics store: {type(exc).__name__})")


def build_context(settings: Settings | None = None, *, persist: bool = True) -> NetContext:
    settings = settings or get_settings()
    registry = load_registry(settings.network_registry_path)
    store = open_netdiag_store(settings, persist=persist)
    guard = CostGuard(registry, settings)
    return NetContext(settings, registry, store, guard, Diagnostics(registry, settings, store, guard))
