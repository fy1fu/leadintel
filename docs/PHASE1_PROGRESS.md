# Phase 1 progress report: real-data run, Duval / St. Johns / Clay

**Date:** 2026-11-03.
**Environment:** build sandbox, PostgreSQL 16 + PostGIS 3.4.
**Run:** `leadintel discover --since 2026-10-04` (30 days of Sunbiz filings).

All numbers below come from real sources. Nothing was mocked.

## 1. What the pipeline produced (run 5)

| Item | Result |
|---|---|
| Coverage | Overture **complete** · Sunbiz **complete** · DBPR **complete** (evidence only), for all three counties. Run status `completed`. |
| Overture | Release 2026-10-21.0. Snapshot of 96,024 places (62 MB read once, then reused). **92,486** in target counties: Duval 65,674 · St. Johns 17,289 · Clay 9,523. 3,576 closed (kept with status). |
| Sunbiz | 105 daily files (corporate + fictitious name + both event files) imported once, then reused on later runs. **64,690 organizations** statewide: 47,047 LLC · 9,875 fictitious name · 3,942 corporation · 1,757 foreign LLC · 1,301 non-profit · … |
| Date ledger (filings) | 56 verified · 2 weekend · 4 not yet published · **0 unexplained gaps**. A transient 401 on one event file was retried successfully on a later run. |
| Quarantine | 1 real record (document number `11`), kept with its reason (V14) |
| Address roles | 30,657 principal addresses classified **not operating**: registered-agent address, mass-filer address or PO box |
| DBA → owner links | 7,310 recorded. 718 resolved so far; the rest have owners outside the 30-day window (they resolve with the quarterly import, V3). |
| Registry placement | Filings in target ZIPs geocoded (Census). 35 linked to existing listings (e.g. "HAZE VAPE & SMOKE SHOP" → "Haze Vape And Smoke Shop"). **1,582 registry-only locations.** Unplaceable or out-of-area filings are counted, not guessed. |
| DBPR | Construction: 5,274 active in-target licences → 826 linked, 40 ambiguous skipped. Cosmetology: 16,426 → 607 linked. County/ZIP disagreements counted (V13). Licences never create prospects. **1,214 locations** carry licence evidence. *Superseded by run 9 under the corrected validity and geography rules: see §5.* |
| De-duplication | 346 automatic merges (same phone + near-identical name), all reversible. **444 in the review queue.** 12,031 co-located pairs (shared phone, different business) correctly left alone. |
| Locations | **93,722** active prospects-to-be (listings + registry) |
| Data quality | 512,796 current facts, **all `ingested`, 0 verified**. Import counts are reported separately from verification (no verification step exists before Phase 2). |
| Qualification (provisional) | 8,847 provisionally qualified · 44,043 needs research · 40,832 not qualified |
| Paid requests | **0** |

### Niche comparison: discovery-level, no winner chosen (`leadintel report`)

| Niche | County | Discovered | No independent website listed | Social-only listed | Provisionally qualified | Needs research | Avg LPS (candidates) | With phone | With email |
|---|---|---|---|---|---|---|---|---|---|
| Cleaning | Duval | 589 | 178 | 72 | 77 | 260 | 45.5 | 577 | 221 |
| Cleaning | St. Johns | 150 | 34 | 19 | 12 | 74 | 46.3 | 150 | 65 |
| Cleaning | Clay | 70 | 22 | 9 | 10 | 30 | 45.9 | 64 | 25 |
| Landscaping | Duval | 431 | 109 | 37 | 44 | 195 | 45.5 | 412 | 155 |
| Landscaping | St. Johns | 162 | 43 | 25 | 22 | 80 | 46.9 | 163 | 71 |
| Landscaping | Clay | 120 | 47 | 30 | 24 | 44 | 48.2 | 116 | 55 |

How to read these numbers:
- Presence counts come from listing data. Website discovery (Phase 2) must confirm "no website" before it is treated as fact.
- Outdated websites are "not measured" until Phase 2.
- All scores are `provisional`.

## 2. Problems found by running on real data, and fixed

| Problem | Fix |
|---|---|
| Duplicate detection merged *different* people (two insurance agents sharing an office phone) and queued 11,800 co-located pairs | Auto-merge only same phone + near-identical names (≥ 0.8); partial similarity → review; dissimilar → co-location, left alone. All 621 earlier merges were reversed with `unmerge` (3.8 s, no data lost). |
| Industry classification put 8,040 Duval businesses into "landscaping" (any category containing the word "service") | Whole taxonomy terms required; regression test added |
| One Sunbiz file failed with a transient 401 | One retry; event-file gaps now also mark coverage partial |
| A crashed run stayed "running"; an interrupted run was indistinguishable | Advisory lock (one run at a time); crashed → `failed`, killed → `interrupted` |
| Coverage rows lost when a later source failed | Each coverage write commits on its own |
| Slow queries (7 min de-duplication; per-row feature scan) | Index-assisted spatial pre-filter; set-based feature query (6 s for 94k locations); ZIP index |
| Mobile layout: the map covered the list | Fixed and verified in a browser at 390 px |

