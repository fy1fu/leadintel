# Open verification items (before Phase 1)

Status as of 2026-11-03 (updated during Phase 1). Items are closed only by a real request or a real data check, never by assumption.

**Completeness** items can make LeadIntel miss businesses or evidence; results are labelled `partial` in coverage. **Correctness** items can make stored values wrong; they are guarded by validation and tests until closed.

| # | Item | Current evidence | Affects | Blocking? | How to close |
|---|---|---|---|---|---|
| V1 | Official positions of the corporate fields not shown in the definition excerpts | Reconstructed layout parses 6,352 real records consistently; officer block starts at field 37 as officially stated | **Correctness** (field values for unconfirmed positions) | No (parser will validate every record) | Compare [SUNBIZ.md §4](SUNBIZ.md) with https://dos.sunbiz.org/data-definitions/cor.html |
| V2 | Sunbiz SFTP on port 22 | Sandbox: tunnel opened, connection reset before the SSH banner. HTTPS/WebDAV (primary) works. **Windows 10 (2026-11-03): SSH banner, SFTP login and directory listing all succeeded** (`net-check --only sunbiz_https,sunbiz_sftp`). Closure left to review. | Neither (fallback path only) | No | Windows procedure in [SUNBIZ.md §7](SUNBIZ.md) |
| V3 | Sunbiz quarterly ZIP contents (member layouts, active-only vs all records, principal-state population) | ZIP directories intact; contents not downloaded | **Completeness** (back-fill of gap dates) and **correctness** of quarterly-sourced fields | Partly: affects back-fill design | First quarterly import on your machine |
| V4 | Geofabrik extracts (required for OSM) | Sandbox: main host resets during TLS; `download-ext2` returns proxy 502. **Windows 10 (2026-11-03): `geofabrik_main` operational (MD5 file and PBF range read); `download-ext2` did not resolve in DNS on that network.** OSM ingestion itself not run. | **Completeness** (OSM as a source) | Yes for OSM; Overture covers primary discovery | `leadintel net-check --only geofabrik_main,geofabrik_ext2` on Windows |
| V5 | Overpass instances | Reset or timeout from the sandbox | Completeness (optional fallback) | No (optional fallback) | Same, on Windows |
| V6 | Common Crawl index, Wayback, RDAP (`data.iana.org`, `rdap.verisign.com`) | Reset, or refused by the sandbox proxy. **Windows 10 (2026-11-03): Common Crawl data, index and CloudFront, Wayback CDX, IANA RDAP bootstrap, Verisign RDAP and rdap.org all operational.** Closure left to review. | Completeness (optional website-history signals) | No (optional website-history signals) | Same, on Windows |
| V7 | Duval BTR report export | Tax Collector page works; report portal shows a Cloudflare bot challenge (never bypassed) | **Completeness** (BTR evidence in Duval) | No | Manual export or public-records request; decide per county |
| V8 | NPI bulk files | Page reports "down for maintenance"; API works | Completeness (NPI bulk; the API works) | No | Re-run diagnostics later |
| V9 | Sunbiz HTTPS rate limits for bulk downloads | First full 30-day download on Windows (2026-11-03): 107 files fetched; 8 requests failed with a dropped connection (`incomplete chunked read`) or `401` under load; 6 succeeded on the retry, 2 failed twice and were recorded as gaps (coverage `partial`); the next run fetched them (`completed`). Quarterly download not yet observed. | Completeness (bulk download throughput) | No | Observe during the first quarterly download (resumable) |
| V10 | Clay and St. Johns County BTR data access | Not yet checked; Duval's report portal is behind a bot challenge | **Completeness** (BTR evidence in St. Johns / Clay) | **No** (decided: generic manual-CSV import covers any county) | Check each county Tax Collector's published reports; public-records request where needed |
| V11 | Review-count evidence from free sources | Overture has no review fields (verified). No permitted free bulk source identified. | **Completeness** (review evidence only by manual observation) | No (manual entry planned) | Manual research fields, or Google Places after the [ROADMAP.md §4](ROADMAP.md) checklist |
| V12 | Windows run of the PR #1 test suite and live diagnostics | **Closed: passed on Windows 10 Home 22H2, 2026-11-03**, at commit `692f1db`. All 12 checks, the two Windows failures found and their fixes: [Windows validation](#windows-validation-v12-2026-11-03) | **Correctness** (platform-specific behaviour on Windows) | Before merging PR #1 | Procedure in PR #1 description |
| V13 | DBPR licensee CSV column meanings (no published layout found); county-code map (all 67 codes, alphabetical from 11: Clay 20, Duval 26, St. Johns 65) | **Open: inferred, not officially verified.** Consistency check only: 99.21% of 626,509 rows agree with the Census ZIP->county relationship; the 0.79% disagreements are investigated in [DBPR_GEOGRAPHY.md](DBPR_GEOGRAPHY.md). On import, county/ZIP conflicts are excluded and never placed in a target county; code-only and ZIP-only assignments are flagged; every decision is stored with its raw values and reason (`code_map: inferred_v13`) | **Correctness** (licence evidence attribution) | No (evidence only; never creates prospects) | Find the official DBPR extract layout and code table, or ask DBPR |
| V14 | One real Sunbiz corporate record with document number `11` (file 20261027c.txt, line 220) | **Open.** Quarantined, not guessed; still the only quarantine record after run 9 (2026-11-03) | Correctness (single record) | No | Re-check after the next quarterly file |

