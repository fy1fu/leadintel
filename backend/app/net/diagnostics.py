"""Network diagnostics: run probes, roll results up per service and capability, render reports.

Status vocabulary (never "works" without a successful real request):

  operational                - every probe succeeded
  degraded                   - some probes succeeded, some failed
  partially_verified         - every probe that ran succeeded, but some were skipped (e.g. credentials missing)
  reachable_not_operational  - the network path was allowed (TCP/tunnel established) but the service failed
  blocked_by_network_policy  - the egress proxy refused the connection (allowlist or upstream refusal)
  dns_failure                - the hostname did not resolve (direct mode)
  unreachable_timeout        - connection attempts timed out
  unreachable                - other connection failure
  not_tested                 - probes skipped (e.g. credentials not configured)
  disabled                   - optional integration not enabled; never contacted
"""
from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.config import Settings
from app.costs.guard import CostGuard
from app.net.probe import ProbeResult, Prober
from app.net.registry import Registry, Service
from app.net.store import NetStore, ProbeRow

STATUS_RANK = {  # lower is healthier; used for mirror ordering and roll-ups
    "operational": 0, "partially_verified": 1, "degraded": 1, "not_tested": 2, "reachable_not_operational": 3, "unreachable_timeout": 4,
    "unreachable": 4, "dns_failure": 4, "blocked_by_network_policy": 5, "disabled": 6,
}


@dataclass
class ProbeSummary:
    probe: str
    outcome: str
    failure_stage: str | None
    failure_kind: str | None
    error: str | None
    http_status: int | None
    bytes: int
    duration_ms: float
    at: str
    url: str | None


@dataclass
class ServiceReport:
    key: str
    host: str
    port: int
    capability: str
    purpose: str
    required: bool
    enabled: bool
    cost_class: str
    priority: int
    status: str
    network_allowed: bool | None
    last_checked: str | None
    last_success: str | None
    last_successful_download: str | None
    last_download_bytes: int
    probes: list[ProbeSummary] = field(default_factory=list)


@dataclass
class CapabilityReport:
    key: str
    description: str
    required: bool
    status: str             # operational | operational_via_fallback | partially_verified | degraded | unavailable | not_tested | disabled
    active_service: str | None
    critical: bool
    services: list[str]


def service_status(enabled: bool, rows: list[ProbeResult] | list[ProbeRow]) -> tuple[str, bool | None]:
    if not enabled:
        return "disabled", None
    ran = [r for r in rows if r.outcome != "skipped"]
    if not ran:
        return "not_tested", None
    ok = [r for r in ran if r.outcome == "success"]
    allowed: bool | None = None
    if any(r.network_reached for r in ran):
        allowed = True
    elif any(r.failure_kind == "proxy_denied" for r in ran):
        allowed = False
    if len(ok) == len(ran):
        return ("partially_verified" if len(ran) < len(rows) else "operational"), True
    if ok:
        return "degraded", True
    if allowed:
        return "reachable_not_operational", True
    kinds = {r.failure_kind or "" for r in ran}
    if "proxy_denied" in kinds or "paid_provider_blocked" in kinds:
        return "blocked_by_network_policy", allowed
    if any(k.startswith("dns") for k in kinds):
        return "dns_failure", allowed
    if any(k.endswith("timeout") for k in kinds):
        return "unreachable_timeout", allowed
    return "unreachable", allowed