## 3. Acceptance criteria (ROADMAP §2 Phase 1)

| Criterion | Status |
|---|---|
| Imports reproduce every source line from the database | ✅ Raw line, file and line number are stored for Sunbiz; payload and hash for Overture and DBPR |
| A rerun creates 0 duplicates | ✅ Runs 2–5 reused every file and snapshot; re-imports created no new rows; unchanged scores were not rewritten |
| A DBA owned by an LLC is one business with both filings | ✅ Tested (including when the owner arrives later). Live: 718 resolved links. |
| Registered-agent addresses never appear as operating locations | ✅ Tested; 30,657 classified live |
| Missing file day → coverage `partial`, never "no new businesses" | ✅ Tested (ledger + pipeline). Live run had no unexplained gaps. |
| Malformed lines land in quarantine | ✅ Tested (Sunbiz; DBPR since §5: malformed dates, unknown statuses, wrong column counts); 1 live case (Sunbiz). The real DBPR files contain none. |
| Import counts are never presented as verified counts | ✅ Reports show `verified_facts: 0` with an explicit note |
| Network diagnostics run with the business DB stopped | ✅ Tested (`--no-store`, unreachable-DB test, API 503 test) |
| Stacked-signal cap test passes | ✅ Property-based (300 cases) |
| 0 paid calls | ✅ Every run reports `paid_requests: 0` |

## 4. Still open in Phase 1

- **F7 job queue.** Runs are synchronous via the CLI with an advisory lock. Procrastinate for API-triggered runs is pending.
- **API/UI actions.** Unmerge, review-queue decisions and config restore exist as functions, not yet as endpoints or screens.
- **Quarterly Sunbiz import (V3).** Back-fills older owners, so DBA links resolve, and reconciles gaps.
- **Windows run (V12).**
- **St. Johns / Clay BTR investigation** (non-blocking; manual CSV import is ready).
- **V13: DBPR layout and county-code map.** Still inferred, not officially verified ([DBPR_GEOGRAPHY.md](DBPR_GEOGRAPHY.md)).

## 5. PR #3 review fixes and re-validation (2026-11-03)

Four fixes, one commit each. Everything below was measured; nothing is inferred from the code.

### 5.1 Fixes

| # | Problem | Fix | Tests added |
|---|---|---|---|
| 1 | **Sunbiz same-size stale cache.** A file republished with the same size but different content was treated as unchanged. | A file's identity is now its size plus its Last-Modified. A changed Last-Modified triggers a download. Every version is kept at a content-addressed path (`<name>.<sha12>.<ext>`) with a metadata sidecar. Earlier `source_file` rows and checksums are never overwritten (unique key now includes Last-Modified, migration 0004). Partial downloads resume only for the same remote version and are never imported. A failed refresh leaves the previous file and metadata intact. | `test_download_versions.py` (13): unchanged → no download; same size + newer timestamp → download; size change → download; interrupted download → resume, no partial import; failed refresh → previous version intact; corrupted cache; refresh rules; every version kept in the database |
| 2 | **DBPR malformed expiry dates.** Blank and malformed dates were treated alike. | Dates are classified blank, valid or malformed (impossible dates such as 02/30 count as malformed). Malformed dates, unknown status codes and wrong column counts are quarantined with the raw line and reason. Only status `A`, a valid non-past expiry and a licence number count as evidence; a blank expiry does not (no published DBPR rule says it means active). Evidence that is no longer accepted is superseded (history kept) and its link deactivated. | `test_dbpr_validation.py` (11): blank, valid future, expired, malformed, impossible date, missing and invalid status, no licence number, wrong column count; stale evidence withdrawn and scores fall; re-import stable; withdrawn evidence returns if valid again |
| 3 | **DBPR geography.** Only three county codes were mapped, so a non-target code with a target ZIP passed. | `resolve_geo`: county code vs ZIP (ZCTA). A conflict, an ambiguous ZIP or an out-of-state row gets **no county** and can never reach a target county. Code-only and ZIP-only results are flagged (inferred). Matching is restricted to the resolved county. Raw values, status, flags and reason are stored. The full 67-code map stays **V13 / inferred**. Audit in [DBPR_GEOGRAPHY.md](DBPR_GEOGRAPHY.md). `source_file.parser_version` (migration 0005) re-imports files processed under older rules once. | `test_dbpr_geo.py` (30): agree; disagree; non-target code + target ZIP; ambiguous ZIP; unknown, blank and out-of-state codes; missing ZIP; ZIP outside the Florida ZCTAs; incomplete address; invariants over every code × ZIP × state combination; stored audit fields; county-restricted matching |
| 4 | **Merge history.** Rows kept on the merged location were not recorded, and a shared source link was duplicated on the survivor. | Kept rows are recorded with the survivor row they duplicate (`moved["_kept"]`). Source links use `(source_key, external_id)` as their identity. Unmerge records what it restored and still reverts older events. Matching thresholds unchanged. | `test_merge_partial.py` (3): two locations sharing some but not all contacts, profiles, links, aliases, facts and addresses, plus a third, uninvolved location; moved vs kept rows; no duplicates; exact restoration; repeated merge, unmerge and re-merge; old-format events |

