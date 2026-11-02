"""FastAPI application (network diagnostics endpoints for now)."""
from __future__ import annotations

import html

from fastapi import FastAPI, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from app.net.service import NetContext, build_context


def create_app(ctx: NetContext | None = None) -> FastAPI:
    app = FastAPI(title="LeadIntel", version="0.2.0")
    app.state.net = ctx or build_context()

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/network/diagnostics")
    def get_diagnostics() -> dict:
        return app.state.net.diagnostics.full_report()

    @app.post("/api/network/diagnostics/run")
    async def run_diagnostics(only: list[str] | None = Query(default=None),
                              include_disabled: bool = False) -> dict:
        d = app.state.net.diagnostics
        run_id = await run_in_threadpool(d.run, only, include_disabled)
        return {"run_id": run_id, **d.full_report()}

    @app.get("/diagnostics/network", response_class=HTMLResponse)
    def diagnostics_page() -> str:
        return render_html(app.state.net.diagnostics.full_report())

    return app


STATUS_CLASS = {
    "operational": "ok", "operational_via_fallback": "warn", "degraded": "warn", "not_tested": "muted",
    "disabled": "muted", "reachable_not_operational": "bad", "blocked_by_network_policy": "bad",
    "dns_failure": "bad", "unreachable_timeout": "bad", "unreachable": "bad", "unavailable": "bad",
    "blocked_by_policy": "muted",
}


def _badge(status: str) -> str:
    return f'<span class="badge {STATUS_CLASS.get(status, "muted")}">{html.escape(status.replace("_", " "))}</span>'


def render_html(report: dict) -> str:
    e = html.escape
    cap_rows = "".join(
        f"<tr><td>{e(c['key'])}<div class='sub'>{e(c['description'])}</div></td>"
        f"<td>{'Required' if c['required'] else 'Optional'}</td><td>{_badge(c['status'])}"
        f"{' <strong class=crit>required capability unavailable</strong>' if c['critical'] else ''}</td>"
        f"<td>{e(c['active_service'] or '—')}</td></tr>" for c in report["capabilities"])
    svc_rows = []
    for s in report["services"]:
        errs = [p for p in s["probes"] if p["outcome"] != "success"]
        err_html = "".join(
            f"<div class='err'><code>{e(p['probe'])}</code> {e(p['outcome'])}"
            f"{' · ' + e((p['failure_stage'] or '') + '/' + (p['failure_kind'] or '')) if p['failure_kind'] else ''}"
            f"<div class='sub'>{e((p['error'] or '')[:240])}</div></div>" for p in errs) or "—"
        allowed = {True: "yes", False: "no", None: "unknown"}[s["network_allowed"]]
        svc_rows.append(
            f"<tr><td><strong>{e(s['key'])}</strong><div class='sub'>{e(s['purpose'])}</div></td>"
            f"<td><code>{e(s['host'])}:{s['port']}</code></td>"
            f"<td>{'Required' if s['required'] else 'Optional'}{'' if s['enabled'] else ' · disabled'}</td>"
            f"<td>{_badge(s['status'])}</td><td>{allowed}</td>"
            f"<td>{e(s['last_checked'] or 'never')}</td>"
            f"<td>{e(s['last_successful_download'] or '—')}</td><td>{err_html}</td></tr>")
    paid_rows = "".join(f"<tr><td>{e(p['key'])}</td><td><code>{e(', '.join(p['hosts']))}</code></td>"
                        f"<td>{_badge(p['status'])}</td></tr>" for p in report["paid_providers"])
    nr_rows = "".join(f"<tr><td><code>{e(n['host'])}</code></td><td>{e(n['reason'])}</td></tr>"
                      for n in report["not_required"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Network diagnostics · LeadIntel</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--line:#e3e2dd;--ok:#1e7a46;--warn:#9a6700;--bad:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ececea;--muted:#a3a39e;--card:#1e1e1c;--line:#33332f;--ok:#4cc38a;--warn:#e2b340;--bad:#ff6b5e}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1280px;margin:0 auto;padding:24px 16px}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}}
.sub{{color:var(--muted);font-size:12px}} .wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}}
table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;color:var(--muted);font-weight:600}} code{{font-size:12px}}
.badge{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid currentColor;white-space:nowrap}}
.ok{{color:var(--ok)}} .warn{{color:var(--warn)}} .bad{{color:var(--bad)}} .muted{{color:var(--muted)}} .crit{{color:var(--bad)}}
.err{{margin-bottom:6px}} form{{margin:12px 0}} button{{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}}
</style></head><body><main>
<h1>Network diagnostics</h1>
<div class="sub">Generated {e(report['generated_at'])} · latest run {e(report['latest_run_id'] or 'none yet')} ·
egress proxy {'in use' if report['egress']['proxy'] else 'not used'}. A service is only shown as operational after a real request succeeded.</div>
<form method="post" action="/api/network/diagnostics/run" onsubmit="event.preventDefault();this.querySelector('button').disabled=true;fetch(this.action,{{method:'POST'}}).then(()=>location.reload())">
<button type="submit">Run diagnostics now</button></form>
<h2>Capabilities</h2><div class="wrap"><table><thead><tr><th>Capability</th><th>Need</th><th>Status</th><th>Active service</th></tr></thead><tbody>{cap_rows}</tbody></table></div>
<h2>Services</h2><div class="wrap"><table><thead><tr><th>Service</th><th>Host</th><th>Need</th><th>Status</th><th>Network allowed</th><th>Last checked</th><th>Last successful download</th><th>Errors</th></tr></thead><tbody>{''.join(svc_rows)}</tbody></table></div>
<h2>Paid providers</h2><div class="sub">Blocked by the cost guard unless explicitly enabled with a budget. Paid requests made: {len(report['paid_requests_made'])}.</div>
<div class="wrap"><table><thead><tr><th>Provider</th><th>Hosts</th><th>Status</th></tr></thead><tbody>{paid_rows}</tbody></table></div>
<h2>Hosts not required</h2><div class="wrap"><table><thead><tr><th>Host</th><th>Reason</th></tr></thead><tbody>{nr_rows}</tbody></table></div>
</main></body></html>"""


# Run with: uvicorn app.api.main:create_app --factory