## Validation of the PR #3 review fixes (2026-11-03)

Environment: build sandbox (Linux), Python 3.13.18, PostgreSQL 16.15 + PostGIS 3.4.2, Chromium. Windows was not run for this section; see [Windows validation](#windows-validation-v12-2026-11-03) below. Full results, the real-data comparison and limitations are in [PHASE1_PROGRESS.md §5](PHASE1_PROGRESS.md).

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

## Windows validation (V12), 2026-11-03

Run on a real Windows PC by the project owner, following [WINDOWS_SETUP.md](WINDOWS_SETUP.md), with results relayed from the PowerShell output. Every value below was measured on that machine unless marked "Linux".

### Environment

| Item | Value |
|---|---|
| OS | Windows 10 Home 22H2 (build 19045), Windows PowerShell 5.1 |
| Python | 3.13.17 (python.org), virtual environment in `backend\.venv` |
| PostgreSQL | 16 (EDB installer, service `postgresql-x64-16`), PostGIS 3.4.2, pg_trgm 1.6 |
| Database roles | `leadintel` is a regular (non-superuser) role that owns `leadintel` and `leadintel_test`; extensions were created once by `postgres` |
| Python packages | Same versions as the Linux sandbox, except psycopg 3.3.7 (libpq 18.0.4) and pyarrow 26.0.1, patch-level newer |
| Code | `phase-1` at `692f1db` |
| Repository location | Inside a OneDrive-synced folder. OneDrive sync was paused during the data runs |
| Browser | Chrome, desktop window and DevTools device mode (iPhone 16, 393 × 852) |

### Results

| # | Check | Command(s) | Result |
|---|---|---|---|
| 1 | Setup and dependencies | `py -3.13 -m venv .venv`; `.venv\Scripts\pip install -e ".[dev]"` | **Pass**: all packages installed from native `win_amd64` wheels |
| 2 | PostgreSQL / PostGIS connectivity | `leadintel db status` | **Pass**: connected over IPv6 and IPv4; password shown as `***` |
| 3 | Migrations from a clean database | `leadintel db upgrade` | **Pass**: revision `0005 (head)`; 30 tables, the same as Linux |
| 4 | Full test suite on PostgreSQL / PostGIS | `.venv\Scripts\python -m pytest -rs -p no:cacheprovider` | **Pass: 237 passed, 5 skipped, 0 failed, nothing deselected** (61.6 s). The 5 skipped are the opt-in live tests (check 5). First run failed (see Failures 1) |
| 5 | Live-network tests | `$env:LEADINTEL_LIVE_TESTS="1"`; `pytest tests/test_live_network.py -v` | **Pass: 5 passed** (59.1 s): three Overture mirrors, live failover, live diagnostics |
| 6 | Network diagnostics, zero paid requests | `leadintel net-check --no-store` | **Pass**: exit code 0, `paid requests made: 0`, all 14 paid providers `blocked_by_policy`. Sunbiz HTTPS and SFTP operational. Optional Overpass servers timed out; `download-ext2.geofabrik.de` did not resolve; the Duval tax portal's bot challenge was detected and not bypassed (V7) |
| 7 | Real-data discovery (Sunbiz, DBPR, de-duplication) | `leadintel config seed`; `leadintel discover --since 2026-10-04` | **Pass**: run 1 `partial` (36 min 19 s; 2 Sunbiz files refused twice by the server, recorded as gaps, see V9); run 2 `completed` (4 min 20 s; fetched the 2 missing files, reused 107) |
| 8 | Interrupted run and re-run idempotency | run 3 plain re-run; run 4 force-stopped after 60 s with `Stop-Process -Force`; run 5 recovery | **Pass**: runs 1–5 = `partial`, `completed`, `completed`, **`interrupted`**, `completed`. **0 duplicates** on 8 natural-key checks; totals identical before and after runs 3–5 |
| 9 | Merge / unmerge on real data | Rollback-only script: unmerge merge event 1, then re-merge, then roll back | **Pass**: merged location active again, 7 rows moved back, re-merge restored the state exactly, nothing created or lost; rolled back (still 346 merge events) |
| 10 | Browser, desktop and mobile | `.venv\Scripts\uvicorn app.api.main:create_app --factory --port 8000`; Chrome | **Pass**: list, map and profile on desktop; stacked layout and profile on iPhone 16 width; **no console errors in an Incognito window**. The 2 console errors in the normal profile came from browser extensions that inject elements into the page. 8 console warnings were not reviewed |
| 11 | Secret scan, zero paid requests | `git status`; `git check-ignore -v ..\data .venv`; search of tracked files and `git log -p --all` for both passwords | **Pass**: working tree clean; `data` and `.venv` ignored; neither the Sunbiz password nor the database password appears in any tracked file or commit. `paid_requests: 0` in runs 1, 2, 3 and 5 (run 4 was killed before writing a result). Linux: the Sunbiz password appears 0 times in all 27 commits on all 4 branches |
| 12 | Backup and restore | `pg_dump -Fc` (§4); `createdb -O leadintel leadintel_restore_check`, extensions created by `postgres`; `pg_restore -U leadintel --no-owner` (§5) | **Pass**: 99,972,481-byte dump; restored into a new, separate database; row counts and full-content fingerprints of all 30 tables, both extensions and the revision **identical** to the source; source fingerprint unchanged by the backup. `pg_restore` reported the 3 expected errors (see notes) and exit code 1. The data-folder archive (`Compress-Archive`) was not tested |

**Real data compared with Linux** (same code and the same `--since` date; the Windows run was later the same day):
- **Identical:** Overture places in the target counties 92,486 (Duval 65,674, St. Johns 17,289, Clay 9,523); closed places 3,576; co-located pairs left alone 12,031; automatic merges 346; the quarantined Sunbiz record (V14, `20261027c.txt` line 220).
- **Higher on Windows:** organisations 67,704 vs 65,088 and active locations 93,789 vs 93,732. More daily Sunbiz files had been published by the time of the Windows run.
- **Not investigated:** current licence facts 1,424 vs 1,426, a difference of 2.

### Failures found on Windows, and fixes

1. **Missing database connection timeout (fixed in `692f1db`).**
   - **Symptom:** on Windows, a refused PostgreSQL connection is not reported early. With no timeout set, psycopg waited its 130 s default: measured 130.0 s with no timeout, 3.0 s with `connect_timeout=3`.
   - **Effect:** the first full test run aborted at `test_api_business.py::test_business_db_down_returns_503_while_diagnostics_work`, and `test_config_store.py::test_database_unavailable_falls_back_to_defaults` hung the same way. In real use, a stopped PostgreSQL service would have made the API and CLI hang for 130 s.
   - **Fix:** a 5 s default `connect_timeout` for the business database, migrations and the diagnostics store. A timeout in the URL or `PGCONNECT_TIMEOUT` is preserved.
   - **Result:** both tests pass on Windows.
2. **Test fixture needed a superuser (fixed in `d8944b5`, before the Windows run).** The fixture recreated PostGIS, which a regular role cannot do. It now drops only LeadIntel's own tables and refuses to run against a database whose name doesn't end in `_test`. The Windows run (check 4) confirms it as a regular role.

### Windows-specific notes

- **Test timeouts:** pytest-timeout uses the `thread` method on Windows, so a single test timeout ends the whole test session; the remaining tests are not reported.
- **Process chain:** `leadintel.exe` starts the virtual environment's `python.exe`, which starts the base interpreter. Force-stopping the launcher ends the whole chain, so stopping the child processes afterwards reports "process not found".
- **Interrupted runs:** for an interrupted run, `finished_at` is the moment the next run detected it, not the moment the process stopped.
- **PowerShell 5.1:** it drops an empty argument passed to a native program, and `Read-Host` shows what is typed. `setx` values reach only new windows, while `$env:` values last for one window only.
- **Restore warnings:** a restore by the regular `leadintel` role prints 3 harmless errors, `COMMENT ON EXTENSION` for postgis and pg_trgm and `COPY spatial_ref_sys`, all for objects owned by the extensions. No LeadIntel data is affected (check 12).
- **Repository inside OneDrive:** sync was paused during downloads to avoid file locks and uploading about 300 MB of source data; no locking errors occurred.

### Setup problems hit during this run (environment, not product)

- **Placeholder values.** The `setx` placeholders (`choose-a-password`, the Sunbiz username text) were first entered literally, which caused authentication failures. Corrected by re-entering the real values.
- **Forgotten `postgres` password.** It was reset by temporarily switching only the two local `host all all` lines of `pg_hba.conf` to `trust`, setting new passwords, and restoring the original file from a backup copy, confirmed byte-identical by hash comparison. A wrong password was then rejected again.

### Documentation gaps noticed (not changed in this commit)

`WINDOWS_SETUP.md`:
- does not say to check out the `phase-1` branch, although the default branch is `main`;
- doesn't make clear which values are placeholders to replace;
- doesn't give the command that starts the web UI (it is in `NETWORK.md`);
- doesn't mention the live tests;
- doesn't mention the 3 expected restore errors;
- says "tested commands are marked ✅" but marks none.

Separately, `leadintel db status` prints a raw traceback for a wrong password, rather than the "business database unavailable" message described in §6.

V13 and V14 remain **open**. The DBPR county-code map is consistent with Census ZIP data on both Linux and Windows, but it is **not officially verified**, and the V14 record is still quarantined.

## Phase A portability verification on Windows, 2026-11-03

Phase A (`95d5ceb`) lets downloaded source files keep working after the data folder is moved to another folder or computer. This section records its verification on the same Windows PC as the [Windows validation](#windows-validation-v12-2026-11-03). Results were taken from the PowerShell output provided by the project owner.

### Environment

| Item | Value |
|---|---|
| OS | Windows 10 Home 22H2 (build 19045), Windows PowerShell 5.1 |
| Python | 3.13.17 |
| PostgreSQL | 16 (EDB installer), PostGIS 3.4.2, pg_trgm 1.6 |
| Code | `phase-1` at `95d5ceb2978e86eece90c61147ca2ab6245da2e5` |
| Repository location | Inside a OneDrive-synced folder; sync paused during the test |

### Automated tests

| Check | Result |
|---|---|
| Portability regression tests (`tests/test_portability.py`) | **11 passed** (3.20 s) |
| Full test suite on PostgreSQL / PostGIS | **248 passed, 5 skipped, 0 failed** (63.63 s). The 5 skipped are the opt-in live-network tests |

### Real-data verification on copies

Every operation that could change data ran only against copies: the real data folder, copied to `C:\leadintel-portability-test`, and the real database, restored into the separate database `leadintel_portability_check`. The originals were only read: to take baselines, to make the copies, and for the before/after integrity comparison.

The checks used a dedicated script that is not part of the repository. It had two network guards:

- **HTTPX transport:** the app's download check ran on an HTTPX client whose transport refused every request, including HEAD, GET and redirect hops.
- **Python sockets:** inside the script's own Python process only, `socket.getaddrinfo`, `connect` and `connect_ex` refused any host other than localhost. This is not an operating-system-wide block. PostgreSQL's connection to the local copy database does not go through Python sockets.

No HTTP request was attempted and no network attempt was refused in any check.

Before any source-data file or sidecar under check was opened or hashed, the script resolved its path on disk, following links. The path had to lie inside the copy and outside the original; otherwise the check failed without opening the file.

| # | Check | Result |
|---|---|---|
| 1 | Starting state | Original data folder: 229 files, including 113 `.meta.json` sidecars and no partial downloads. 0 of the 238 items (229 files, 9 folders) were flagged by the offline, recall-on-open or recall-on-data-access attributes. Original database: 112 `source_file` rows, all stored as legacy absolute Windows paths into the original data folder (Sunbiz 109, DBPR 2, Census boundaries 1) |
| 2 | Baselines | SHA-256 of every original file; row count and content fingerprint of all 30 tables, both extensions and the Alembic revision |
| 3 | Copy, then move | Copy identical to the original (229 files: names, sizes, SHA-256); still identical after moving it to a different folder |
| 4 | Database copy | `pg_dump -Fc` (95 MB) restored as the regular `leadintel` role; only the 3 expected restore errors (see [Windows-specific notes](#windows-specific-notes)). Copy fingerprint identical to the original's |
| 5 | Stored paths resolve in the moved copy | **112 of 112** rows found in the moved copy with matching SHA-256; 0 failed, 0 exceptions |
| 6 | Cache reuse from legacy sidecars | All 113 sidecars passed a validity preflight first. **113 of 113** files reused with no download; each sidecar was rewritten to the portable file-name form. **0 HTTP requests attempted**; 0 network attempts refused |
| 7 | New registrations are relative | A new row was stored as `dbpr/CONSTRUCTIONLICENSE_1.78384f22878e.csv` and resolved inside the moved copy; rolled back (112 rows before and after) |
| 8 | Stored paths after the sidecar rewrite | 112 of 112 rows still resolved; 0 failed, 0 exceptions |
| 9 | Second move | Copy unchanged by the move (229 files); 116 data files identical to the original; 113 sidecar names unchanged. **113 of 113** file-name-only sidecars reused with **0 HTTP requests attempted**; 0 network attempts refused; 112 of 112 rows resolved |
| 10 | Originals untouched | Original data folder (229 files: names, sizes, SHA-256) and original database fingerprint **identical** before and after the test |

The copy folder and the copy database were kept for review.

### Limitations and open items

- Existing `source_file` rows keep their legacy absolute paths. Phase A maps them when they are looked up and does not rewrite them; only new registrations are stored relative to the data folder.
- **Follow-up, since implemented:** at `95d5ceb`, `resolve_local_path()` mapped a legacy absolute path by its last component named `boundaries`, `dbpr`, `overture` or `sunbiz`, wherever that component appeared in the path, and fell back to the stored absolute path if no mapped file existed. It now resolves by `source_file.kind` with exact folder layouts and no fallback: see [Source path resolution by kind](#source-path-resolution-by-kind-2026-11-03).
- **V13** (DBPR file layout and county-code map) remains **open**: not officially confirmed.
- **V14** (Sunbiz document number `11`) remains **open**: still quarantined, pending the next quarterly file.

## Source path resolution by kind, 2026-11-03

Two commits on `phase-1`, both tested on Linux and Windows:

- **`b50ea12`** `fix: constrain source path resolution by kind`. `resolve_local_path()` now requires the row's `kind`:
  - The six managed kinds map to explicit folders: `cor`, `fic`, `cor_event` and `fic_event` to `sunbiz/<kind>/`; `dbpr_licensees` to `dbpr/`; `boundary_county` to `boundaries/`.
  - A relative entry must be exactly `<folder>/<file>`. A legacy absolute entry must end in `<folder>/<file>`, and the old data folder may have any name. The file is looked up only in the current data folder; a missing copy gives no result and never falls back to the stored absolute path.
  - Any other kind (e.g. `btr_csv`) is an external file: only an existing file at an absolute stored path is accepted, never a path relative to the working directory.
  - For managed kinds, only regular files inside the data folder are returned; links resolving outside it are rejected. External kinds are handled separately and must point to an existing file at an absolute path. A path starting with `/` is read as a POSIX path even if a name contains a backslash.
  - The resolver locates the file but does not verify its contents. Consumers requiring integrity verification must compare the file's SHA-256 against the stored value.
- **`56162bd`** `fix: store absolute paths for manual BTR imports`. `leadintel import-btr` stores the resolved absolute path of the CSV. Existing rows are not modified.

### Test results

| Run | Environment | Result |
|---|---|---|
| Full test suite (297 collected) | **Windows**: Python 3.13.17, PostgreSQL 16.15, PostGIS 3.4.2, pytest 9.2.0 | **290 passed, 7 skipped, 0 failed** (63.84 s) |
| `tests/test_portability.py` and `tests/test_cli_import_btr.py` | Windows, as above | **53 passed, 2 skipped** (5.40 s) |
| Full test suite (297 collected) | Linux build sandbox, test database only | **292 passed, 5 skipped, 0 failed** |

Results for Windows were taken from the output provided by the project owner.

**Not executed on Windows** (7 skipped):

- **5 live-network tests:** they run only with `LEADINTEL_LIVE_TESTS=1`, which was intentionally not set.
- **`test_posix_absolute_path_with_a_literal_backslash_is_parsed_as_posix`:** POSIX only, because a backslash cannot be part of a file name on Windows.
- **`test_link_inside_the_data_folder_pointing_outside_is_rejected`:** skipped because creating a symbolic link was not available.

Both platform-specific tests ran and passed on Linux; the second is therefore not verified on Windows.

**Warning (non-failing):** `StarletteDeprecationWarning`, about using `httpx` with `starlette.testclient`; it recommends `httpx2`. Not changed.

V13 and V14 remain **open**:

- **V13:** the DBPR licence file layout and county-code map still need official verification.
- **V14:** Sunbiz document number `11` remains quarantined, pending a recheck against the next quarterly file.

The v0.3 requirements are consolidated in [ROADMAP.md](ROADMAP.md); the 2026-11-03 architecture review is in [REVIEW.md](REVIEW.md).
