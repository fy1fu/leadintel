# Consolidated data model (v0.3)

PostgreSQL + PostGIS, managed by Alembic from Phase 1. This merges the v0.1 schema, the v0.2 additions and the v0.3 requirements, updated with the 2026-11-03 decisions.

**Network diagnostics are not part of this database.** Their tables (`net_probe_result`, `net_event`) live in their own store (`LEADINTEL_NETDIAG_DB_URL`, default SQLite), so connectivity can be diagnosed when this database is down.

Legend: 📋 already designed · 🆕 new in v0.3 · ✅ exists in PR #1

## 1. Sources and provenance

| Table | Status | Key columns / notes |
|---|---|---|
| `source` | 📋 + 🆕 | `key`, `cost_class`, `licence`, `attribution`, `enabled`. **New:** `reliability` (default per field group, from `config/sources.yaml`), `reliability_notes` |
| `source_record` | 📋 + 🆕 | `source`, `external_id`, `fetched_at`, `payload`, `payload_hash`, `licence_tag`. **New:** `raw_line` (bytes), `file_name`, `line_no`, `file_sha256`, so every Sunbiz/DBPR lead reproduces its original line. |
| `source_file` | 🆕 | `source`, `kind` (`cor` / `cor_event` / `fic` / `fic_event` / `quarterly` / `dbpr` …), `logical_date`, `remote_path`, `size`, `remote_last_modified`, `sha256`, `status` (`verified` / `failed` / `superseded`), `records`, `quarantined` |
| `source_date_ledger` | 🆕 | `source`, `kind`, `date`, `state` (`verified` / `not_yet_published` / `absent_holiday` / `absent_unexplained` / `failed`), `checked_at`. Drives coverage for date filters. |
| `quarantine_record` | 🆕 | `source_file_id`, `line_no`, `raw_line`, `reason`, `field` |
| `fact` | 📋 + 🆕 | Per-field provenance: `provenance` ∈ directory / website / registry / inferred / **manual** 🆕; `observed_at`, `fetched_at`, `last_verified_at`, `confidence`. **New:** `verification_state` ∈ `ingested` / `corroborated` / `verified` / `conflicting` / `stale`, and `ttl_days`. Ingestion alone never sets `verified`. |
| `coverage` | 📋 | Source × area × filter → `complete` / `partial` / `failed` / `not_run` / `unsupported` |
| `net_probe_result`, `net_event` | ✅ | From PR #1. **Stay in the separate diagnostics store; not migrated here.** |
| `cost_ledger` | 📋 | Every provider call, including $0 ones; paid approvals |

## 2. Businesses

| Table | Status | Notes |
|---|---|---|
| `organization` | 📋 + 🆕 | Legal entity. **New:** `filing_kind` (corporation / llc / lp / nonprofit / foreign_* / fictitious_name / general_partnership), `filing_type_code` (raw, e.g. `FLAL`), `document_number`, `registration_date`, `status`, `status_changed_at` |
| `organization_link` | 🆕 | `from_org`, `to_org`, `kind` (`dba_owned_by`, `successor_of`, `same_entity_as`), `evidence` (e.g. `own_charter = L26…`), `confidence` |
| `person_role` | 🆕 | Officer / owner / registered agent. Name kept only as needed for linking; **not surfaced as a lead contact**. |
| `address` | 🆕 | `owner_type`, `owner_id`, `role` (`principal` / `mailing` / `registered_agent` / `owner` / `listing`), normalized fields, `geom`, `county_fips`, `geocode_method`, `is_operating_location` (false for RA and mass-filer addresses), `shared_count` |
| `location` | 📋 + 🆕 | The prospect unit: `display_name`, `geom`, `county_fips`, `place_fips`, `cbsa`, `industry_profile`, `chain_flag`, `operating_status`, `first_seen_at`. **New:** `completeness` (missing / unverified / conflicting key fields) |
| `target_area` | 📋 + 🆕 | Configured targets from `config/areas.yaml` (initially Duval, St. Johns, Clay counties): type (county / city / ZIP / radius / polygon / state), geometry, enabled |
| `location_alias` | 🆕 | Trade names, DBA names, listing names |
| `location_source_link` | 📋 | `match_score`, `method`, `confirmed_by` |
| `review_queue` | 📋 | Merge / association decisions |
| `manual_observation` | 🆕 | User-entered research: `listing_url`, `platform` (google / facebook / yelp / nextdoor / other), `review_count`, `rating` (nullable), `observed_at`, `observed_by`, `verification_status` (`unverified` / `checked_twice` / `disputed`), `note`. Feeds `fact` with `provenance = manual`. **Never refreshed automatically**; the UI and exports label it *manually observed* and show its age. |

