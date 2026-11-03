# Architecture review before Phase 1 (2026-11-03)

**Scope:**
- PR #1 (`network-diagnostics`, code).
- PR #2 (`plan-v0.3`, docs).
- PLAN.md, ROADMAP.md, SCORING.md, DATA_MODEL.md, VERIFICATION.md, SUNBIZ.md, NETWORK.md.

**Method:**
- Full read of every document.
- Requirement trace against the v0.3 specification and the 2026-11-03 decisions.
- `grep` audits of code and config.
- Test-suite run.

Findings were fixed in the documents in the same commit unless listed as open.

## 1. Consistency issues found and fixed

| # | Issue | Fix |
|---|---|---|
| C1 | Order of operations between keyword/filing bonuses and the evidence cap was undefined; keyword weights had no defined scale | SCORING.md §4.4: one normative pipeline. Bonuses enter a bounded adjustment `T`; caps are applied **last** as a `min`. Property-based test (invariant 11). |
| C2 | Two ranking notions ("initial priority" for registry leads vs BOS) | Removed. Provisional BOS/LPS come from the same pipeline in Phases 1–2. |
| C3 | Stemming applied to negative keywords ("Trusted Lawn Care" → `trust` penalty) | Stemming applies to positive groups only; trade words neutralize `capital` / `trust` penalties (invariant 14) |
| C4 | WQS 60–69 fell between "outdated" (< 60) and "reasonably good" (≥ 70) | Explicit "average / moderate improvement opportunity" band |
| C5 | "Qualified prospect" was used by the niche comparison but never defined | SCORING.md §6 definition (approval item F8) |
| C6 | Contact completeness ignored verification state (imported = verified) | Verification multipliers in LPS (invariant 13) |
| C7 | Manual observations could be overwritten; staleness effect unspecified | Append-only with `reverifies_id`; staleness removes only review evidence (invariant 12) |
| C8 | Google/Yelp listings were stored as social profiles, making "Facebook + Google listing" social-only | `online_profile.platform_kind` (social / link_hub / listing) |
| C9 | ROADMAP mentioned SMS, which is not one of the four channels | Text messaging explicitly unsupported |
| C10 | ROADMAP §1.12 table rows had the wrong column count; BTR status was outdated | Table fixed; generic BTR manual-CSV importer added; St. Johns/Clay non-blocking |
| C11 | Keyword lists missed `detail`, `fishing` and `boat maintenance` from the v0.3 lists | Added |
| C12 | Industry priority had no defined effect | Proposed: ordering only, never BOS/LPS (F9) |

## 2. Schema gaps found and fixed (DATA_MODEL.md, 🔧 rows)

| Requirement | Gap | Added |
|---|---|---|
| LLC + DBA + listing = one business | No location ↔ organization relation | `location_organization` (legal_owner / trade_name / franchise_parent) |
| Reversible deduplication, audit | No merge history | `merge_event` |
| Coverage per search | `coverage` referenced runs and areas that weren't defined | `discovery_run`; single `area` table (replaces `search_area` / `target_area`) |
| Time to close, funnel analytics | No status history | `lead_status_history` |
| v0.1 notes and custom lists | Dropped in consolidation | `lead_note`, `lead_list`, `lead_list_item` |
| Do-not-contact enforcement | No normalized contact identity; enforcement scope vague | `contact_point`; enforcement rules spelled out (inbound still loggable) |
| Explainable caps | Scores didn't record the cap | `score.stage`, `evidence_points`, `caps_applied`, `pre_cap_value` |
| Conventions | Undecided keys, time and geometry types | DATA_MODEL.md §0 |

### Support check: required capabilities → tables

| Capability | Tables |
|---|---|
| Scoring | `score`, `score_factor`, `keyword_hit`, `config_version`, `benchmark_set`, `peer`, `gap_item` |
| Provenance and data quality | `source`, `source_file`, `source_record`, `source_date_ledger`, `quarantine_record`, `fact` (verification_state, TTL), `coverage`, `location.completeness` |
| Deduplication | `location`, `location_alias`, `location_source_link`, `location_organization`, `review_queue`, `merge_event` |
| Address roles | `address` (principal / mailing / registered_agent / owner / listing, `is_operating_location`) |
| DBA relationships | `organization` (`filing_kind`), `organization_link` (`dba_owned_by`) |
| Manual observations | `manual_observation` (append-only) → `fact` (`provenance = manual`) |
| CRM and revenue | `lead`, `lead_status_history`, `lead_note`, `lead_list*`, `contact_event`, `contact_point`, `follow_up`, `do_not_contact`, `campaign*`, `package*`, `deal`, `authorization_record`, `outcome_snapshot` |