### 5.2 Results

Environment: build sandbox (Linux), Python 3.13.18, PostgreSQL 16.15 + PostGIS 3.4.2, Chromium (Playwright). Windows has not been run (V12).

| Check | Result |
|---|---|
| Full suite on PostgreSQL/PostGIS | **228 passed, 5 skipped**. The 5 skips are the opt-in live-network tests, run separately below. |
| Stacked-signal cap property test | **300 passing, 0 failing** (seed 0, and again with a random seed). Hypothesis rejected 78 and 86 inputs via `assume()`; these are not counted. |
| Live-network tests (`LEADINTEL_LIVE_TESTS=1`) | **5 passed**: three Overture mirrors, live failover, live diagnostics. Sunbiz and DBPR were exercised live by runs 8–9. |
| Browser, desktop 1440 px and mobile 390 px | **Passed.** 1,000 list items, profile opens, no page or console errors, no HTTP errors, no horizontal overflow. |
| Migration of the existing database (0003 → 0005) | **1.2 s; every row count identical** before and after (20 counts across 15 tables compared). Backup taken first. All 116 existing files read `parser_version = NULL` and were re-imported once where the rules changed (DBPR). |
| Interrupted runs | Run 6 killed (SIGKILL) during Overture, run 7 early in the run, run 8 **during DBPR matching**. Each was marked `interrupted` at the next start. No partial DBPR import persisted (file rows and current licence facts unchanged) and no DBPR coverage was recorded for run 8. Stages completed before the kill (Overture, Sunbiz) kept their coverage. |
| Recovery run 9 | **Completed** in 12 m 50 s. Coverage complete for 3 sources × 3 counties. Sunbiz reused all 107 files (no Last-Modified changes, so nothing was downloaded again). |
| Duplicates after the kills and run 9 | **0** on all natural keys: source records, file versions, organisations, facts, current licence facts per location, source links, current scores, current qualification, quarantine lines, coverage, review pairs. |
| Paid requests | **0** in every run; no paid-provider code, `config/` or network-layer changes in the fixes. |
| Secret scan (21 commits on the branch) | No key, token or private-key patterns. The Sunbiz password value appears **0** times. The public username is a common word that appears only as a path segment, in licence labels and in test fixtures. No data, database or `.env` files committed. |

### 5.3 Real data after the fixes (run 9)

| Item | Run 5 (old rules) | Run 9 (corrected rules) |
|---|---|---|
| DBPR construction: in target / active / linked | 14,389 / 5,274 / 826 | 13,945 / 5,111 / 823 |
| DBPR cosmetology: in target / active / linked | 26,844 / 16,426 / 607 | 26,092 / 15,931 / 603 |
| County/ZIP conflicts excluded (construction + cosmetology) | counted, **not** excluded | 114 + 308 = **422** (matches the audit) |
| DBPR rows quarantined | not checked | 0 (the real files contain no malformed dates or statuses) |
| Licence evidence superseded | n/a | 7 (history kept) |
| Locations with licence evidence | 1,214 | 1,208 |
| Locations | 93,722 | 93,732 (10 new Sunbiz filings) |
| Qualification (provisional) | 8,847 / 44,043 / 40,832 | 8,847 / 44,042 / 40,843 |

The 1,253 locations whose inputs changed were re-scored. All other scores were unchanged and were not rewritten.

### 5.4 Limitations still open

- **V13.** The DBPR column meanings and county-code map are inferred from the data and checked against Census ZCTAs. **They are not officially verified.**
  - County-code-only placements are labelled inferred: 597 construction and 224 cosmetology rows in the target counties.
  - The conservative policy excludes about 1.3% of otherwise-active target licences, some of which may be genuine border cases.
- **V14.** The one Sunbiz record with document number `11` is still quarantined, not guessed.
- **Existing merges.** The 967 merge events written before Fix 4 have no `_kept` record. They still unmerge exactly, but kept rows were not logged for them. The duplicate check found no duplicated source links from them.
- **One-time history rows.**
  - Files registered before Fix 1 without a Last-Modified get one extra `source_file` row with the same checksum when first seen again; nothing is overwritten.
  - DBPR records gained one new payload version each (21,042 rows) when the geo decision was added.
- **V12.** Windows has not been run.
