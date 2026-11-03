"""Command-line entry point: `leadintel net-check`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.net.diagnostics import render_markdown, render_text
from app.net.service import build_context


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="leadintel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    nc = sub.add_parser("net-check", help="probe every registered external service with real requests")
    nc.add_argument("--only", help="comma-separated service keys")
    nc.add_argument("--include-disabled", action="store_true", help="also probe optional services that are disabled")
    nc.add_argument("--json", action="store_true", help="print the full JSON report")
    nc.add_argument("--markdown", type=Path, help="write a Markdown report to this path")
    nc.add_argument("--no-run", action="store_true", help="report the latest stored results without probing")
    nc.add_argument("--no-store", action="store_true", help="keep results in memory only (no database at all)")
    args = ap.parse_args(argv)

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
