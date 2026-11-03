# Open verification items (before Phase 1)

Status as of 2026-11-03 (updated during Phase 1). Items are closed only by a real request or a real data check, never by assumption.

**Completeness** items can make LeadIntel miss businesses or evidence; results are labelled `partial` in coverage. **Correctness** items can make stored values wrong; they are guarded by validation and tests until closed.

| # | Item | Current evidence | Affects | Blocking? | How to close |
|---|---|---|---|---|---|
| V1 | Official positions of the corporate fields not shown in the definition excerpts | Reconstructed layout parses 6,352 real records consistently; officer block starts at field 37 as officially stated | **Correctness** (field values for unconfirmed positions) | No (parser will validate every record) | Compare [SUNBIZ.md §4](SUNBIZ.md) with https://dos.sunbiz.org/data-definitions/cor.html |
| V2 | Sunbiz SFTP on port 22 | Sandbox: tunnel opened, connection reset before the SSH banner. HTTPS/WebDAV (primary) works. | Neither (fallback path only) | No | Windows procedure in [SUNBIZ.md §7](SUNBIZ.md) |
| V3 | Sunbiz quarterly ZIP contents (member layouts, active-only vs all records, principal-state population) | ZIP directories intact; contents not downloaded | **Completeness** (back-fill of gap dates) and **correctness** of quarterly-sourced fields | Partly: affects back-fill design | First quarterly import on your machine |
| V4 | Geofabrik extracts (required for OSM) | Sandbox: main host resets during TLS; `download-ext2` returns proxy 502 | **Completeness** (OSM as a source) | Yes for OSM; Overture covers primary discovery | `leadintel net-check --only geofabrik_main,geofabrik_ext2` on Windows |
| V5 | Overpass instances | Reset or timeout from the sandbox | Completeness (optional fallback) | No (optional fallback) | Same, on Windows |
| V6 | Common Crawl index, Wayback, RDAP (`data.iana.org`, `rdap.verisign.com`) | Reset, or refused by the sandbox proxy | Completeness (optional website-history signals) | No (optional website-history signals) | Same, on Windows |
| V7 | Duval BTR report export | Tax Collector page works; report portal shows a Cloudflare bot challenge (never bypassed) | **Completeness** (BTR evidence in Duval) | No | Manual export or public-records request; decide per county |
| V8 | NPI bulk files | Page reports "down for maintenance"; API works | Completeness (NPI bulk; the API works) | No | Re-run diagnostics later |
| V9 | Sunbiz HTTPS rate limits for bulk downloads | Only small requests made so far | Completeness (bulk download throughput) | No | Observe during the first quarterly download (resumable) |
| V10 | Clay and St. Johns County BTR data access | Not yet checked; Duval's report portal is behind a bot challenge | **Completeness** (BTR evidence in St. Johns / Clay) | **No** (decided: generic manual-CSV import covers any county) | Check each county Tax Collector's published reports; public-records request where needed |
| V11 | Review-count evidence from free sources | Overture has no review fields (verified). No permitted free bulk source identified. | **Completeness** (review evidence only by manual observation) | No (manual entry planned) | Manual research fields, or Google Places after the [ROADMAP.md §4](ROADMAP.md) checklist |

| V12 | Windows run of the PR #1 test suite and live diagnostics | Not yet run; Windows-specific socket handling added, not exercised | **Correctness** (platform-specific behaviour on Windows) | Before merging PR #1 | Procedure in PR #1 description |
| V13 | DBPR licensee CSV column meanings (no published layout found); county-code map (all 67 codes, alphabetical from 11: Clay 20, Duval 26, St. Johns 65) | **Open: inferred, not officially verified.** Consistency check only: 99.21% of 626,509 rows agree with the Census ZIP->county relationship; the 0.79% disagreements are investigated in [DBPR_GEOGRAPHY.md](DBPR_GEOGRAPHY.md). On import, county/ZIP conflicts are excluded and never placed in a target county; code-only and ZIP-only assignments are flagged; every decision is stored with its raw values and reason (`code_map: inferred_v13`) | **Correctness** (licence evidence attribution) | No (evidence only; never creates prospects) | Find the official DBPR extract layout and code table, or ask DBPR |
| V14 | One real Sunbiz corporate record with document number `11` (file 20261027c.txt, line 220) | **Open.** Quarantined, not guessed; still the only quarantine record after run 9 (2026-11-03) | Correctness (single record) | No | Re-check after the next quarterly file |

## Validation of the PR #3 review fixes (2026-11-03)

Environment: build sandbox (Linux), Python 3.13.18, PostgreSQL 16.15 + PostGIS 3.4.2, Chromium. **Windows not run (V12).** Full results, the real-data comparison and limitations are in [PHASE1_PROGRESS.md §5](PHASE1_PROGRESS.md).

| Check | Result |
|---|---|
| Full PostgreSQL suite | 228 passed, 5 skipped (the opt-in live tests) |
| Cap property test | 300 passing, 0 failing |
| Live-network tests | 5 passed |
| Browser (desktop + mobile) | Passed, no errors |
| Migration 0003 → 0005 on the real database | All row counts identical |
| Killed runs (Overture, early, DBPR stage) | Marked `interrupted`; no partial import, no duplicates, no false coverage; recovery run completed |
| Paid requests | 0 |
| Secret scan of the branch | Clean |

V13 and V14 remain **open**. The DBPR county-code map is consistent with Census ZIP data but is **not officially verified**.

The v0.3 requirements are consolidated in [ROADMAP.md](ROADMAP.md); the 2026-11-03 architecture review is in [REVIEW.md](REVIEW.md).
