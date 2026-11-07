# LeadIntel — project handoff (start here)

Handoff date: 2026-11-06. This file is the starting point for any new development session. Detail lives in
[DELIVERY_STATUS.md](DELIVERY_STATUS.md) (tests, security notes, approvals), [PLAN.md](PLAN.md), [ROADMAP.md](ROADMAP.md),
[SCORING.md](SCORING.md), [research/M3_PREP.md](research/M3_PREP.md) and [security/M2_CRM_WRITE_PATH.md](security/M2_CRM_WRITE_PATH.md).
Those decisions are settled; do not re-plan them.

## 1. What LeadIntel is

A local-first prospecting tool for a small web-design business in north-east Florida (Duval, Clay, St. Johns
counties). It:
- loads public business data (Overture places, Sunbiz filings, DBPR licences, county tax receipts, Census geography);
- scores each business as a website customer: BOS (business opportunity) and LPS (lead priority), both provisional;
- audits a business's existing website on request and scores it (WQS, website quality), with evidence,
  recommendations and a peer comparison;
- tracks the sales work in a local CRM (claims, pipeline status, follow-ups, notes, lists, saved searches).

It runs on one Windows PC as the "Friend Edition": per-user install under `%LOCALAPPDATA%\LeadIntel`, a local
PostgreSQL 16 + PostGIS database, the app in an Edge app-mode window. No cloud services; free data sources only.

## 2. Repository state (at handoff)

