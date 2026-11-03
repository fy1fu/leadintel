# Consolidated data model (v0.3)

PostgreSQL + PostGIS, managed by Alembic from Phase 1. This merges the v0.1 schema, the v0.2 additions and the v0.3 requirements, updated with the 2026-11-03 decisions.

**Network diagnostics are not part of this database.** Their tables (`net_probe_result`, `net_event`) live in their own store (`LEADINTEL_NETDIAG_DB_URL`, default SQLite), so connectivity can be diagnosed when this database is down.

Legend: 📋 already designed · 🆕 new in v0.3 · ✅ exists in PR #1 · 🔧 added in the 2026-11-03 architecture review

## 0. Conventions (🔧, fixed now to avoid later migrations)

- **Keys:** surrogate `bigint` identity keys. Natural keys get unique constraints, e.g. `(source, external_id)` and `(source, document_number)`, so re-imports upsert instead of duplicating.
- **Time:** `timestamptz` in UTC for every event and observation. Source-native dates (filing dates, licence dates) stay `date`.
- **Geometry:** EPSG:4326 `geometry`; distance queries cast to `geography`.
- **Append-only history:** `source_record`, `fact`, `score`, `score_factor`, `manual_observation`, `contact_event`, `lead_status_history`, `merge_event`, `config_version`. Corrections add rows; they never overwrite.
- **Soft retraction:** user-entered rows are retracted with a reason and timestamp, never hard-deleted.
- **Actor columns** (`created_by`, `logged_by`, `observed_by`) exist from the start, even though v1 is single-user.
- **Contact points:** phone numbers in E.164, emails lower-cased, postal addresses normalized, so do-not-contact matching is exact.

## 1. Sources and provenance

| Table | Status | Key columns / notes |
|---|---|---|
| `source` | 📋 + 🆕 | `key`, `cost_class`, `licence`, `attribution`, `enabled`. **New:** `reliability` (default per field group, from `config/sources.yaml`), `reliability_notes` |
| `source_record` | 📋 + 🆕 | `source`, `external_id`, `fetched_at`, `payload`, `payload_hash`, `licence_tag`. **New:** `raw_line` (bytes), `file_name`, `line_no`, `file_sha256`, so every Sunbiz/DBPR lead reproduces its original line. |
| `source_file` | 🆕 | `source`, `kind` (`cor` / `cor_event` / `fic` / `fic_event` / `quarterly` / `dbpr` …), `logical_date`, `remote_path`, `size`, `remote_last_modified`, `sha256`, `status` (`verified` / `failed` / `superseded`), `records`, `quarantined` |
| `source_date_ledger` | 🆕 | `source`, `kind`, `date`, `state` (`verified` / `not_yet_published` / `absent_holiday` / `absent_nonworkday` / `absent_unexplained` / `failed`), `checked_at`. Drives coverage for date filters; `absent_unexplained` and `failed` make coverage `partial`. |
| `quarantine_record` | 🆕 | `source_file_id`, `line_no`, `raw_line`, `reason`, `field` |
| `fact` | 📋 + 🆕 | Per-field provenance: `provenance` ∈ directory / website / registry / inferred / **manual** 🆕; `observed_at`, `fetched_at`, `last_verified_at`, `confidence`. **New:** `verification_state` ∈ `ingested` / `corroborated` / `verified` / `conflicting` / `stale`, and `ttl_days`. Ingestion alone never sets `verified`. |
| `area` | 📋 + 🔧 | **One table** for saved search areas and campaign targets (replaces the separate v0.1 `search_area` and v0.3 `target_area`): `kind` (county / city / ZIP / radius / polygon / state / multi), `spec`, `geom`, `is_target`, `enabled`. Seeded from `config/areas.yaml` (Duval, St. Johns, Clay). |
| `discovery_run` | 📋 (v0.1) | `area_id`, filters, sources, `status`, per-source counts, `started_at` / `finished_at` |
| `coverage` | 📋 | `discovery_run_id`, source × area cell × filter → `complete` / `partial` / `failed` / `not_run` / `unsupported`, with a reason |
| `net_probe_result`, `net_event` | ✅ | From PR #1. **Stay in the separate diagnostics store; not migrated here.** |
| `cost_ledger` | 📋 | Every provider call, including $0 ones; paid approvals |

## 2. Businesses