## 3. v0.3 requirement trace

| v0.3 section | Where it is covered | Status |
|---|---|---|
| §1 Sunbiz / DBA priority, layouts, address roles, linking, provenance, malformed data | ROADMAP 1.1; SUNBIZ.md; SCORING 3; DATA_MODEL 1–2 | Covered |
| §2 Keyword lists (all initial and additional terms), penalties, phrase matching, audit | SCORING 2; ROADMAP 1.2 | Covered (C3, C11) |
| §3 Established businesses without websites; nine presence categories | SCORING 1; ROADMAP 1.3 | Covered. Reviews come only from manual observation (no free source). |
| §4 WQS / BOS / LPS, signals, caps, explainability | SCORING 4–7 | Covered |
| §5 Niche targeting, priorities, comparison, conversion tracking | ROADMAP 1.5; SCORING 6 | Covered (F8, F9) |
| §6 First campaign, 20–30 shortlist, export fields | ROADMAP 1.6, Phase 4 | Covered |
| §7 Competitor analysis | ROADMAP 1.7 | Covered. Search *ranking* is not measurable with free sources; on-site SEO only. |
| §8 | Redacted | Not assumed |
| §9 Service packages, MRR (tracking what you sell, not building sites) | ROADMAP 1.9; DATA_MODEL 5 | Covered |
| §10 CRM, manual channels, safeguards | ROADMAP 1.9; DATA_MODEL 5 | Covered |
| §11 Performance metrics, reversible outcome learning | ROADMAP 1.10; SCORING 8 | Covered |
| §12 Google Places optional + checklist | ROADMAP 1.11, 4 | Covered |
| §13 Phase order | ROADMAP 2 | Adopted; mockups removed per decision D5 |

## 4. Exclusion checks

| Check | Method | Result |
|---|---|---|
| No website generation or mockups | grep of code, config and docs | None in code or config. The docs mention them only as explicitly out of scope. Service packages describe offerings the user sells and tracks; nothing generates sites. |
| No automated outreach | grep for mail/SMS/telephony libraries and send/dial code | None. CRM design is manual logging only, with no sending integrations. |
| No paid services under default config | Code audit + tests | Every outbound path goes through `CostGuard`: the infrastructure client request hook (each redirect hop included), safe-egress validation of each hop, probes and resolvers, SSH/SFTP. Paid hosts appear only in the blocked `paid_providers` list (14 providers). Default `LEADINTEL_ALLOW_PAID=false`; enabling needs two settings. Tests assert 0 paid requests. |
| Diagnostics independent of the business database | Tests | Own store, memory fallback, `--no-store`; tested with the main database pointing at an unreachable Postgres |

## 5. PR #1 code review notes (non-blocking)

| # | Finding | Recommendation |
|---|---|---|
| R1 | `safe_egress.py` sets httpx's private `transport._pool` to install the DNS-pinning backend | Keep it, pin `httpx`/`httpcore` minor versions, and keep the existing tests as the guard. Alternative: a small custom transport on `httpcore` directly. |
| R2 | `Settings.db_url` (SQLite `leadintel.db`) is unused and could be mistaken for the business database | Remove or rename at the start of Phase 1 |
| R3 | The diagnostics API has no authentication | Keep binding to `127.0.0.1` (uvicorn's default); document it (F4) |
| R4 | `MirrorSet` reads diagnostics health from the diagnostics store | Business code must treat that store as optional and fall back to priority order (already supported with `store=None`) |
| R5 | The SFTP fallback accepts the server host key on first use (TOFU) | Pin the fingerprint after the first successful connection; alert on change |
| R6 | Probes reference specific public files (`CC-MAIN-2024-10`, `TIGER2025`, `cilb_certified.csv`) | Expected drift shows up as a failure. Review yearly. |
| R7 | The Overture reader re-reads remote row groups per call (~42 MB for one small town) | Local per-release cache (F6) |

## 6. Remaining blockers

| Blocks | Item |
|---|---|
| Phase 1 start | Approval of the foundation decisions F1–F11 (ROADMAP.md §6) and the phase sequence (§2) |
| Merging PR #1 | Your code review; Windows test run (V12) |
| OSM source only | Geofabrik reachability from a normal connection (V4). Overture remains the primary discovery source, so this does not block Phase 1 overall. |
