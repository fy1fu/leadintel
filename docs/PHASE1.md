# Phase 1 implementation checklist: discovery and qualification

Approved 2026-11-03 (F1–F11 with clarifications; five-phase sequence). Scope: **discovery and qualification only.** No CRM, competitor benchmarking, website crawling, website generation or outreach.

Phase 1 builds on the network layer from PR #1 and must not remove any of it:
- registry, diagnostics and the separate diagnostics store;
- cost guard;
- safe-egress client;
- Overture mirror failover;
- OSM extract downloader.

Each milestone is **done only when its tests pass and its real-data check has been run and reported.** The ✅ markers below are updated as items land.

---

## M0 — Foundation

| # | Item | Acceptance criteria | Tests |
|---|---|---|---|
| 0.1 | Business DB on Postgres + PostGIS + pg_trgm, Alembic migration `0001` | Fresh `alembic upgrade head` creates the Phase 1 schema. `downgrade base` removes it. Conventions as in DATA_MODEL.md §0. | Migration up/down/up on a scratch DB; schema matches models |
| 0.2 | Settings split | `LEADINTEL_DATABASE_URL` = business DB only. Diagnostics keep `LEADINTEL_NETDIAG_DB_URL`. The unused SQLite `db_url` is removed (review R2). | Existing 97 network tests still pass; diagnostics run with the business DB stopped |
| 0.3 | Config: defaults + versioned DB copy (F1) | Packaged defaults in `config/defaults/*.yaml` (areas, keywords, industries, scoring, sources) always load. On first run they are seeded into `config_version`. Scoring, geography, industry priorities and exclusions keep version history. Scoring-weight changes need approval and can be reverted. If the DB is unavailable, the app starts on the defaults and says so. `network.yaml` is never stored in the DB. | Seed, propose, approve, revert; an unapproved scoring change is not active; DB down → defaults with a warning |
| 0.4 | Areas (D1) | Duval (12031), St. Johns (12109), Clay (12019) loaded from Census cartographic boundary files with source checksums. The area resolver supports county / place / ZIP (ZCTA) / radius / polygon. | Point-in-area for known addresses in each county; an outside point is rejected; repeat load is idempotent |
| 0.5 | Source and provenance core | `source`, `source_file`, `source_record` (raw line / payload + hash), `fact` (provenance, `verification_state`, timestamps, TTL), `discovery_run`, `coverage`, `quarantine_record` | Every location in the test fixtures traces back to its source records |
| 0.6 | Windows guide (F2) | `docs/WINDOWS_SETUP.md`: native PostgreSQL / PostGIS / Python / Node installation, first run, backup (`pg_dump -Fc` + data folder), restore, and recovery (DB down, corrupt snapshot, failed migration) | Commands reviewed. Live Windows run is verification item V12. |

## M1 — Discovery pipeline (first milestone)

| # | Item | Acceptance criteria | Tests |
|---|---|---|---|
| 1.1 | Overture snapshot per release (F6) | Bounding box of enabled areas → local GeoParquet under `data/overture/<release>/`. Manifest holds release, bbox, mirror used, source file list with sizes, row count and SHA-256. Re-running with the same release and area makes **0** remote data requests. A truncated or corrupt snapshot is detected by checksum and rebuilt. Cleanup keeps the active and previous release and never deletes a release referenced by an unfinished run. | Reuse without network (mock transport asserts no calls); checksum mismatch → rebuild; cleanup keeps referenced releases |
| 1.2 | Overture import | Points filtered to target areas in PostGIS. Normalization: empty websites dropped; URLs classified independent / social / link_hub / listing; phones to E.164; emails lower-cased; Wikidata brand ID kept. One `source_record` per place. Facts start as `ingested`. Closed places are kept with status, never silently dropped. | Normalization unit tests (including empty-string websites and Facebook URLs never becoming websites); import is idempotent (rerun → 0 new rows) |
| 1.3 | Sunbiz download (HTTPS/WebDAV) | Date-ranged daily `c`, `ce`, `f` and `fe` files. Size check against the listing; SHA-256 stored. Re-downloaded if size or modified date changes. Date ledger states as in SUNBIZ.md §5. Credentials only from env, never logged. | Mock WebDAV server: listing, gaps → `absent_unexplained`, short file → `failed`, changed file → re-import |
| 1.4 | Sunbiz parser | Byte-slice + per-field decode, NUL strip, `MMDDYYYY` dates, layouts from SUNBIZ.md §4. A wrong length, undecodable field or invalid date → quarantine with reason; the rest of the file still imports. | Fixtures from real records (personal names replaced); malformed lines; mixed encodings |
| 1.5 | Sunbiz import | `organization` with `filing_kind` + raw code. Principal / mailing / registered-agent / owner addresses as typed `address` rows. RA address = principal, or an address shared by > N filings → `is_operating_location = false`. DBA → owner via `own_charter` (`organization_link dba_owned_by`). Events update status (dissolution, reinstatement). | One company with several DBAs; DBA filed before its owner appears (link resolves later); RA-address case; idempotent re-import |
| 1.6 | Geocoding and county | Census batch geocoder for operating addresses (cached). County from the geocoded point; ZIP fallback recorded as `geocode_method`. Only operating locations inside target areas become prospects. | Recorded geocoder responses; ZIP fallback path; out-of-area record kept as an organization but not as a prospect |
| 1.7 | Entity resolution v1 | Rules (§Relationships below). Auto-merge only on strong evidence. Ambiguous → `review_queue`. Every merge → `merge_event`; unmerge restores prior links. Source records are never altered. | Same name + shared address is **not** auto-merged; same phone within 150 m is merged; multi-location brand stays as separate locations; unmerge round-trip |
| 1.8 | Coverage and run reporting | Each `discovery_run` records per-source × area coverage: `complete` / `partial` (ledger gaps, quarantine) / `failed` / `not_run`. The report separates *imported* from *verified* counts. A source failure never yields "0 businesses found". | Failed source → `failed` and absent from counts; ledger gap → `partial` |
| 1.9 | CLI end-to-end | `leadintel discover --areas duval,st_johns,clay --since <date>` runs: config → areas → Overture snapshot/import → Sunbiz download/import → geocode → resolve → coverage report. Resumable and idempotent. | Integration test on fixtures + Postgres; real-data run on the three counties, reported with counts |