| Table | Status | Notes |
|---|---|---|
| `organization` | 📋 + 🆕 | Legal entity. **New:** `filing_kind` (corporation / llc / lp / nonprofit / foreign_* / fictitious_name / general_partnership), `filing_type_code` (raw, e.g. `FLAL`), `document_number`, `registration_date`, `status`, `status_changed_at` |
| `location_organization` | 🔧 | Many-to-many between a prospect location and the legal entities behind it: `role` (`legal_owner` / `trade_name` / `franchise_parent`), `evidence`, `confidence`. This is how *LLC + DBA + map listing = one business* is stored. |
| `organization_link` | 🆕 | `from_org`, `to_org`, `kind` (`dba_owned_by`, `successor_of`, `same_entity_as`), `evidence` (e.g. `own_charter = L26…`), `confidence` |
| `person_role` | 🆕 | Officer / owner / registered agent. Name kept only as needed for linking; **not surfaced as a lead contact**. |
| `address` | 🆕 | `owner_type`, `owner_id`, `role` (`principal` / `mailing` / `registered_agent` / `owner` / `listing`), normalized fields, `geom`, `county_fips`, `geocode_method`, `is_operating_location` (false for RA and mass-filer addresses), `shared_count` |
| `location` | 📋 + 🆕 | The prospect unit: `display_name`, `geom`, `county_fips`, `place_fips`, `cbsa`, `industry_profile`, `chain_flag`, `operating_status`, `first_seen_at`. **New:** `completeness` (missing / unverified / conflicting key fields) |
| `location_alias` | 🆕 | Trade names, DBA names, listing names |
| `location_source_link` | 📋 | `match_score`, `method`, `confirmed_by` |
| `review_queue` | 📋 | Merge / association decisions |
| `merge_event` | 🔧 | Audit of every merge and split: who, when, which records, score/method. Merges are reversible because canonical rows are rebuilt from linked source records. |
| `manual_observation` | 🆕 + 🔧 | User-entered research: `listing_url`, `platform` (google / facebook / yelp / nextdoor / other), `review_count`, `rating` (nullable), `observed_at`, `observed_by`, `verification_status` (`unverified` / `reverified` / `disputed`), `note`. **Append-only:** a re-check adds a new row with `reverifies_id` pointing at the earlier one; nothing is overwritten. Mistakes are retracted (`retracted_at`, `retraction_reason`), never deleted. The full history stays available for audit. *Current* means the latest non-retracted row per (location, platform); it counts toward scores only while ≤ 180 days old. **Never refreshed automatically**; the UI and exports label it *manually observed* and show its age. |

## 3. Online presence and websites

| Table | Status | Notes |
|---|---|---|
| `website`, `location_website` | 📋 | `association_confidence`, `evidence`, `website_status` (six values, SCORING.md §1.1) |
| `online_profile` | 📋 + 🔧 | Replaces `social_profile`. `platform` (facebook, instagram, tiktok, linkedin, nextdoor, linktree, google_listing, yelp, other) **plus `platform_kind`** (`social` / `link_hub` / `listing`). Listings such as Google or Yelp never count toward social-only presence. Also `url`, `source`, `verified_at`, `verification_state`. |
| `presence` | 🆕 | `location_id`, `presence_type` (SCORING.md §1.2), `computed_at`, `inputs` |
| `crawl`, `page`, `measurement`, `assessment` | 📋 | v0.1 |

## 4. Targeting and scoring

| Table | Status | Notes |
|---|---|---|
| `config_version` | 📋 + 🆕 | Content hash of `scoring.yaml`, `keywords.yaml`, `industries/*`. **New:** `proposed_by` (user / outcome-analysis), `approved_by`, `approved_at`, `supersedes`, `reverted_at`. |
| `industry_profile` | 📋 + 🆕 | One file per niche with `targeting` (enabled, priority, geography), `keywords`, `benchmark` (archetype, features, rubric anchors, value tier). |
| `keyword_hit` | 🆕 | `location_id` / `organization_id`, `group`, `term`, `matched_text`, `field`, `effect` |
| `score` | 📋 + 🔧 | `kind` (wqs / bos / lps), `value`, `confidence`, `config_version`, `computed_at`; history kept. **New:** `stage` (`provisional` before website research is complete, `final`), `evidence_points`, `caps_applied` (list, e.g. `evidence_gate:35`), `pre_cap_value`. |
| `score_factor` | 🆕 | One row per factor: `component`, `factor`, `effect`, `evidence_ref`, `provenance` |
| `benchmark_set`, `peer` | 📋 | Per (profile, market) |
| `gap_item` | 🆕 | `location_id`, `feature`, `peer_share` (e.g. 0.75 of peers have a quote form), `prospect_has` (bool / unknown) |