## 3. Online presence and websites

| Table | Status | Notes |
|---|---|---|
| `website`, `location_website` | 📋 | `association_confidence`, `evidence`, `website_status` (six values, SCORING.md §1.1) |
| `social_profile` | 📋 + 🆕 | **New:** `platform` enum (facebook, instagram, tiktok, linkedin, nextdoor, linktree, yelp, google_listing, other), `source`, `verified_at` |
| `presence` | 🆕 | `location_id`, `presence_type` (SCORING.md §1.2), `computed_at`, `inputs` |
| `crawl`, `page`, `measurement`, `assessment` | 📋 | v0.1 |

## 4. Targeting and scoring

| Table | Status | Notes |
|---|---|---|
| `config_version` | 📋 + 🆕 | Content hash of `scoring.yaml`, `keywords.yaml`, `industries/*`. **New:** `proposed_by` (user / outcome-analysis), `approved_by`, `approved_at`, `supersedes`, `reverted_at`. |
| `industry_profile` | 📋 + 🆕 | One file per niche with `targeting` (enabled, priority, geography), `keywords`, `benchmark` (archetype, features, rubric anchors, value tier). |
| `keyword_hit` | 🆕 | `location_id` / `organization_id`, `group`, `term`, `matched_text`, `field`, `effect` |
| `score` | 📋 | `kind` (wqs / bos / lps), `value`, `confidence`, `config_version`, `computed_at`; history kept |
| `score_factor` | 🆕 | One row per factor: `component`, `factor`, `effect`, `evidence_ref`, `provenance` |
| `benchmark_set`, `peer` | 📋 | Per (profile, market) |
| `gap_item` | 🆕 | `location_id`, `feature`, `peer_share` (e.g. 0.75 of peers have a quote form), `prospect_has` (bool / unknown) |

## 5. Campaigns, CRM, packages, revenue

| Table | Status | Notes |
|---|---|---|
| `campaign` | 🆕 | `name`, `industry_profile`, `area_id`, `target_size` (default 25), `status`, `created_at` |
| `campaign_member` | 🆕 | `campaign_id`, `location_id`, `rank`, `included_reason`, `score_snapshot` (frozen) |
| `lead` | 📋 → 🆕 | **Status set replaced:** new, qualified, researching, ready_to_contact, contacted, follow_up_required, replied, interested, meeting_scheduled, proposal_sent, negotiating, won, lost, not_interested, do_not_contact. Mapping from v0.1: `saved` → `qualified`, `reviewed` → `researching`, `contacted` → `contacted`, `interested` → `interested`, `not_interested` → `not_interested`, `excluded` → `do_not_contact` (with reason). |
| `contact_event` | 🆕 | `lead_id`, `channel` (phone / email / mail / in_person), `direction`, `occurred_at`, `summary`, `outcome`, `notes`, `logged_by`. **Manual logging only; there is no sending, dialing or messaging integration.** |
| `follow_up` | 🆕 | `lead_id`, `due_at`, `action`, `done_at` |
| `do_not_contact` | 🆕 | `location_id` / phone / email / address, `reason`, `source`, `added_at`. Enforced before any contact can be logged as planned. |
| `package`, `package_item` | 🆕 | User-defined: setup price, hosting / month, maintenance / month, included updates, add-ons, estimated cost. No defaults hard-coded. |
| `deal` | 🆕 | `lead_id`, `package_id`, `proposal_amount`, `expected_value`, `actual_value`, `mrr`, `status`, `won_at` / `lost_at`, `reason_lost` |
| `authorization_record` | 🆕 | e.g. Google Business Profile owner authorization: who, when, method. Required before such package items can be marked delivered. |
| `outcome_snapshot` | 🆕 | Frozen attribution at first contact (industry, source, presence type, scores, config version) for conversion analytics |
