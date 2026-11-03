"""Runtime settings, read from environment variables (no secrets in code)."""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


@dataclass
class Settings:
    config_dir: Path = field(default_factory=lambda: Path(os.environ.get("LEADINTEL_CONFIG_DIR", REPO_ROOT / "config")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("LEADINTEL_DATA_DIR", REPO_ROOT / "data")))
    database_url: str | None = field(default_factory=lambda: os.environ.get("LEADINTEL_DATABASE_URL"))
    # Network diagnostics use their OWN store so they keep working when the main business
    # database is down or not yet set up. Never derived from LEADINTEL_DATABASE_URL.
    netdiag_db_url: str | None = field(default_factory=lambda: os.environ.get("LEADINTEL_NETDIAG_DB_URL"))

    # Egress proxy. Default: honour the standard HTTPS_PROXY / NO_PROXY variables.
    proxy_url: str | None = field(default_factory=lambda: None if _env_bool("LEADINTEL_IGNORE_PROXY")
                                  else os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    no_proxy: list[str] = field(default_factory=lambda: _env_list("NO_PROXY") or _env_list("no_proxy"))
    # Additional CA bundle to trust on top of the system store (e.g. a corporate/egress proxy CA).
    ca_bundle: str | None = field(default_factory=lambda: os.environ.get("LEADINTEL_CA_BUNDLE")
                                  or os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))

    # Paid providers: blocked unless BOTH the global switch and the per-provider list allow them.
    allow_paid: bool = field(default_factory=lambda: _env_bool("LEADINTEL_ALLOW_PAID"))
    enabled_paid_providers: list[str] = field(default_factory=lambda: _env_list("LEADINTEL_PAID_PROVIDERS"))
    # Optional integrations enabled in addition to registry defaults (service keys).
    enabled_optional: list[str] = field(default_factory=lambda: _env_list("LEADINTEL_ENABLE_SERVICES"))

    connect_timeout_s: float = 10.0
    read_timeout_s: float = 20.0

    @property
    def network_registry_path(self) -> Path:
        return self.config_dir / "network.yaml"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.data_dir / 'leadintel.db'}"

    @property
    def netdiag_url(self) -> str:
        if self.netdiag_db_url:
            return self.netdiag_db_url
        return f"sqlite:///{self.data_dir / 'netdiag.db'}"

    @cached_property
    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()  # system trust store, hostname verification on
        if self.ca_bundle and Path(self.ca_bundle).is_file():
            ctx.load_verify_locations(cafile=self.ca_bundle)
        return ctx

    @property
    def proxy_address(self) -> tuple[str, int] | None:
        if not self.proxy_url:
            return None
        u = urlsplit(self.proxy_url)
        return (u.hostname or "", u.port or (443 if u.scheme == "https" else 80))

    def proxy_applies(self, host: str) -> bool:
        """True when traffic to `host` must go through the configured egress proxy."""
        if not self.proxy_url:
            return False
        h = host.lower().strip("[]")
        for entry in self.no_proxy:
            e = entry.lower().lstrip("*")
            if not e:
                continue
            if "/" in e:  # CIDR entries are matched for IP-literal hosts only
                import ipaddress
                try:
                    if ipaddress.ip_address(h) in ipaddress.ip_network(e, strict=False):
                        return False
                except ValueError:
                    pass
                continue
            if h == e.lstrip(".") or h.endswith(e if e.startswith(".") else "." + e):
                return False
        return True


def get_settings() -> Settings:
    return Settings()
