# LeadIntel — development handoff (start here)

State at handoff: 2026-11-06. Authoritative detail: [DELIVERY_STATUS.md](DELIVERY_STATUS.md). Design: [PLAN.md](PLAN.md),
[ROADMAP.md](ROADMAP.md), [SCORING.md](SCORING.md), [research/M3_PREP.md](research/M3_PREP.md),
[security/M2_CRM_WRITE_PATH.md](security/M2_CRM_WRITE_PATH.md). Do not re-plan what is already decided there.

## Product

Local-first prospecting tool for a small web-design business in north-east Florida (Duval, Clay, St. Johns). It
finds local businesses from public data, scores them as website customers (BOS, LPS), audits their existing websites
(WQS), and tracks the sales work (local CRM). Runs on one Windows PC ("Friend Edition": per-user install, local
PostgreSQL + PostGIS, Edge app window). No cloud; no paid services without approval.

## Repository state

- `/home/user/leadintel`, branch `phase-1`, HEAD `bebe72f`; working tree clean except this handoff update (see
  `git status`). 30 local commits are **not pushed**: GitHub `origin/phase-1` is at `b20f99a`. Nothing merged.
- Backend `backend/`: FastAPI, SQLAlchemy 2, psycopg3, Alembic head **0007**, static JS frontend
  (`backend/app/api/static`, MapLibre 4.7.1). CLI `backend/app/cli.py` (Windows: `packaging/windows/run_cli.py`).
- Windows packaging `packaging/windows/`: Setup / launch / Load Data scripts, `build_friend_zip.py`, hashed lock.
- Last package: `dist/LeadIntel-1.3.0-m3-TEST.zip` (TEST build from `bebe72f`, 497,076 bytes, SHA-256
  `7095416ab10cfeb89167bc683adeddb2a170124a7d4210d36adfa79e934cfca6`; `dist/` is not tracked).

## Completed

Phase 1 data pipeline; B0 maintenance lock; M1 salesperson interface; M2 local CRM (with the restricted sales login);
M3: Search Area and center search, browser egress proxy, crawl runner, website checks (26 criteria, axe-core 4.14.0,
browser timing, domain history), migration 0007 (audits, pages, artifacts, findings, recommendations, benchmarks,
saved searches), audit service + API + `crawl` command, WQS `wqs-1.0` (provisional), recommendations, peer
benchmark, audit report and saved-search interface, calibration tooling.

Verification: full suite on `leadintel_test` at `bebe72f`: **1055 passed, 6 skipped, 0 failed**. Windows: the
combined M1 → M3 session (`docs/windows/M1_M3_TEST_PROCEDURE.md`) on a separate test account with the TEST ZIP above
was **reported successful by the user** (summary confirmation; per-step results, counts and Setup output were not
recorded). The real Friend Edition has not been upgraded.

## Rules to keep

- Every business write and managed-file write under `maintenance_lock` (fail-closed commit gate);
  `tests/test_write_coverage.py` must classify every new command and write site.
- CRM writes: lock by default; the `leadintel_crm` login is restricted by PostgreSQL to CRM tables. Do not widen it.
- One shared `ProspectFilter` for list, counts, map, export and saved searches; radius canonical in metres.
- Crawling: user-initiated, one audit at a time, robots.txt, ≤ 15 pages, 1 s per host, only through
  `BrowserEgressProxy`, GET/HEAD only, watchdog, never fill or submit forms, never frame the live site.
- Unknown is never poor; WQS stays "provisional" until calibrated; BOS/LPS change only by versioned, explained steps.

## Known limitations / needs attention

- WQS not calibrated (needs 60–100 human-rated real sites; target Spearman ≥ 0.7). Visual design is never assessed.
- BOS/LPS do not use website evidence yet; no WQS list filter; Census revenue prior and `mailto:` outreach not built.
- One timing-sensitive CRM test failed once under load (then passed repeatedly); watch it.
- Security notes R1–R3 and the licensing review before wider distribution: see DELIVERY_STATUS.md.
- The watchdog depends on a Playwright internal; a test fails if a Playwright upgrade moves it.

## Testing

- Start PostgreSQL if stopped:
  `su postgres -s /bin/bash -c "/usr/lib/postgresql/16/bin/pg_ctl -D /var/lib/postgresql/leadintel-dev/data -o '-p 54329 -c listen_addresses=127.0.0.1 -k /tmp' -l /var/lib/postgresql/leadintel-dev/log.txt -w start"`
- Full suite (about 6–8 min; fixtures refuse any database not ending in `_test`):
  `cd backend && env -u LEADINTEL_DATABASE_URL -u LEADINTEL_CRM_DATABASE_URL -u LEADINTEL_DATA_DIR LEADINTEL_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:54329/leadintel_test .venv/bin/python -m pytest -q -p no:cacheprovider`

## Approval required (ask first)

Push / merge; any change or migration of a real database (the dev `leadintel` database and the Windows Friend
Edition database; tests use `leadintel_test` only); installing on the real Friend Edition; building any ZIP;
destructive data operations; paid providers or recurring costs; OAuth; public deployment; weakening security; crawling
real prospect websites in bulk. Commits: signed, author Levi Daniel, explicit file staging; show the staged files,
diff summary, `git diff --cached --check` and the message, then **wait for approval**. No model names in repository
content.

## Next steps

1. Decide whether to push the 30 local commits to GitHub (approval).
2. Decide whether and when to upgrade the real Friend Edition to M3 (applies migrations 0006 and 0007; take the
   automatic backup; approval).
3. WQS calibration: `leadintel wqs-calibration export`, rate 60–100 real sites (about 20 twice), `report`; keep
   "provisional" until the targets are met.
4. Then, as versioned and explained changes: website evidence into BOS/LPS, a WQS list filter, the Census revenue
   prior. M4 (competitor analysis, campaigns via `mailto:`) only after that — see ROADMAP.md.
