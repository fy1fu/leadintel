"""Typed loader for config/network.yaml (the external-service registry)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

CostClass = Literal["free", "free_limited", "paid"]
ProbeKind = Literal["http", "download", "ssh_banner", "sftp_list"]


class ProbeSpec(BaseModel):
    key: str
    kind: ProbeKind
    url: str | None = None
    resolver: str | None = None          # dynamic URL resolution (e.g. latest Overture object)
    path: str | None = None              # sftp path
    expect_status: list[int] = Field(default_factory=lambda: [200, 206])
    expect_body_contains: str | None = None
    expect_body_contains_bytes: str | None = None
    expect_body_regex: str | None = None
    expect_magic: str | None = None
    expect_magic_hex: str | None = None
    range_bytes: int = 0                 # 0 = whole (small) body, capped by max_bytes
    max_bytes: int = 262_144
    min_bytes: int = 1
    timeout_s: float | None = None
    needs_env: list[str] = Field(default_factory=list)
    method: Literal["GET", "PROPFIND", "HEAD", "OPTIONS"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    # Names of the env vars holding username and password for HTTP Basic auth. Values are never
    # stored in config, results, logs or reports.
    basic_auth_env: list[str] = Field(default_factory=list, max_length=2)


class Service(BaseModel):
    key: str
    capability: str
    priority: int = 1
    host: str
    port: int = 443
    scheme: Literal["https", "http"] | None = None
    local: bool = False
    purpose: str
    enabled: bool = True
    cost_class: CostClass = "free"
    probes: list[ProbeSpec]

    @property
    def base_url(self) -> str:
        scheme = self.scheme or ("http" if self.local else "https")
        default = 443 if scheme == "https" else 80
        return f"{scheme}://{self.host}" + ("" if self.port == default else f":{self.port}")


class Capability(BaseModel):
    required: bool
    description: str


class PaidProvider(BaseModel):
    key: str
    hosts: list[str]
    purpose: str


class NotRequired(BaseModel):
    host: str
    reason: str


class Registry(BaseModel):
    version: int
    user_agent: str
    capabilities: dict[str, Capability]
    services: list[Service]
    paid_providers: list[PaidProvider] = Field(default_factory=list)
    not_required: list[NotRequired] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "Registry":
        keys = [s.key for s in self.services]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate service keys in network registry")
        for s in self.services:
            if s.capability not in self.capabilities:
                raise ValueError(f"service {s.key} references unknown capability {s.capability}")
            if s.cost_class == "paid":
                raise ValueError(f"service {s.key}: paid services belong under paid_providers")
        paid = self.paid_hosts()
        for s in self.services:
            if s.host in paid:
                raise ValueError(f"service {s.key} uses paid host {s.host}")
        return self

    # -- lookups -------------------------------------------------------------
    def paid_hosts(self) -> dict[str, str]:
        return {h.lower(): p.key for p in self.paid_providers for h in p.hosts}

    def service(self, key: str) -> Service:
        for s in self.services:
            if s.key == key:
                return s
        raise KeyError(key)

    def services_for(self, capability: str) -> list[Service]:
        return sorted((s for s in self.services if s.capability == capability), key=lambda s: s.priority)

    def is_registered_host(self, host: str, port: int | None = None) -> Service | None:
        h = host.lower()
        for s in self.services:
            if s.host.lower() == h and (port is None or s.port == port):
                return s
        return None

    def effective_enabled(self, service: Service, extra_enabled: list[str] | None = None) -> bool:
        return service.enabled or service.key in (extra_enabled or [])


def load_registry(path: Path) -> Registry:
    with open(path, encoding="utf-8") as fh:
        return Registry.model_validate(yaml.safe_load(fh))