class Diagnostics:
    def __init__(self, registry: Registry, settings: Settings, store: NetStore, guard: CostGuard | None = None,
                 prober: Prober | None = None):
        self.registry = registry
        self.settings = settings
        self.store = store
        self.guard = guard or CostGuard(registry, settings)
        self.prober = prober or Prober(registry, settings, self.guard)

    def enabled(self, svc: Service) -> bool:
        return self.registry.effective_enabled(svc, self.settings.enabled_optional)

    # ---- run ----------------------------------------------------------------
    def run(self, only: list[str] | None = None, include_disabled: bool = False, workers: int = 8) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        jobs = [(svc, spec) for svc in self.registry.services
                if (not only or svc.key in only) and (include_disabled or self.enabled(svc))
                for spec in svc.probes]
        results: list[ProbeResult] = []
        # Probes of the same service run sequentially (polite); services run in parallel.
        by_service: dict[str, list] = {}
        for svc, spec in jobs:
            by_service.setdefault(svc.key, []).append((svc, spec))

        def run_service(items: list) -> list[ProbeResult]:
            return [self.prober.run(svc, spec) for svc, spec in items]

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for out in pool.map(run_service, by_service.values()):
                results.extend(out)
        self.store.save_probe_results(run_id, results)
        return run_id

    # ---- report ---------------------------------------------------------------
    def service_reports(self) -> list[ServiceReport]:
        latest = self.store.latest_results()
        reports = []
        for svc in self.registry.services:
            current = {p.key for p in svc.probes}  # ignore results of probes no longer defined
            rows = [r for r in latest.get(svc.key, []) if r.probe_key in current]
            enabled = self.enabled(svc)
            status, allowed = service_status(enabled, rows)
            dl_at, dl_bytes = self.store.last_successful_download(svc.key)
            ls = self.store.last_success(svc.key)
            reports.append(ServiceReport(
                key=svc.key, host=svc.host, port=svc.port, capability=svc.capability, purpose=svc.purpose,
                required=self.registry.capabilities[svc.capability].required, enabled=enabled,
                cost_class=svc.cost_class, priority=svc.priority, status=status, network_allowed=allowed,
                last_checked=max((r.started_at for r in rows), default=None) and max(r.started_at for r in rows).isoformat(),
                last_success=ls.isoformat() if ls else None,
                last_successful_download=dl_at.isoformat() if dl_at else None, last_download_bytes=dl_bytes,
                probes=[ProbeSummary(r.probe_key, r.outcome, r.failure_stage, r.failure_kind, r.error, r.http_status,
                                     r.bytes_downloaded, r.duration_ms, r.started_at.isoformat(), r.url)
                        for r in sorted(rows, key=lambda x: x.probe_key)]))
        return reports

    def capability_reports(self, services: list[ServiceReport] | None = None) -> list[CapabilityReport]:
        services = services or self.service_reports()
        out = []
        for cap_key, cap in self.registry.capabilities.items():
            members = sorted((s for s in services if s.capability == cap_key), key=lambda s: s.priority)
            enabled = [s for s in members if s.enabled]
            active = next((s for s in enabled if s.status == "operational"), None)
            if not enabled:
                status = "disabled"
            elif active is not None:
                status = "operational" if active is enabled[0] else "operational_via_fallback"
            elif any(s.status in ("degraded", "partially_verified") for s in enabled):
                active = next(s for s in enabled if s.status in ("degraded", "partially_verified"))
                status = active.status
            elif all(s.status == "not_tested" for s in enabled):
                status = "not_tested"
            else:
                status = "unavailable"
            out.append(CapabilityReport(cap_key, cap.description, cap.required, status,
                                        active.key if active else None,
                                        critical=cap.required and status in ("unavailable",),
                                        services=[s.key for s in members]))
        return out

    def full_report(self) -> dict:
        services = self.service_reports()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "latest_run_id": self.store.latest_run_id(),
            "egress": {"proxy": bool(self.settings.proxy_url),
                       "note": "Hosts reached through an egress proxy: DNS is resolved by the proxy; "
                               "'network_allowed' means the proxy established the tunnel."},
            "capabilities": [asdict(c) for c in self.capability_reports(services)],
            "services": [asdict(s) for s in services],
            "paid_providers": [{"key": p.key, "hosts": p.hosts, "purpose": p.purpose,
                                "status": "allowed" if self.guard.is_allowed(p.key) else "blocked_by_policy",
                                "contacted": False}
                               for p in self.registry.paid_providers],
            "paid_requests_made": [asdict(a) | {"at": a.at.isoformat()} for a in self.guard.paid_requests_made()],
            "not_required": [n.model_dump() for n in self.registry.not_required],
        }


# --------------------------------------------------------------------------- rendering
def render_text(report: dict) -> str:
    lines = ["CAPABILITIES"]
    for c in report["capabilities"]:
        flag = " !! REQUIRED CAPABILITY UNAVAILABLE" if c["critical"] else ""
        lines.append(f"  {c['key']:<18} {c['status']:<25} {'required' if c['required'] else 'optional':<9} "
                     f"active={c['active_service'] or '-'}{flag}")
    lines.append("\nSERVICES")
    for s in report["services"]:
        lines.append(f"  {s['key']:<24} {s['host'] + ':' + str(s['port']):<52} {s['status']:<26} "
                     f"allowed={'?' if s['network_allowed'] is None else s['network_allowed']}")
        for p in s["probes"]:
            err = f" [{p['failure_stage']}/{p['failure_kind']}] {(p['error'] or '')[:100]}" if p["outcome"] != "success" else ""
            lines.append(f"      - {p['probe']:<22} {p['outcome']:<8} {p['bytes']:>7}B {p['duration_ms']:>8.0f}ms{err}")
    lines.append("\nPAID PROVIDERS (blocked unless explicitly enabled)")
    for p in report["paid_providers"]:
        lines.append(f"  {p['key']:<18} {p['status']}")
    lines.append(f"  paid requests made: {len(report['paid_requests_made'])}")
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    def yn(v):
        return "yes" if v is True else "no" if v is False else "unknown"
    md = [f"# LeadIntel network diagnostics", "",
          f"Generated {report['generated_at']} (run `{report['latest_run_id']}`). "
          f"Egress proxy in use: {yn(report['egress']['proxy'])}.", "",
          "## Capabilities", "", "| Capability | Required | Status | Active service |", "|---|---|---|---|"]
    for c in report["capabilities"]:
        md.append(f"| {c['key']} | {yn(c['required'])} | {'**' + c['status'] + '**' if c['critical'] else c['status']} | {c['active_service'] or '—'} |")
    md += ["", "## Services", "",
           "| Service | Host | Purpose | Required | Status | Network allowed | Last successful download | Latest error |",
           "|---|---|---|---|---|---|---|---|"]
    for s in report["services"]:
        errs = [p for p in s["probes"] if p["outcome"] not in ("success",)]
        err = "; ".join(f"{p['probe']}: {p['failure_stage'] or ''}/{p['failure_kind'] or ''}" for p in errs) or "—"
        md.append(f"| {s['key']} | `{s['host']}:{s['port']}` | {s['purpose']} | {yn(s['required'])} | {s['status']} | "
                  f"{yn(s['network_allowed'])} | {s['last_successful_download'] or '—'} | {err} |")
    md += ["", "## Paid providers", "", "| Provider | Hosts | Status |", "|---|---|---|"]
    for p in report["paid_providers"]:
        md.append(f"| {p['key']} | {', '.join(p['hosts'])} | {p['status']} |")
    md += ["", f"Paid requests made: **{len(report['paid_requests_made'])}**", "",
           "## Hosts not required", "", "| Host | Reason |", "|---|---|"]
    for n in report["not_required"]:
        md.append(f"| `{n['host']}` | {n['reason']} |")
    return "\n".join(md) + "\n"