## M2 — Qualification and provisional scoring

| # | Item | Acceptance criteria | Tests |
|---|---|---|---|
| 2.1 | Keyword engine | Phrase / word-boundary matching; stemming for positive groups only; neutralization rules; `keyword_hit` rows | SCORING invariant 14 and the false-positive list |
| 2.2 | Industry profiles v1 | Seed niches with Overture taxonomy, OSM tag, DBPR licence and keyword mappings. Priority affects ordering only (F9). | Classification fixtures per niche |
| 2.3 | Provisional BOS / LPS | SCORING §4.4 pipeline; evidence gate; verification multipliers; factor ledger; `stage = provisional`; caps recorded | Invariants 1, 2, 5, 6, 9, 11 (property-based), 12, 13 |
| 2.4 | Qualification (F8) | Separate from ranking: a boolean plus reasons. Requirements: evidence ≥ 3, website-related opportunity (or `unknown` → "needs research"), a usable contact route (a legitimate business mailing address counts; registered-agent or mass-filer addresses do not), inside a target area, not excluded. LPS ranks qualified prospects. | A qualified prospect can have low LPS; a mail-only legitimate address qualifies; an RA address does not |
| 2.5 | Niche comparison v1 | Per niche × county: discovered, provisionally qualified, needs research, contact availability, confidence / source completeness, coverage state. No automatic winner. | Report built from fixtures; incomplete coverage is flagged |
| 2.6 | Prospect export | CSV with provenance, verification state per contact field, scores with stage, and reasons. Missing values stay empty with a reason, never inferred. | Export columns; no inferred emails |

## M3 — Additional sources

| # | Item | Acceptance criteria | Tests |
|---|---|---|---|
| 3.1 | DBPR licences | Relevant licensee CSVs (construction trades, cosmetology, etc., chosen per industry profile). Streamed with resume and checksum. Licences link to organizations and locations as evidence. | Parser fixtures; idempotent import; licence evidence appears in the factor ledger |
| 3.2 | BTR manual CSV import | Generic column mapping per county. Validation report. Rows → source records and facts (`provenance = registry`, source = county BTR). | Mapping errors reported, not guessed |
| 3.3 | Duval BTR automation | Only if a permitted automated path exists; the bot challenge is never bypassed | Probe / adapter test |

## M4 — Inspectable results

| # | Item | Acceptance criteria | Tests |
|---|---|---|---|
| 4.1 | Minimal API + map/list page | FastAPI on `127.0.0.1`. MapLibre with OpenFreeMap tiles, clustered prospects, list synced with the map, profile panel showing sources, verification states and score factors | API tests; Playwright smoke test |
| 4.2 | Phase 1 acceptance run | All ROADMAP §2 Phase 1 criteria re-checked on real data for the three counties | Report committed to `docs/` |

---

## Relationship rules (entity resolution v1)

| Situation | Handling |
|---|---|
| One company owns several DBAs | One `organization` (owner) + one per DBA, linked `dba_owned_by`. Each DBA's operating address yields its own location unless it matches an existing location. |
| One business, several physical locations | Separate `location` rows linked to the same organization (`location_organization`); never merged across addresses more than 150 m apart. |
| Several listings for the same operating business | Merged into one location only on strong evidence: same normalized phone within 150 m, or the same Overture GERS ID. Otherwise → review queue. |
| Different locations with different websites or online presence | Websites and profiles attach to the **location**, not the organization |
| Similar names or a shared address | **Never sufficient on its own.** Name similarity ≥ 0.9 within 75 m without a conflicting phone → review queue. A shared address (office suites, registered agents) → no merge. |
| Ambiguous match | `review_queue` with both sides and the evidence; nothing combined until decided |
| Reversal | `merge_event` keeps before/after link sets; unmerge restores them; source records are never modified |

## Non-negotiables checked at every milestone

- No website generation or mockups.
- No outreach code.
- 0 paid requests under default settings (test).
- Network diagnostics run without the business DB (test).
- Every fact has provenance and a verification state.
- Merges are reversible.
- Score factors are recorded.
- History is append-only.
- Coverage-aware reports.
- The full PR #1 test suite stays green.
