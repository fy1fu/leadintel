"""Command-line entry point: `leadintel net-check`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.db.locks import EXIT_LOCK_HELD, EXIT_LOCK_LOST, LockLost, OperationInProgress
from app.net.diagnostics import render_markdown, render_text
from app.net.service import build_context

EXIT_CODES_HELP = f"""exit codes (business commands):
  0  success
  2  invalid input (for example an unknown area key)
  4  discovery finished with partial coverage
  {EXIT_LOCK_HELD}  another LeadIntel operation holds the maintenance lock; nothing was changed
  {EXIT_LOCK_LOST}  the maintenance lock was lost during the operation; it stopped before any further write"""

# Every command is either reading or writing (Phase B0). Writing commands run under the shared maintenance
# lock; tests/test_write_coverage.py fails if a command is added without being classified here.
WRITING_COMMANDS = {("db", "upgrade"), ("config", "seed"), ("discover", None), ("score", None), ("import-btr", None)}
READING_COMMANDS = {("net-check", None), ("db", "status"), ("config", "show"), ("config", "history"),
                    ("report", None), ("export", None)}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="leadintel", epilog=EXIT_CODES_HELP,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    nc = sub.add_parser("net-check", help="probe every registered external service with real requests")
    nc.add_argument("--only", help="comma-separated service keys")
    nc.add_argument("--include-disabled", action="store_true", help="also probe optional services that are disabled")
    nc.add_argument("--json", action="store_true", help="print the full JSON report")
    nc.add_argument("--markdown", type=Path, help="write a Markdown report to this path")
    nc.add_argument("--no-run", action="store_true", help="report the latest stored results without probing")
    nc.add_argument("--no-store", action="store_true", help="keep results in memory only (no database at all)")
    db = sub.add_parser("db", help="business database maintenance")
    db.add_argument("action", choices=["upgrade", "status"])
    cf = sub.add_parser("config", help="versioned application settings (F1)")
    cf.add_argument("action", choices=["seed", "show", "history"])
    cf.add_argument("--kind", default="scoring")
    dc = sub.add_parser("discover", help="run discovery for the target areas")
    dc.add_argument("--areas", help="comma-separated area keys (default: all enabled targets)")
    dc.add_argument("--since", help="Sunbiz filings from this date (YYYY-MM-DD; default 30 days ago)")
    dc.add_argument("--until", help="Sunbiz filings up to this date (default today)")
    dc.add_argument("--release", help="Overture release (default: latest)")
    dc.add_argument("--no-sunbiz", action="store_true")
    dc.add_argument("--no-overture", action="store_true")
    sc = sub.add_parser("score", help="recompute provisional scores and qualification for the target areas")
    rp = sub.add_parser("report", help="niche comparison (no automatic winner)")
    rp.add_argument("--niches", help="comma-separated industry profiles (default: all)")
    ex = sub.add_parser("export", help="prospect CSV with provenance and verification states")
    ex.add_argument("path", type=Path)
    ex.add_argument("--niche")
    ex.add_argument("--qualification", help="comma-separated: provisionally_qualified,needs_research,not_qualified")
    ex.add_argument("--limit", type=int, default=100000)
    bt = sub.add_parser("import-btr", help="import a county business-tax-receipt CSV (manual export)")
    bt.add_argument("path", type=Path)
    bt.add_argument("--county", required=True, help="target area key, e.g. st_johns")
    bt.add_argument("--mapping", required=True, help="YAML file mapping fields to CSV column names")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "net-check":
        try:
            return _business(args)
        except OperationInProgress as exc:
            print(f"leadintel: {exc}. Nothing was changed; try again when it has finished.", file=sys.stderr)
            return EXIT_LOCK_HELD
        except LockLost as exc:
            print(f"leadintel: the maintenance lock was lost ({exc}); the operation stopped before any further "
                  "write. Run it again.", file=sys.stderr)
            for note in getattr(exc, "__notes__", []):
                print(f"leadintel: {note}", file=sys.stderr)
            return EXIT_LOCK_LOST

    ctx = build_context(persist=not args.no_store)
    if ctx.store.mode != "persistent":
        print(f"note: diagnostics store is {ctx.store.mode}", file=sys.stderr)
    if not args.no_run:
        only = [k.strip() for k in args.only.split(",")] if args.only else None
        ctx.diagnostics.run(only=only, include_disabled=args.include_disabled)
    report = ctx.diagnostics.full_report()
    if args.markdown:
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    critical = [c["key"] for c in report["capabilities"] if c["critical"]]
    if report["paid_requests_made"]:
        print("ERROR: paid requests were made", file=sys.stderr)
        return 3
    return 2 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())


def _business(args) -> int:
    """Commands that need the business database (network diagnostics never do)."""
    from datetime import date

    from app.config import get_settings
    from app.db.locks import maintenance_lock
    settings = get_settings()

    def locked(engine, operation: str):
        return maintenance_lock(engine, operation, data_dir=settings.data_dir)

    if args.cmd == "db":
        from pathlib import Path

        from alembic import command
        from alembic.config import Config
        cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
        cfg.set_main_option("sqlalchemy.url", settings.business_db_url)
        from app.db.session import make_engine
        if args.action == "upgrade":
            # Alembic uses its own engine to the same database; its commits pass the gate under this lock.
            with locked(make_engine(settings), "db upgrade"):
                command.upgrade(cfg, "head")
        else:
            # Reading only: Alembic would commit its (empty) transaction, which the fail-closed gate refuses
            # without the lock. Inside an explicit transaction Alembic does not commit; LeadIntel rolls it back.
            with make_engine(settings).connect() as conn:
                trans = conn.begin()
                try:
                    cfg.attributes["connection"] = conn
                    command.current(cfg, verbose=True)
                finally:
                    trans.rollback()
        return 0
    if args.cmd == "config":
        from sqlalchemy.orm import Session

        from app.appconfig.store import ConfigService
        from app.db.session import make_engine
        svc = ConfigService(settings)
        engine = make_engine(settings)
        if args.action == "seed":
            with locked(engine, "config seed"), Session(engine) as s:
                print("seeded:", svc.seed(s) or "nothing (already seeded)")
                s.commit()
        elif args.action == "show":
            eff = svc.effective(engine)
            print(json.dumps({"mode": eff.mode, "versions": eff.versions, "warnings": eff.warnings, args.kind: eff[args.kind]},
                             indent=2, default=str))
        else:
            with Session(engine) as s:
                for r in svc.history(s, args.kind):
                    print(f"v{r.version:<3} {r.status:<11} by {r.created_by:<10} approved_by={r.approved_by} {r.created_at:%Y-%m-%d %H:%M} {r.note or ''}")
        return 0
    if args.cmd == "discover":
        from app.pipeline.discover import run_discovery
        res = run_discovery(settings, area_keys=args.areas.split(",") if args.areas else None,
                            since=date.fromisoformat(args.since) if args.since else None,
                            until=date.fromisoformat(args.until) if args.until else None,
                            overture_release=args.release, sunbiz=not args.no_sunbiz, overture=not args.no_overture)
        print(json.dumps({"run_id": res.run_id, "status": res.status, "coverage": res.coverage, "counts": res.counts},
                         indent=2, default=str))
        return 0 if res.status == "completed" else 4
    if args.cmd == "import-btr":
        # Phase B0: the CSV is copied into the managed data folder (btr/<county>/) and imported from that
        # verified copy; see app/sources/btr/storage.py for the full sequence and its guarantees.
        import yaml

        from app.db.session import make_engine
        from app.sources.btr.csv_import import MappingError
        from app.sources.btr.storage import BtrImportError, prevalidate, run_btr_import
        try:
            with open(args.mapping, encoding="utf-8") as fh:
                mapping = yaml.safe_load(fh)
            limit = prevalidate(args.path, args.county, mapping)
            engine = make_engine(settings)
            with locked(engine, "import-btr"):
                out = run_btr_import(engine, settings.data_dir, args.path, args.county, mapping, limit)
        except (BtrImportError, MappingError, yaml.YAMLError, OSError) as exc:
            print(f"leadintel import-btr: {exc}", file=sys.stderr)
            for note in getattr(exc, "__notes__", []):
                print(f"leadintel import-btr: {note}", file=sys.stderr)
            return 2
        print(json.dumps({"action": out.action, "stored": out.stored, **out.stats, "warnings": out.warnings},
                         indent=2, default=str))
        return 0
    if args.cmd in ("score", "report", "export"):
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from app.appconfig.store import ConfigService
        from app.db.session import make_engine
        engine = make_engine(settings)
        if args.cmd == "score":
            from app.scoring.runner import score_all
            with locked(engine, "score"), Session(engine) as s:
                fips = [r[0] for r in s.execute(text("SELECT spec->>'geoid' FROM area WHERE kind='county' AND is_target AND enabled"))]
                print(json.dumps(score_all(s, ConfigService(settings).effective(engine), fips), indent=2))
                s.commit()
            return 0
        with Session(engine) as s:
            fips = [r[0] for r in s.execute(text("SELECT spec->>'geoid' FROM area WHERE kind='county' AND is_target AND enabled"))]
            if args.cmd == "report":
                from app.reports.prospects import niche_comparison
                print(json.dumps(niche_comparison(s, fips, args.niches.split(",") if args.niches else None), indent=2, default=str))
            else:
                from app.reports.prospects import export_csv
                n = export_csv(s, args.path, fips, niche=args.niche,
                               quals=args.qualification.split(",") if args.qualification else None, limit=args.limit)
                print(f"wrote {n} prospects to {args.path}")
        return 0
    return 1