## 5. Campaigns, CRM, packages, revenue

| Table | Status | Notes |
|---|---|---|
| `campaign` | 🆕 | `name`, `industry_profile`, `area_id`, `target_size` (default 25), `status`, `created_at` |
| `campaign_member` | 🆕 | `campaign_id`, `location_id`, `rank`, `included_reason`, `score_snapshot` (frozen) |
| `lead` | 📋 → 🆕 + 🔧 | One per location the user is pursuing: `status`, `next_action`, `next_action_due`, `first_contacted_at` (derived from contact events), `do_not_contact` (derived). |
| `lead_status_history` | 🔧 | Every status change with timestamp and actor. Needed for time-to-close and funnel analytics. |
| `lead_note` | 📋 (v0.1) | Free-text notes |
| `lead_list`, `lead_list_item` | 📋 (v0.1) | Custom user lists, separate from campaigns |
| `lead` statuses | 📋 → 🆕 | **Status set replaced:** new, qualified, researching, ready_to_contact, contacted, follow_up_required, replied, interested, meeting_scheduled, proposal_sent, negotiating, won, lost, not_interested, do_not_contact. Mapping from v0.1: `saved` → `qualified`, `reviewed` → `researching`, `contacted` → `contacted`, `interested` → `interested`, `not_interested` → `not_interested`, `excluded` → `do_not_contact` (with reason). |
| `contact_event` | 🆕 | `lead_id`, `channel` (phone / email / mail / in_person), `direction` (outbound / inbound), `occurred_at`, `summary`, `outcome` (no_answer / left_message / replied / interested / not_interested / bounced / returned_mail / meeting_booked / other), `contact_point_id`, `notes`, `logged_by`. **Manual logging only; there is no sending, dialing or messaging integration.** |
| `follow_up` | 🆕 | `lead_id`, `due_at`, `action`, `done_at` |
| `contact_point` | 🔧 | Normalized phone / email / postal address, linked to locations through `fact` |
| `do_not_contact` | 🆕 | `location_id` or `contact_point_id`, `reason`, `source`, `added_at`, `added_by`. **Enforcement:** a DNC match blocks scheduling follow-ups, adding to campaigns, logging *outbound* contact, and exporting that contact point. Inbound contact can still be logged, because the business may contact you. |
| `package`, `package_item` | 🆕 | User-defined: setup price, hosting / month, maintenance / month, included updates, add-ons, estimated cost. No defaults hard-coded. |
| `deal` | 🆕 | `lead_id`, `package_id`, `proposal_amount`, `expected_value`, `actual_value`, `mrr`, `status`, `won_at` / `lost_at`, `reason_lost` |
| `authorization_record` | 🆕 | e.g. Google Business Profile owner authorization: who, when, method. Required before such package items can be marked delivered. |
| `outcome_snapshot` | 🆕 | Frozen attribution at first contact (industry, source, presence type, scores, config version) for conversion analytics |

## 6. As implemented in Phase 1 (migrations 0001-0002)

Implemented tables:
- **Sources and provenance:** `config_version`, `source`, `source_file`, `source_date_ledger`, `source_record`, `quarantine_record`, `fact`.
- **Geography and runs:** `area`, `discovery_run`, `coverage`, `geocode_cache`.
- **Organizations:** `organization`, `organization_link`, `person_role`, `address`.
- **Locations:** `location`, `location_alias`, `location_source_link`, `location_organization`, `online_profile`, `contact_point`, `location_contact`.
- **Resolution:** `review_queue`, `merge_event`.
- **Scoring:** `keyword_hit`, `score`, `score_factor`, `qualification`.

Differences from the design above, decided during implementation:
- `location_contact` links locations to normalized `contact_point`s (with source and verification state). `fact` rows keep the per-field provenance.
- `score.inputs_hash`: a score is re-written only when its inputs or config versions change, so history stays append-only without churn.
- `qualification` is its own append-only table (status + reasons + stage), separate from `score` (F8).
- Network diagnostics tables remain in the separate diagnostics store.
- CRM, campaign, package and deal tables are **not** created in Phase 1 (Phase 4–5).
