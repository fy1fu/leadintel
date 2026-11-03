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
| DBPR | Construction: 5,274 active in-target licences → 826 linked, 40 ambiguous skipped. Cosmetology: 16,426 → 607 linked. County/ZIP disagreements counted (V13). Licences never create prospects. **1,214 locations** carry licence evidence. |
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
| Malformed lines land in quarantine | ✅ Tested; 1 live case |
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
