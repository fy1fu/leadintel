# LeadIntel — development handoff (for a fresh session)

Read this first, then [DELIVERY_STATUS.md](DELIVERY_STATUS.md) (authoritative status, tests, approvals). Do not
re-plan or re-derive what is below; the design documents are [PLAN.md](PLAN.md), [ROADMAP.md](ROADMAP.md),
[SCORING.md](SCORING.md), [research/M3_PREP.md](research/M3_PREP.md) and [security/M2_CRM_WRITE_PATH.md](security/M2_CRM_WRITE_PATH.md).

## Product

Local-first prospecting tool for a small web-design business in north-east Florida (Duval, Clay, St. Johns). It
finds local businesses from public data, scores how promising each is as a website customer (BOS, LPS), audits
their existing websites (WQS), and tracks the sales work (local CRM). Runs on one Windows PC ("Friend Edition":
per-user install, local PostgreSQL + PostGIS, Edge app window). No cloud, no paid services without approval.

## Repository

- `/home/user/leadintel`, branch `phase-1`, local only; GitHub `origin/phase-1` is at `b20f99a` (local is ahead;
  run `git log --oneline origin/phase-1..HEAD`). Never push without approval.
- Backend: `backend/` (FastAPI, SQLAlchemy 2, Alembic head **0007**, psycopg3, static JS frontend in
  `backend/app/api/static`, MapLibre 4.7.1). CLI: `backend/app/cli.py` (Windows: `packaging/windows/run_cli.py`).
- Windows packaging: `packaging/windows/` (Setup/launch/Load Data PowerShell, `build_friend_zip.py`, hashed lock
  `requirements-win-py313.lock`).

## Key design rules (keep them)

- B0 maintenance lock + fail-closed commit gate: every business write and every managed-file write happens under
  `maintenance_lock`; `tests/test_write_coverage.py` classifies every CLI command and file/commit site.
- CRM writes: maintenance lock by default; a separate `leadintel_crm` login restricted by PostgreSQL to the CRM
  tables (`self_check`) may write during Load Data. Do not widen it.
- One shared `ProspectFilter` (`app/api/filters.py`) for list, counts, map, export and saved searches; the Search
  Area radius is canonical in metres (400 m – 160,934.4 m).
- Crawling: user-initiated, one audit at a time, robots.txt, ≤ 15 pages, 1 s per host, browser only through
  `BrowserEgressProxy` (SSRF / DNS rebinding), GET/HEAD only, watchdog against hung pages, no form filling.
  Development crawls only against synthetic local sites (127.0.0.2 via the test-only policy hook).
- Unknown is never poor: not-assessed criteria never lower WQS; inaccessible sites get no score.
- WQS `wqs-1.0` is **provisional** until human calibration; BOS/LPS must not change silently (versioned changes only).
- Screenshots in `<data>/crawl/` with sha256 in the database; newest 3 audits per business keep them.

## Status

- Done: Phase 1 data pipeline, B0, M1 interface, M2 CRM, M3 Search Area + center search, egress proxy, crawl
  runner, website checks (26 criteria, axe-core, timing, domain history), migration 0007, audit service/API/`crawl`
  command, WQS, recommendations, benchmark, saved searches (server), calibration tooling.
- Built and tested but awaiting commit approval at handoff time: the audit report and saved-search interface, the
  Windows lock with Playwright, the combined Windows procedure — check `git status` and DELIVERY_STATUS.md.
- Not done: human WQS labels (60–100 real sites), Census revenue prior, WQS as BOS/LPS input or list filter,
  `mailto:` outreach, M3 TEST ZIP, every Windows check.

## Testing

- PostgreSQL dev cluster (start if stopped):
  `su postgres -s /bin/bash -c "/usr/lib/postgresql/16/bin/pg_ctl -D /var/lib/postgresql/leadintel-dev/data -o '-p 54329 -c listen_addresses=127.0.0.1 -k /tmp' -l /var/lib/postgresql/leadintel-dev/log.txt -w start"`
- Full suite (only the `_test` database; the fixtures refuse anything else):
  `cd backend && env -u LEADINTEL_DATABASE_URL -u LEADINTEL_CRM_DATABASE_URL -u LEADINTEL_DATA_DIR LEADINTEL_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:54329/leadintel_test .venv/bin/python -m pytest -q -p no:cacheprovider`
  (about 8 minutes; Playwright 1.63.0 and its Chromium are installed in the dev venv).
- Latest result: in DELIVERY_STATUS.md.

## Approval boundaries (ask first)

Push or merge; any real database change or migration (the dev `leadintel` database and the Windows Friend Edition
database are off-limits — tests use `leadintel_test` only); installing on the real Friend Edition; destructive data
operations; paid providers or recurring costs; OAuth; public deployment; weakening security; building any ZIP
(TEST or release); crawling real prospect websites in bulk. Commits: signed (`-S`), author
Levi Daniel (the email as given by the user, not in this file), explicit file staging, and **show the staged list, diff summary, checks and
message, then wait for approval** before committing. No model names in repository content.

## Next milestone

1. After approval: build the M3 TEST ZIP and run the Windows session (`docs/windows/M1_M3_TEST_PROCEDURE.md`); fix
   what it finds.
2. WQS calibration with real human labels (export → rate → report); keep "provisional" until the targets are met.
3. Then, as versioned and explainable changes: website evidence into BOS/LPS, a WQS list filter, the Census
   revenue prior — before starting M4 (see ROADMAP.md).
