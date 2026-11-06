"""Website audits inside LeadIntel (M3): run, store and read.

* User-initiated, one audit at a time (``AuditJobs``), in a background thread of the web server - no worker
  infrastructure. The audit holds the B0 maintenance lock like every writer: it is refused while Load Data or an
  upgrade runs, and its screenshots and database writes are gated exactly like other business writes.
* All browser traffic goes through ``BrowserEgressProxy``; each audit uses a fresh browser (and a fresh context per
  site). Nothing is typed, clicked or submitted on the website; no cookies or form data are stored.
* A run left "running" by a stopped server is marked ``interrupted`` by the next audit.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit import benchmark as bench
from app.audit import recommend
from app.db.locks import OperationInProgress, ensure_write_allowed, maintenance_lock
from app.db.models import (Score, ScoreFactor, WebsiteAudit, WebsiteAuditArtifact, WebsiteAuditFinding, WebsiteAuditPage,
                           WebsiteAuditRecommendation, WebsiteBenchmark)
from app.scoring import wqs as wqs_mod

log = logging.getLogger(__name__)
KEEP_SCREENSHOT_AUDITS = 3          # screenshots are kept for the newest audits of a business; findings forever
STATE_TO_STATUS = {"blocked": "blocked", "robots_disallowed": "robots_disallowed", "robots_unavailable": "robots_unavailable",
                   "failed": "failed"}


class AuditUnavailable(RuntimeError):
    """The audit cannot start (no website, crawler component missing, another audit or operation running)."""


def crawler_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def start_url_for(conn, location_id: int) -> str | None:
    row = conn.execute(text(
        "SELECT url FROM online_profile WHERE location_id = :l AND platform_kind = 'independent' "
        "ORDER BY (verification_state = 'verified') DESC, id LIMIT 1"), {"l": location_id}).first()
    return row[0] if row else None


@dataclass
class AuditDeps:
    """What an audit needs from the outside world (replaced by synthetic doubles in tests)."""
    launch_browser: callable                  # (playwright) -> browser
    policy: object = None                     # EgressPolicy
    resolver: object = None                   # DNS resolver for the proxy and the robots fetcher
    history_client: callable = None           # () -> InfraClient-like client, or None to skip domain history
    guard: object = None
    limits: object = None                     # CrawlLimits; None = the defaults


def default_deps(settings) -> AuditDeps:
    from app.costs.guard import CostGuard
    from app.net.browser_proxy import BrowserEgressProxy
    from app.net.http import InfraClient
    from app.net.registry import load_registry
    reg = load_registry(settings.network_registry_path)
    guard = CostGuard(reg, settings)
    channel = os.environ.get("LEADINTEL_CRAWL_BROWSER") or ("msedge" if os.name == "nt" else None)

    def launch(p):
        kw = {"args": BrowserEgressProxy.chromium_args()}
        if channel:
            kw["channel"] = channel
        return p.chromium.launch(**kw)
    return AuditDeps(launch_browser=launch, guard=guard,
                     history_client=lambda: InfraClient(reg, settings, guard, timeout=20))


def run_audit(engine: Engine, data_dir: Path, location_id: int, deps: AuditDeps, *, requested_by: str | None = None,
              on_started=None) -> int:
    """Runs one audit end to end under the maintenance lock; returns the audit id."""
    with maintenance_lock(engine, "website audit", data_dir=data_dir):
        return run_audit_locked(engine, data_dir, location_id, deps, requested_by=requested_by, on_started=on_started)


def run_audit_locked(engine: Engine, data_dir: Path, location_id: int, deps: AuditDeps, *,
                     requested_by: str | None = None, on_started=None) -> int:
    """The audit itself; the caller holds the maintenance lock (the CLI takes it first, like every writing command).
    Every run is a new audit row: earlier audits, their evidence and the current WQS stay untouched unless this run
    produces a new score."""
    with Session(engine) as s:
        s.execute(text("UPDATE website_audit SET status = 'interrupted', finished_at = now(), "
                       "reason = 'LeadIntel stopped while this audit was running' WHERE status = 'running'"))
        url = start_url_for(s.connection(), location_id)
        if url is None:
            raise AuditUnavailable("no website is recorded for this business")
        audit = WebsiteAudit(location_id=location_id, status="running", start_url=url, requested_by=requested_by)
        s.add(audit)
        s.commit()
        audit_id = audit.id
    if on_started:
        on_started(audit_id)
    try:
        _crawl_and_store(engine, data_dir, audit_id, location_id, url, deps)
    except Exception as exc:
        with Session(engine) as s:
            s.execute(text("UPDATE website_audit SET status = 'failed', finished_at = now(), reason = :r "
                           "WHERE id = :i AND status = 'running'"),
                      {"i": audit_id, "r": str(exc) if isinstance(exc, AuditUnavailable)
                       else f"the audit stopped unexpectedly ({type(exc).__name__})"})
            s.commit()
        raise
    return audit_id


def _crawl_and_store(engine, data_dir, audit_id, location_id, url, deps: AuditDeps) -> None:
    import ssl

    from playwright.sync_api import sync_playwright

    from app.crawl.history import domain_history
    from app.crawl.runner import CrawlLimits, SiteCrawler, Watchdog, driver_killer
    from app.net.browser_proxy import BrowserEgressProxy
    from app.net.safe_egress import EgressPolicy, SafeFetcher, default_resolver
    policy = deps.policy or EgressPolicy()
    resolver = deps.resolver or default_resolver
    proxy = BrowserEgressProxy(policy=policy, resolver=resolver, guard=deps.guard).start()
    fetcher = SafeFetcher(deps.guard, ssl_context=ssl.create_default_context(), policy=policy, resolver=resolver,
                          user_agent="LeadIntelBot/0.1")
    res, watchdog = None, None
    try:
        cm = sync_playwright()
        with cm as p:
            kill = driver_killer(cm)
            watchdog = Watchdog(kill) if kill else None
            if watchdog is None:
                log.warning("browser watchdog unavailable: a page that never finishes could stall the audit")
            try:
                browser = deps.launch_browser(p)
            except Exception as exc:
                raise AuditUnavailable("the browser for website audits could not be started (LeadIntel uses Microsoft Edge "
                                       f"on Windows): {str(exc).splitlines()[0][:200]}") from exc
            try:
                res = SiteCrawler(fetcher=fetcher, proxy=proxy, browser=browser, data_dir=data_dir, watchdog=watchdog,
                                  limits=deps.limits or CrawlLimits()).crawl(url, f"location-{location_id}")
            finally:
                if not (watchdog and watchdog.fired):      # after the watchdog a browser call would wait forever
                    browser.close()
    except Exception:
        if not (res is not None and watchdog and watchdog.fired):   # leaving a killed driver can raise
            raise
    finally:
        proxy.stop()
        fetcher.close()
    history = None
    if deps.history_client and res.state == "crawled":
        try:
            with deps.history_client() as hc:
                history = domain_history(hc, url)
        except Exception as exc:  # noqa: BLE001 - optional observation
            history = {"status": f"unavailable ({type(exc).__name__})"}
    _store(engine, data_dir, audit_id, location_id, res, history)


def _store(engine: Engine, data_dir: Path, audit_id: int, location_id: int, res, history) -> None:
    criteria = res.criteria()
    w = wqs_mod.compute(criteria) if criteria else None
    if res.state == "crawled":
        loaded = [p for p in res.pages if p.facts]
        status = "failed" if not loaded else ("completed" if len(loaded) == len(res.pages) else "partial")
        reason = None if status == "completed" else (
            f"{len(res.pages) - len(loaded)} of {len(res.pages)} pages could not be loaded" if loaded
            else "no page of the website could be loaded")
    else:
        status, reason = STATE_TO_STATUS.get(res.state, "failed"), res.reason
    with Session(engine) as s:
        a = s.get(WebsiteAudit, audit_id)
        a.status, a.reason, a.robots, a.crawl_delay_s = status, reason, res.robots, res.crawl_delay_s
        a.pages_crawled = sum(1 for p in res.pages if p.facts)
        a.finished_at = _utcnow()
        a.observations = {"domain_history": history, "timing": res.perf_home,
                          "skipped": [[u, r] for u, r in res.skipped[:200]]}
        for i, pr in enumerate(res.pages, 1):
            page = WebsiteAuditPage(audit_id=audit_id, seq=i, url=pr.url[:2000], final_url=(pr.final_url or "")[:2000] or None,
                                    status=pr.status, title=pr.title, html_bytes=pr.html_bytes, text_sha256=pr.text_sha256,
                                    links=pr.links, elapsed_ms=pr.elapsed_ms, blocked_reason=pr.blocked_reason,
                                    error=pr.error, facts=pr.facts, axe=pr.axe)
            s.add(page)
            s.flush()
            for art in pr.artifacts:
                if _artifact_ok(data_dir, art):
                    s.add(WebsiteAuditArtifact(audit_id=audit_id, page_id=page.id, kind=art["kind"], rel_path=art["path"],
                                               sha256=art["sha256"], bytes=art["bytes"]))
        for c in criteria:
            s.add(WebsiteAuditFinding(audit_id=audit_id, criterion=c.key, dimension=c.dimension, kind=c.kind,
                                      passed=c.passed, weight=c.weight, evidence=c.evidence[:2000], source=c.source))
        b = None
        if w and w["value"] is not None:
            a.wqs, a.wqs_confidence, a.wqs_version, a.wqs_status = w["value"], w["confidence"], w["version"], w["status"]
            a.dimensions, a.explanation = w["dimensions"], w["explanation"]
            s.execute(text("UPDATE score SET superseded_at = now() WHERE location_id = :l AND kind = 'wqs' "
                           "AND superseded_at IS NULL"), {"l": location_id})
            sc = Score(location_id=location_id, kind="wqs", stage="provisional", value=w["value"], pre_cap_value=w["value"],
                       caps_applied=[], confidence=w["confidence"],
                       config_versions={"wqs": w["version"], "audit_id": audit_id, "calibration": w["calibration"]})
            s.add(sc)
            s.flush()
            for d, contribution in w["contributions"].items():
                s.add(ScoreFactor(score_id=sc.id, component="wqs_dimension", factor=d, effect=contribution,
                                  evidence={"dimension_score": w["dimensions"][d]["score"],
                                            "passed": w["dimensions"][d]["passed"], "failed": w["dimensions"][d]["failed"],
                                            "not_assessed": w["dimensions"][d]["not_assessed"], "audit_id": audit_id},
                                  provenance="measured"))
            a.score_id = sc.id
            s.flush()
            b = bench.compute(s.connection(), location_id, w["value"])
            s.add(WebsiteBenchmark(audit_id=audit_id, scope=b["scope"], niche=b.get("niche"), area=b.get("area"),
                                   peers=b["peers"], stats=b))
        elif w is not None:
            a.wqs_status, a.dimensions, a.explanation = w["status"], w["dimensions"], w["explanation"]
        for r in recommend.build(criteria, b if b and b["scope"] != "none" else None):
            s.add(WebsiteAuditRecommendation(audit_id=audit_id, **r))
        s.flush()
        old_files = _expire_screenshots(s, location_id)
        s.commit()
    _remove_files(data_dir, old_files)


def _expire_screenshots(s: Session, location_id: int) -> list[str]:
    """Retention: drops the screenshot rows of all but the newest audits of this business; returns their files."""
    rows = s.execute(text(
        "DELETE FROM website_audit_artifact x USING website_audit a WHERE a.id = x.audit_id AND a.location_id = :l "
        "AND a.id NOT IN (SELECT id FROM website_audit WHERE location_id = :l ORDER BY started_at DESC, id DESC LIMIT :k) "
        "RETURNING x.rel_path"), {"l": location_id, "k": KEEP_SCREENSHOT_AUDITS}).scalars().all()
    return list(rows)


def _remove_files(data_dir: Path, rel_paths: list[str]) -> None:
    """Deletes expired screenshots (only inside <data>/crawl), then any crawl folders left empty."""
    root = (Path(data_dir) / "crawl").resolve()
    dirs = set()
    for rel in rel_paths:
        p = (Path(data_dir) / rel).resolve()
        if not p.is_relative_to(root) or p == root:
            continue
        try:
            if p.is_file():
                ensure_write_allowed(p)
                p.unlink()
            dirs.add(p.parent)
        except OSError:
            log.warning("could not remove an expired screenshot: %s", rel)
    for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
        while d != root and d.is_relative_to(root):
            try:
                ensure_write_allowed(d)
                d.rmdir()                    # only when empty
            except OSError:
                break
            d = d.parent


def _utcnow():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc)


def _artifact_ok(data_dir: Path, art: dict) -> bool:
    """Only files inside <data>/crawl whose checksum matches are recorded."""
    try:
        root = (Path(data_dir) / "crawl").resolve()
        p = (Path(data_dir) / art["path"]).resolve()
        return p.is_relative_to(root) and p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == art["sha256"]
    except (OSError, KeyError, ValueError):
        return False


def artifact_bytes(conn, data_dir: Path, artifact_id: int) -> bytes | None:
    """Reads a stored screenshot; refuses paths outside <data>/crawl and files whose checksum no longer matches."""
    row = conn.execute(text("SELECT rel_path, sha256 FROM website_audit_artifact WHERE id = :i"), {"i": artifact_id}).first()
    if row is None:
        return None
    root = (Path(data_dir) / "crawl").resolve()
    p = (Path(data_dir) / row[0]).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    data = p.read_bytes()
    return data if hashlib.sha256(data).hexdigest() == row[1] else None


# ---------------------------------------------------------------- reads (API)
def history(conn, location_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(text(
        "SELECT id, status, start_url, started_at, finished_at, wqs, wqs_confidence, wqs_status, pages_crawled, reason "
        "FROM website_audit WHERE location_id = :l ORDER BY started_at DESC, id DESC LIMIT 50"), {"l": location_id}).mappings()]


def detail(conn, audit_id: int) -> dict | None:
    a = conn.execute(text("SELECT * FROM website_audit WHERE id = :i"), {"i": audit_id}).mappings().first()
    if a is None:
        return None
    out = dict(a)
    out["pages"] = [dict(r) for r in conn.execute(text(
        "SELECT p.id, p.seq, p.url, p.final_url, p.status, p.title, p.elapsed_ms, p.blocked_reason, p.error, p.links, "
        "coalesce((SELECT json_agg(json_build_object('id', x.id, 'kind', x.kind) ORDER BY x.id) FROM website_audit_artifact x "
        "WHERE x.audit_id = :i AND x.page_id = p.id), '[]'::json) AS artifacts, "
        "coalesce(jsonb_array_length(p.axe), 0) AS axe_issues "
        "FROM website_audit_page p WHERE p.audit_id = :i ORDER BY p.seq"), {"i": audit_id}).mappings()]
    out["findings"] = [dict(r) for r in conn.execute(text(
        "SELECT criterion, dimension, kind, passed, weight, evidence, source, measured_at FROM website_audit_finding "
        "WHERE audit_id = :i ORDER BY dimension, criterion"), {"i": audit_id}).mappings()]
    out["recommendations"] = [dict(r) for r in conn.execute(text(
        "SELECT criterion, severity, priority, title, finding, evidence, why, suggestion, benchmark "
        "FROM website_audit_recommendation WHERE audit_id = :i ORDER BY priority"), {"i": audit_id}).mappings()]
    b = conn.execute(text("SELECT stats FROM website_benchmark WHERE audit_id = :i"), {"i": audit_id}).first()
    out["benchmark"] = b[0] if b else None
    prev = conn.execute(text("SELECT id, started_at, wqs FROM website_audit WHERE location_id = :l AND wqs IS NOT NULL "
                             "AND (started_at, id) < (:t, :i) ORDER BY started_at DESC, id DESC LIMIT 1"),
                        {"l": a["location_id"], "t": a["started_at"], "i": audit_id}).mappings().first()
    out["previous"] = dict(prev) if prev else None
    if prev:
        out["previous"]["dimensions"] = conn.execute(text("SELECT dimensions FROM website_audit WHERE id = :i"),
                                                     {"i": prev["id"]}).scalar()
    out["calibration_note"] = wqs_mod.CALIBRATION_NOTE
    from app.api.security import safe_link        # links are vetted server-side; the page shows the rest as text
    out["start_href"] = safe_link("web", a["start_url"])
    for pg in out["pages"]:
        pg["href"] = safe_link("web", pg["final_url"] or pg["url"])
    out["dimension_labels"] = wqs_mod.DIMENSION_LABELS
    out["dimension_weights"] = wqs_mod.DIMENSION_WEIGHTS
    return out


# ---------------------------------------------------------------- one audit at a time, in the background
@dataclass
class Job:
    location_id: int
    state: str = "starting"         # starting | running | done | error
    audit_id: int | None = None
    message: str | None = None
    started: float = field(default_factory=time.time)


class AuditJobs:
    def __init__(self):
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.last: Job | None = None

    def busy(self) -> bool:
        return self.current is not None

    def start(self, engine: Engine, data_dir: Path, location_id: int, deps: AuditDeps, requested_by=None) -> Job:
        with self._lock:
            if self.current is not None:
                raise AuditUnavailable("another website audit is running; try again when it has finished")
            job = self.current = Job(location_id=location_id)

        def started(aid):
            job.audit_id, job.state = aid, "running"

        def work():
            try:
                job.audit_id = run_audit(engine, data_dir, location_id, deps, requested_by=requested_by, on_started=started)
                job.state = "done"
            except OperationInProgress:
                job.state, job.message = "error", ("another LeadIntel task (such as Load Data) is running; "
                                                   "start the audit again when it has finished")
            except AuditUnavailable as exc:
                job.state, job.message = "error", str(exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("website audit failed")
                job.state, job.message = "error", f"the audit stopped unexpectedly ({type(exc).__name__})"
            finally:
                with self._lock:
                    self.last, self.current = job, None
        threading.Thread(target=work, name=f"website-audit-{location_id}", daemon=True).start()
        return job

    def status(self) -> dict:
        j = self.current or self.last
        if j is None:
            return {"state": "idle"}
        return {"state": j.state, "location_id": j.location_id, "audit_id": j.audit_id, "message": j.message,
                "running": self.current is not None, "elapsed_s": round(time.time() - j.started, 1)}


JOBS = AuditJobs()
