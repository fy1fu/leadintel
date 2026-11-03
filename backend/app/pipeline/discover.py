"""End-to-end discovery run (PHASE1.md items 1.8-1.9).

config -> areas -> Overture snapshot/import -> Sunbiz download/import (+ ledger) -> geocode/placement
-> de-duplication -> coverage + report. Resumable and idempotent: already-imported files and
snapshots are reused; re-running creates no duplicates.

Coverage is recorded per source x area. A source that fails or cannot run is recorded as
`failed` / `not_run` with a reason - never as "0 businesses found". Days with unexplained gaps or
failed files make Sunbiz coverage `partial`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.appconfig.store import ConfigService
from app.config import Settings
from app.costs.guard import CostGuard
from app.db.models import Coverage, DiscoveryRun, SourceDateLedger, SourceFile
from app.db.session import check_database, make_engine
from app.geo.areas import load_counties, target_areas
from app.geo.geocode import CensusGeocoder, load_zip_counties
from app.net.http import InfraClient
from app.net.registry import load_registry
from app.resolve.matching import dedupe_locations, place_registry_orgs
from app.sources.common import ensure_source, register_file
from app.sources.overture.importer import import_snapshot
from app.sources.overture.reader import BBox, OvertureReader
from app.sources.overture.snapshot import SnapshotStore
from app.sources.sunbiz.client import MissingCredentials, SunbizClient, needs_refresh
from app.sources.sunbiz.importer import import_file
from app.sources.sunbiz.ledger import PARTIAL_STATES, classify_days

log = logging.getLogger("leadintel.discover")
SUNBIZ_KINDS = ("cor", "fic", "cor_event", "fic_event")


@dataclass
class RunResult:
    run_id: int
    status: str
    coverage: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def _cov(s: Session, run_id: int, source: str, areas, state: str, reason: str | None, counts: dict | None = None, detail=None):
    for a in areas:
        s.add(Coverage(run_id=run_id, source_key=source, area_id=a.id, state=state, reason=reason,
                       record_count=(counts or {}).get(a.key, 0), detail=detail or {}))


def run_discovery(settings: Settings, *, area_keys: list[str] | None = None, since: date | None = None,
                  until: date | None = None, overture_release: str | None = None, sunbiz: bool = True,
                  overture: bool = True, client: InfraClient | None = None, engine=None) -> RunResult:
    engine = engine or make_engine(settings)
    check_database(engine)
    cfgsvc = ConfigService(settings)
    with Session(engine) as s:
        cfgsvc.seed(s)
        s.commit()
    cfg = cfgsvc.effective(engine)
    reg = load_registry(settings.network_registry_path)
    guard = CostGuard(reg, settings)
    until = until or date.today()
    since = since or until - timedelta(days=30)
    own_client = client is None
    client = client or InfraClient(reg, settings, guard, timeout=600)
    try:
        return _run(settings, engine, cfg, reg, guard, client, area_keys, since, until, overture_release, sunbiz, overture)
    finally:
        if own_client:
            client.close()


LOCK_KEY = 0x4C454144  # pg advisory lock: one discovery run at a time


class RunInProgress(RuntimeError):
    pass


def _run(settings, engine, cfg, reg, guard, client, area_keys, since, until, overture_release, sunbiz, overture) -> RunResult:
    with engine.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            raise RunInProgress("another discovery run is in progress")
        try:
            with Session(engine) as s:  # we hold the lock, so any 'running' run is stale (process was killed)
                s.execute(text("UPDATE discovery_run SET status='interrupted', finished_at=now() WHERE status='running'"))
                s.commit()
            try:
                return _run_locked(settings, engine, cfg, reg, guard, client, area_keys, since, until, overture_release,
                                   sunbiz, overture)
            except Exception:
                with Session(engine) as s:  # never leave a crashed run looking like it is still running
                    s.execute(text("UPDATE discovery_run SET status='failed', finished_at=now() WHERE status='running'"))
                    s.commit()
                raise
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})


def _run_locked(settings, engine, cfg, reg, guard, client, area_keys, since, until, overture_release, sunbiz, overture) -> RunResult:
    with Session(engine) as s:
        load_counties(s, client, settings.data_dir, cfg["areas"])
        s.commit()
        areas = target_areas(s, area_keys)
        if not areas:
            raise ValueError(f"no enabled target areas match {area_keys}")
        target_fips = {a.spec["geoid"] for a in areas}
        run = DiscoveryRun(area_keys=[a.key for a in areas], status="running",
                           params={"since": since.isoformat(), "until": until.isoformat(), "overture_release": overture_release},
                           config_versions={k: v for k, v in cfg.versions.items()})
        s.add(run)
        s.commit()
        counts: dict = {"paid_requests": 0}
        partial = False

        # ---------------------------------------------------------------- Overture
        if overture:
            try:
                bbox_rows = s.execute(text("SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM "
                                           "(SELECT ST_Extent(geom) e FROM area WHERE id = ANY(:ids)) x"),
                                      {"ids": [a.id for a in areas]}).one()
                bbox = BBox(*[float(v) for v in bbox_rows])
                store = SnapshotStore(settings.data_dir)
                note = None
                try:
                    snap = store.ensure(OvertureReader(reg, client), bbox, release=overture_release)
                except Exception as exc:  # network down -> newest verified local snapshot, coverage partial
                    snap = store.latest_local()
                    if snap is None:
                        raise
                    note = f"latest release could not be checked ({type(exc).__name__}); used local snapshot {snap.release}"
                run.params = {**run.params, "overture_release": snap.release}
                ist = import_snapshot(s, snap, cfg["sources"])
                counts["overture"] = {"release": snap.release, "snapshot_reused": snap.reused, "in_target": ist.in_target_areas,
                                      "new_locations": ist.new_locations, "closed": ist.closed, "per_area": dict(ist.per_area)}
                _cov(s, run.id, "overture", areas, "partial" if note else "complete", note, dict(ist.per_area),
                     {"release": snap.release})
                partial |= bool(note)
                s.commit()
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                log.exception("overture failed")
                _cov(s, run.id, "overture", areas, "failed", f"{type(exc).__name__}: {exc}")
                partial = True
                s.commit()
        else:
            _cov(s, run.id, "overture", areas, "not_run", "disabled for this run")
            s.commit()

        # ---------------------------------------------------------------- Sunbiz
        if sunbiz:
            try:
                counts["sunbiz"] = _run_sunbiz(s, client, settings, cfg, since, until)
                gaps = counts["sunbiz"]["partial_days"]
                state = "partial" if gaps else "complete"
                reason = ("days with missing or failed files: " + ", ".join(f"{k}:{d}" for k, d in gaps[:20])) if gaps else None
                partial |= bool(gaps)
                zc = load_zip_counties(client, settings.data_dir)
                pst = place_registry_orgs(s, CensusGeocoder(client), zc, target_fips, cfg["sources"]["sources"]["sunbiz"]["reliability"])
                counts["placement"] = {k: v for k, v in pst.__dict__.items() if k != "methods"} | {"methods": dict(pst.methods)}
                per_area = dict(s.execute(text(
                    "SELECT a.key, count(DISTINCT lo.location_id) FROM location_organization lo JOIN location l ON l.id=lo.location_id "
                    "JOIN area a ON a.spec->>'geoid' = l.county_fips WHERE a.id = ANY(:ids) GROUP BY a.key"), {"ids": [a.id for a in areas]}).all())
                _cov(s, run.id, "sunbiz", areas, state, reason, per_area, {"since": since.isoformat(), "until": until.isoformat()})
                s.commit()
            except MissingCredentials as exc:
                s.rollback()
                _cov(s, run.id, "sunbiz", areas, "not_run", str(exc))
                partial = True
                s.commit()
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                log.exception("sunbiz failed")
                _cov(s, run.id, "sunbiz", areas, "failed", f"{type(exc).__name__}: {exc}")
                partial = True
                s.commit()
        else:
            _cov(s, run.id, "sunbiz", areas, "not_run", "disabled for this run")
            s.commit()

        # ---------------------------------------------------------------- DBPR licence evidence (M3)
        dbpr_cfg = cfg["sources"]["sources"].get("dbpr")
        if dbpr_cfg and dbpr_cfg.get("files"):
            try:
                counts["dbpr"] = _run_dbpr(s, client, settings, dbpr_cfg, target_fips)
                _cov(s, run.id, "dbpr", areas, "complete", "licence evidence only; licences never create prospects", {})
                s.commit()
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                log.exception("dbpr failed")
                _cov(s, run.id, "dbpr", areas, "failed", f"{type(exc).__name__}: {exc}")
                partial = True
                s.commit()
        else:
            _cov(s, run.id, "dbpr", areas, "not_run", "no DBPR files configured in the active 'sources' settings")
            s.commit()

        # ---------------------------------------------------------------- resolution + summary
        dst = dedupe_locations(s, sorted(target_fips))
        counts["dedupe"] = dst.__dict__
        s.commit()
        from app.scoring.runner import score_all
        counts["scoring"] = score_all(s, cfg, sorted(target_fips))
        counts["summary"] = summarize(s, target_fips)
        counts["paid_requests"] = len(guard.paid_requests_made())
        run.counts = counts
        run.status = "partial" if partial else "completed"
        run.finished_at = datetime.now(timezone.utc)
        s.commit()
        cov = _coverage_rows(s, run.id)
        return RunResult(run.id, run.status, cov, counts)


def _coverage_rows(s: Session, run_id: int) -> list[dict]:
    return [dict(r._mapping) for r in s.execute(text(
        "SELECT c.source_key AS source, a.key AS area, c.state, c.reason, c.record_count AS records "
        "FROM coverage c JOIN area a ON a.id = c.area_id WHERE c.run_id = :r ORDER BY 1, 2"), {"r": run_id})]


def _run_sunbiz(s: Session, client: InfraClient, settings: Settings, cfg, since: date, until: date,
                retry_delay: float = 5.0) -> dict:
    sc = SunbizClient(client, settings.data_dir)
    src = ensure_source(s, "sunbiz", cfg["sources"]["sources"]["sunbiz"])
    threshold = int(cfg["scoring"].get("mass_filer_address_threshold", 25))
    out = {"files_imported": 0, "files_reused": 0, "records": 0, "quarantined": 0, "partial_days": [], "ledger": {}}
    for kind in SUNBIZ_KINDS:
        listing = sc.list_kind(kind)
        in_range = [f for f in listing if since <= f.day <= until]
        failed: set[date] = set()
        for rf in in_range:
            prev = s.execute(select(SourceFile).where(SourceFile.source_id == src.id, SourceFile.remote_path == rf.path,
                                                      SourceFile.status == "imported")
                             .order_by(SourceFile.id.desc()).limit(1)).scalar_one_or_none()
            if not needs_refresh(prev.size if prev else None, prev.remote_last_modified if prev else None, rf):
                out["files_reused"] += 1
                continue
            if prev is not None:
                out.setdefault("files_refreshed", 0)
                out["files_refreshed"] += 1
            for attempt in (1, 2):  # one retry: isolated transient errors (e.g. a sporadic 401) were observed live
                try:
                    dl = sc.download(rf)
                    sf = register_file(s, src, kind=kind, remote_path=rf.path, dl=dl, logical_date=rf.day,
                                       remote_last_modified=rf.last_modified)
                    st = import_file(s, src, sf, kind, dl.path.read_bytes(), mass_filer_threshold=threshold)
                    out["files_imported"] += 1
                    out["records"] += st.parsed
                    out["quarantined"] += st.quarantined
                    s.commit()
                    break
                except Exception as exc:  # noqa: BLE001 - one bad day must not stop the run
                    s.rollback()
                    log.warning("sunbiz %s %s attempt %d failed: %s", kind, rf.day, attempt, exc)
                    if attempt == 2:
                        failed.add(rf.day)
                    else:
                        time.sleep(retry_delay)
        states = classify_days(since, until, {f.day for f in listing}, failed)
        for d, state in states.items():
            s.execute(insert(SourceDateLedger).on_conflict_do_update(
                index_elements=["source_id", "kind", "day"], set_={"state": state, "checked_at": text("now()")}),
                [{"source_id": src.id, "kind": kind, "day": d, "state": state}])
            if state in PARTIAL_STATES:  # filings AND status events: a missing event day can hide a dissolution
                out["partial_days"].append((kind, d.isoformat()))
        out["ledger"][kind] = {st: sum(1 for v in states.values() if v == st) for st in set(states.values())}
        s.commit()
    return out


def summarize(s: Session, target_fips: set[str]) -> dict:
    """Imported vs verified, kept separate on purpose (ingestion is not verification)."""
    f = sorted(target_fips)
    q = lambda sql: s.execute(text(sql), {"f": f}).all()  # noqa: E731
    per_county = {c: n for c, n in q("SELECT county_fips, count(*) FROM location WHERE merged_into_id IS NULL AND county_fips = ANY(:f) GROUP BY 1")}
    status = {k: n for k, n in q("SELECT operating_status, count(*) FROM location WHERE merged_into_id IS NULL AND county_fips = ANY(:f) GROUP BY 1")}
    states = {k: n for k, n in q("""SELECT f.verification_state, count(*) FROM fact f JOIN location l ON l.id=f.subject_id
        WHERE f.subject_type='location' AND f.superseded_at IS NULL AND l.merged_into_id IS NULL AND l.county_fips = ANY(:f) GROUP BY 1""")}
    return {"locations_per_county": per_county, "locations_by_status": status, "fact_verification_states": states,
            "verified_facts": states.get("verified", 0),
            "note": "Imported records are not verified business information; verification happens in later steps."}


def _run_dbpr(s: Session, client: InfraClient, settings: Settings, dbpr_cfg: dict, target_fips: set[str]) -> dict:
    from pathlib import Path

    from app.sources.common import download_checked
    from app.sources.dbpr.importer import PARSER_VERSION, import_extract
    src = ensure_source(s, "dbpr", dbpr_cfg)
    zc = load_zip_counties(client, settings.data_dir)
    out = {}
    for f in dbpr_cfg["files"]:
        name = Path(f["url"]).name
        dl = download_checked(client, f["url"], settings.data_dir / "dbpr" / name)
        prev = s.execute(select(SourceFile).where(SourceFile.source_id == src.id, SourceFile.remote_path == f["url"],
                                                  SourceFile.sha256 == dl.sha256, SourceFile.status == "imported",
                                                  SourceFile.parser_version == PARSER_VERSION).limit(1)).scalar_one_or_none()
        if prev is not None:
            out[name] = {"reused": True}
            continue
        sf = register_file(s, src, kind="dbpr_licensees", remote_path=f["url"], dl=dl)
        st = import_extract(s, src, sf, dl.path.read_bytes(), zc, target_fips)
        out[name] = {k: (dict(v) if hasattr(v, "items") else v) for k, v in st.__dict__.items()}
        s.commit()
    return out