- Repository `/home/user/leadintel`, branch **`phase-1`**, HEAD **`71f0d62`** ("Docs: M3 complete; Windows TEST session
  reported successful; handoff"). The working tree was clean at `71f0d62`; this final handoff edit may still be
  uncommitted — check `git status`.
- **31 commits are not pushed**: GitHub `origin/phase-1` is at `b20f99a`. Nothing has been merged or released.
- Last package: `dist/LeadIntel-1.3.0-m3-TEST.zip` — TEST build from `bebe72f` (code identical to HEAD; later commits
  are documentation only), 497,076 bytes, 122 files, SHA-256
  `7095416ab10cfeb89167bc683adeddb2a170124a7d4210d36adfa79e934cfca6`. `dist/` is not tracked by git.

## 3. Architecture

- `backend/`: Python 3.13, FastAPI, SQLAlchemy 2, psycopg3, Alembic (single head **0007**), PostgreSQL 16 + PostGIS.
  - `app/sources`, `app/pipeline`: data loading ("Load Data" = `discover`); `app/scoring`: BOS/LPS, WQS, calibration;
    `app/crm`: CRM; `app/crawl`: crawler and website checks; `app/audit`: audit service, recommendations,
    benchmark; `app/net`: SSRF-safe fetcher and browser egress proxy; `app/api`: HTTP API + static frontend
    (`app/api/static`, plain JS, MapLibre 4.7.1); `app/cli.py`: command line.
- `packaging/windows/`: Setup / launch / Load Data / stop PowerShell scripts, `build_friend_zip.py`, and the hashed
  `requirements-win-py313.lock` (includes Playwright for audits, which drive the installed Microsoft Edge).
- Local web app hardening: Host check, per-launch token and same-origin rule for every write, CSP with nonces,
  server-vetted links only.

## 4. Completed functionality

- **Phase 1 + B0**: data pipeline with provenance; maintenance lock with a fail-closed commit gate.
- **M1 — salesperson interface**: filter panel, paging, honest counts, map, business detail, CSV export, themes,
  Edge app window and full screen.
- **M2 — local CRM**: profile, claim / release, pipeline status, next action, follow-up dates, notes and contact
  history, lists, sales overview, sales filters; writes protected by the lock or by a separate `leadintel_crm`
  login that PostgreSQL restricts to the CRM tables (migration 0006).
- **M3 — Search Area and website intelligence** (migration 0007):
  - Search Area: radius filter around a center (search by business, ZIP, city or address; map pick; marker drag),
    server-side and shared by list, counts, map and export;
  - website audits from the business view: user-initiated, one at a time, up to 15 pages, robots.txt, 1 s per host,
    all browser traffic through the egress proxy, GET/HEAD only, watchdog against hung pages, screenshots;
  - 26 checks over 7 dimensions (incl. axe-core accessibility, browser timing, domain history); unknown is never
    scored as poor;
  - WQS `wqs-1.0` (provisional) with confidence and plain-language explanations, stored with the existing scores;
  - 3–5 recommendations from actual findings; peer benchmark (same niche, county then target region, ≥ 8 peers);
  - audit report (seven areas, grouped findings, screenshots, history, comparison); live site only in the external
    browser;
  - saved searches (all filters, area in metres, unit, sort, layout); `crawl` command; WQS calibration tooling.

## 5. Verification

- Automated: full suite on the `leadintel_test` database at the final code (`bebe72f`): **1055 passed, 6 skipped,
  0 failed**. Includes 33 synthetic fixture websites, audits on PostgreSQL, migration up/down, security and
  concurrency tests. Headless interface walkthroughs passed at three screen sizes.
- Windows: the combined M1 → M3 session (`docs/windows/M1_M3_TEST_PROCEDURE.md`) was run with the TEST ZIP above on
  a separate Windows test account. **The user was satisfied: everything worked.** That confirmation is the accepted
  result; individual step results were not recorded and do not need to be reproduced.
- The real Friend Edition has **not** been upgraded to M2/M3 yet.

## 6. Limitations and outstanding work

- WQS is **provisional**: weights not yet checked against human ratings of 60–100 real websites (target Spearman
  ρ ≥ 0.7, band agreement ≥ 80 %). The app labels every WQS as provisional. Visual design is never assessed.
- BOS / LPS do not use website evidence yet; there is no WQS list filter.
- Not built: Census revenue prior, `mailto:` outreach, competitor analysis and campaigns (M4–M6).
- Watch: one timing-sensitive CRM test failed once under heavy load (passed repeatedly since).
- The audit watchdog relies on a Playwright internal (pinned 1.63.0); a test fails if an upgrade moves it.
- Before distribution beyond friends: licensing review (PostGIS GPL, VC++ runtime; axe-core MPL-2.0 and Playwright
  Apache-2.0 notices are included). Security notes R1–R3: DELIVERY_STATUS.md.

## 7. Technical and safety decisions to keep

- Every business write and managed-file write runs under `maintenance_lock`; `tests/test_write_coverage.py` must
  classify each new command and write site.
- CRM writes: lock by default; the restricted `leadintel_crm` login only for CRM tables. Never widen it.
- One shared `ProspectFilter` for list, counts, map, export and saved searches; radius canonical in metres
  (400 m – 160,934.4 m).
- Crawling stays user-initiated and controlled (see §4); never fill or submit forms; never frame the live site.
  Development crawls only against synthetic local sites.
- Unknown is never poor. WQS stays "provisional" until calibration meets the targets. BOS/LPS change only through
  versioned, explained steps.
- Decisions on record: Census API key approved (free, outside source control); SAM.gov not approved; Lighthouse
  deferred; email only via `mailto:` (no OAuth); B0.5 fencing token deferred to the next free migration number.

## 8. Working rules and approval boundaries

- Tests only against `leadintel_test` with synthetic data. Never connect to the dev `leadintel` database or the
  Windows Friend Edition database.
- Ask first for: push or merge; any real database change or migration; installing on the real Friend Edition;
  building any ZIP (TEST or release); destructive data operations; paid services; OAuth; public deployment; weakening
  security; crawling real prospect websites in bulk.
- Commits: signed, author Levi Daniel, explicit file staging; show the staged files, diff summary,
  `git diff --cached --check` and the message, then wait for approval. No model names in repository content.
- Test commands:
  - start PostgreSQL if stopped: `su postgres -s /bin/bash -c "/usr/lib/postgresql/16/bin/pg_ctl -D /var/lib/postgresql/leadintel-dev/data -o '-p 54329 -c listen_addresses=127.0.0.1 -k /tmp' -l /var/lib/postgresql/leadintel-dev/log.txt -w start"`
  - full suite (6–8 min): `cd backend && env -u LEADINTEL_DATABASE_URL -u LEADINTEL_CRM_DATABASE_URL -u LEADINTEL_DATA_DIR LEADINTEL_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:54329/leadintel_test .venv/bin/python -m pytest -q -p no:cacheprovider`

## 9. Next steps

1. Decide on pushing the 31 local commits to GitHub (approval).
2. Upgrade the real Friend Edition to M3 when ready (applies migrations 0006 and 0007; Setup takes a backup first;
   approval).
3. WQS calibration: `wqs-calibration export`, rate 60–100 real websites (about 20 twice), `wqs-calibration report`;
   adjust weights only through a new WQS version; drop "provisional" only when the targets are met.
4. Then, as versioned and explained changes: website evidence into BOS/LPS, a WQS list filter, the Census revenue
   prior. Start M4 (see ROADMAP.md) only after that.

## 10. Next-session prompt (copy and paste into a fresh session)

```
You are continuing development of LeadIntel, a local-first prospecting tool for a small web-design business
in north-east Florida. It loads public business data, scores businesses as website customers (BOS, LPS),
audits their existing websites (WQS) and tracks sales work in a local CRM. It runs on one Windows PC
(the "Friend Edition": per-user install, local PostgreSQL + PostGIS, Edge app window).

Repository: /home/user/leadintel, branch phase-1.

First:
1. Read docs/HANDOFF.md completely. It is the definitive handoff. Use docs/DELIVERY_STATUS.md for detail.
2. Inspect the actual repository state: `git status`, `git log --oneline -5`,
   `git log --oneline origin/phase-1..HEAD`. At handoff, HEAD was 71f0d62 with 31 unpushed commits.
3. Continue from that actual state. Do not redo completed work. M1 (interface), M2 (local CRM) and M3
   (Search Area, secure website audits, WQS wqs-1.0, recommendations, peer benchmark, audit report, saved
   searches, migration 0007) are complete. The full suite passed (1055 passed, 6 skipped). The Windows
   M1->M3 test session was successful and the user was satisfied; do not ask to repeat it or for its
   individual results. Do not re-plan decisions recorded in PLAN.md, ROADMAP.md, SCORING.md or
   research/M3_PREP.md.

Keep these rules:
- Every business or managed-file write runs under the maintenance lock.
- CRM writes use the lock or the restricted leadintel_crm login. Never widen that login.
- One shared ProspectFilter serves list, map, counts, export and saved searches.
- Crawling stays user-initiated, one audit at a time, through the egress proxy, GET-only, robots.txt.
- Unknown is never scored as poor. WQS stays provisional until calibrated. BOS/LPS change only through
  versioned, explained steps.

Safety:
- Test only against leadintel_test with synthetic data. Never touch the dev `leadintel` database or
  the Windows Friend Edition database.
- Ask me first before: pushing or merging, any real database change or migration, installing on the
  real Friend Edition, building any ZIP, destructive operations, paid services, OAuth, public
  deployment, weakening security, or crawling real prospect websites in bulk.
- Commits are signed, author Levi Daniel, with explicit file staging. Before each commit, show the
  staged files, diff summary, `git diff --cached --check` and the message, then wait for my approval.
- No model names in repository content. Keep reports concise.

Next steps, from docs/HANDOFF.md section 9 (ask me before each approval item):
1. Whether to push the 31 commits.
2. When to upgrade the real Friend Edition to M3.
3. WQS calibration with 60-100 human-rated real websites.
4. Then versioned website evidence into BOS/LPS, a WQS list filter and the Census revenue prior,
   before any M4 work.
```
