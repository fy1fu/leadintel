# LeadIntel consolidated roadmap (v0.3)

Status: **final architecture proposal, 2026-11-03.** Your decisions are recorded in §5 and the architecture review findings in [REVIEW.md](REVIEW.md); foundation decisions awaiting approval are in §6. No Phase 1 code has been written, and Phase 1 starts only after you approve the implementation sequence (§2). This merges three sets of requirements into one roadmap:
- the original brief (v0.1);
- the free-first architecture (v0.2, [PLAN.md](PLAN.md));
- the network layer in PR [fy1fu/leadintel#1](https://github.com/fy1fu/leadintel/pull/1), not yet merged;
- the v0.3 high-intent acquisition requirements.

Companion documents:
- [SCORING.md](SCORING.md): presence classification, keyword scoring, WQS / BOS / LPS.
- [DATA_MODEL.md](DATA_MODEL.md): consolidated schema.
- [SUNBIZ.md](SUNBIZ.md): verified Sunbiz formats.
- [NETWORK.md](NETWORK.md): network layer.
- [VERIFICATION.md](VERIFICATION.md): open verification items.

Status legend:

| Tag | Meaning |
|---|---|
| ✅ **Implemented** | Code and tests exist in PR #1 (not merged yet) |
| 📋 **Planned** | Designed in v0.1/v0.2, not implemented |
| 🆕 **New** | Required by v0.3; not in any earlier design |
| 🔭 **Optional** | Future enhancement; not on the critical path |

Where a v0.3 requirement *extends* a planned component, it is listed as 📋 + 🆕 with the delta spelled out. Nothing already designed is redesigned unless a v0.3 requirement or new evidence forces it; those cases are called out in §3.

---

## 1. Requirement-by-requirement classification

### 1.1 Sunbiz and fictitious-name (DBA) discovery — v0.3 §1

| Requirement | Status | Existing basis | Work remaining |
|---|---|---|---|
| Verified official layouts before parsing | ✅ (research) | `SUNBIZ.md` §4: corporate 1,440 B, fictitious name 2,098 B, both event files validated on real records | Close V1 (official corporate positions). The parser must re-validate every record. |
| Access path, credentials handling | ✅ | `sunbiz_https` probe; credentials only from env, leak-tested | — |
| Import daily corporate filings | 📋 | `PLAN.md` §3; `SUNBIZ.md` §5 | Build the adapter |
| Import daily fictitious-name filings | 📋 | Same | Build the adapter |
| Import event files (status changes, name changes, dissolutions) | 🆕 | Layouts validated | Needed for the "inactive" negative signal |
| Preserve filing date and entity type | 📋 + 🆕 | `organization` table (v0.1) | Add `filing_kind` (corporation / LLC / LP / non-profit / foreign / fictitious name / general partnership) mapped from filing-type codes (`FLAL`, `DOMP`, `FORL`, `DOMNP`, …), keeping the raw code |
| Address roles: principal / mailing / registered agent / owner | 🆕 | — | Typed `address` rows with a role. **The registered-agent address is never used as the operating location.** |
| County from the right address | 🆕 | — | Principal address → geocode (Census) → county FIPS. Fallbacks: ZIP→county (Census relationship file); the DBA `county` field only when it isn't `MULTIPLE`. **Principal-address state is blank in all daily corporate records** (`SUNBIZ.md`), so state comes from the ZIP. Every derivation is recorded with its method. |
| Registered-agent / mass-filer address detection | 📋 + 🆕 | v0.2 noise filter | Flag principal = RA address, and addresses shared by more than N filings, as `not_operating_location` |
| DBA ↔ owning entity link | 🆕 | `SUNBIZ.md`: all 706 sampled company owners carry a Sunbiz document number in `own_charter` | Exact join on document number. Person owners are kept as relationships without exposing personal data. |
| No double counting (LLC + DBA + map listing = one business) | 📋 | v0.2 §4 organization / location split + entity resolution | Add the DBA→entity edge and DBA name as alias in matching |
| Source records preserved and traceable | 📋 | `source_record` + `fact` provenance (v0.1/v0.2) | Raw fixed-width line stored with file name, line number and SHA-256 |
| Tolerate malformed records, encoding issues, partial downloads | 📋 + 🆕 | `SUNBIZ.md` §5 integrity rules; downloader primitives ✅ | Per-record quarantine table, byte-slice + per-field UTF-8 decode, NUL stripping, date ledger. No crash, no silent drop: quarantine counts appear in coverage. |
| DBA weighted above ordinary LLC | 🆕 | — | `entity_type_weights` in `scoring.yaml` (SCORING.md §3) |

### 1.2 Keyword filtering and scoring — v0.3 §2

| Requirement | Status | Basis / delta |
|---|---|---|
| Holding / SPV name down-ranking | 📋 | v0.2 §3 noise filter (regex idea) |
| Configurable positive/negative keyword groups, editable without code | 🆕 | `config/keywords.yaml`, versioned (SCORING.md §2) |
| Word-boundary, phrase-aware matching (`pressure wash` ≠ `prewash`; `HVAC` case-insensitive; `detail` as a word) | 🆕 | Token-based matcher with stemming variants (`wash`/`washing`); tests for false positives |
| Penalties, not automatic exclusion; record which keywords fired | 🆕 | A `keyword_hit` row per match, shown in the lead profile |
| User-configurable exclusions | 🆕 | `hard_exclude` list, separate from penalties, with an exclusion reason stored |

### 1.3 Established businesses without an independent website — v0.3 §3

| Requirement | Status | Basis / delta |
|---|---|---|
| Don't make new registrations the primary source | 📋 | v0.2 §3: Overture/OSM/DBPR are primary for operating businesses |
| Website classification (no website / inaccessible / uncertain / incomplete) | 📋 | v0.1 §4 + v0.2 §5 |
| Facebook-only, Instagram-only, social-only, directory-only presence | 📋 + 🆕 | v0.2 had a single "social-only" flag. v0.3 needs a separate **presence type** axis (SCORING.md §1). |
| "Reasonably good" vs "outdated" website | 📋 | WQS bands (v0.1) mapped to presence categories |
| Free evidence that a business is established | 🆕 | Overture multi-source confirmation and open status, DBPR licence original-issue date, BTR, Sunbiz formation date, Wayback first-seen, social profile presence. **Verified 2026-11-02:** Overture lists social-only or no-web/no-social businesses in every target niche, nearly all with phones (SCORING.md §1.3). |
| Listing with reviews, review count and rating | 🆕 (manual) + 🔭 (paid) | **Not available from any free source we use**; Overture has no review fields (verified). **Decided:** manual observations. Each one stores listing URL, platform, review count, rating (optional), observation date, who observed it and a verification status. It is labelled *manually observed* everywhere it appears and **is never refreshed automatically**; it shows its age ("observed 41 days ago"). **History is append-only:** re-checks add rows, and after 180 days only the review evidence stops counting (other activity evidence is unaffected) while the original stays available for audit. Google Places stays optional (§1.11). Scraping Google or Yelp is excluded (terms of service). |

### 1.4 Scoring model — v0.3 §4

| Requirement | Status | Basis / delta |
|---|---|---|
| Separate WQS / BOS / LPS | 📋 | v0.1 §6.5 / §7, restored in SCORING.md |
| v0.3 positive and negative signals | 📋 + 🆕 | BOS components kept. New: online-presence signals, DBA / recent licence / BTR signals, service-area breadth, missing contact or quote paths, franchise detection, **operating-evidence gate** |
| "No website" alone never yields a high score | 🆕 | Operating-evidence gate caps BOS without evidence (SCORING.md §4.3) |
| Invariant: new LLC without evidence < established business with 40 reviews and no website | 🆕 | Encoded as an automated scoring test (SCORING.md §7) |
| Every factor visible in the lead profile | 📋 + 🆕 | v0.1 explanation panel → itemized factor ledger with evidence links |

### 1.5 Industry and niche targeting — v0.3 §5

| Requirement | Status | Basis / delta |
|---|---|---|
| Industry profiles for *website benchmarking* (archetypes, required/expected features, rubric anchors) | 📋 | v0.1 §6.3 |
| Industry profiles for *targeting* (enable/disable, priority, keywords, geography) | 🆕 | **Merged into the same profile file**: one `config/industries/*.yaml` per niche with `targeting:`, `keywords:`, `benchmark:` sections. Not a second system. |
| Initial niches (home services, mobile/automotive, marine/coastal) | 🆕 | Seed profiles with taxonomy mappings to Overture categories, OSM tags, DBPR licence types and BTR occupation codes |
| Compare lead quality by industry; industries with most no-website businesses; highest average BOS | 📋 + 🆕 | v0.2 coverage dashboard has industry coverage. Add opportunity aggregates per industry. |
| Track replies and sales by industry; learn conversion rates | 🆕 | Depends on CRM (§1.9) and analytics (§1.10) |
| Industry priority (user-assigned) | 🆕 + 🔧 | Proposed (§6 F9): orders discovery work, default list sorting and campaign tie-breaks. It **does not change BOS or LPS**, which describe the business, not the user's preference. |
| **Niche comparison report** (decided: no niche fixed yet; cleaning and landscaping first candidates) | 🆕 | Side-by-side per niche and area: businesses discovered; no independent website; social-only; outdated website; qualified prospects; average BOS and LPS; contact availability (phone / email / form / mail-only); confidence and source completeness. **No automatic winner and no ranking by size.** Each metric shows its coverage, so a niche with incomplete discovery isn't compared as if it were complete. Counts become available in stages: discovery in Phase 1, presence in Phase 2, scores in Phase 3. |

### 1.6 First-campaign workflow — v0.3 §6

| Requirement | Status | Basis / delta |
|---|---|---|
| Lead lists, saving, CSV/XLSX export | 📋 | v0.1 §10 / v0.2 Phase 1 |
| Campaign = niche + area + target size (20–30) + pipeline run + ranked shortlist | 🆕 | `campaign` entity and a guided workflow screen |
| Campaign-ready export columns | 🆕 | Fixed export schema including reasons, competitor comparison, improvements, citations and timestamps. Missing contact data is exported as empty with a reason, never inferred. |

### 1.7 Competitor-based opportunity analysis — v0.3 §7

| Requirement | Status | Basis / delta |
|---|---|---|
| Comparable local peers per (profile, market), excluding national chains | 📋 | v0.1 §6.4 benchmark sets |
| Search visibility "where measurable" | 🆕 (limited) | No free search-ranking data source exists in the free-first design. Only on-site SEO signals (titles, schema, local pages, indexability) are measured. Ranking positions are not reported. |
| Feature gap: what competitors offer that the prospect lacks (quote form, service areas, gallery, reviews, CTAs, mobile) | 📋 + 🆕 | Peer feature matrix existed. Add a prospect-facing **gap table**. |
| Competitors' presence when the prospect has no website | 🆕 | Peer presence distribution, e.g. "18 of 24 local pressure-washing businesses have their own site" |
| No claims of specific revenue increases | 🆕 | Report wording rule plus a test: gap statements are factual comparisons only |

### 1.8 v0.3 §8 and website generation

Section 8 arrived redacted and nothing has been assumed for it. **Decided: website generation and mockup functionality are out of scope.** They are neither planned nor optional features. The analysis still produces *recommended website improvements* and *competitor gap tables* (descriptions, not generated sites).

### 1.9 Service packages, recurring revenue and CRM — v0.3 §9–10

| Requirement | Status | Basis / delta |
|---|---|---|
| Basic lead statuses, notes, lists | 📋 | v0.1 lead management |
| Full pipeline statuses (New lead … Do not contact), contact history, follow-ups, next action, proposal / expected / actual value, MRR, reason lost | 🆕 | Replaces the v0.1 status set; old values map to new ones (DATA_MODEL.md §5) |
| Manually tracked channels: phone, email, physical mail, in-person visit | 🆕 | `contact_event` log, follow-up dates, notes, outcomes. **Decided: manual outreach only.** No automated bulk email, automated calls or unsolicited messaging, and no sending integrations. |
| Outreach compliance safeguards | 🆕 | Do-not-contact list enforced everywhere (blocks follow-ups, campaign inclusion, outbound logging and export of that contact point); per-channel basis notes; contact-frequency reminders. Phone outreach shows warnings about the federal TCPA, the Florida Telephone Solicitation Act and the National Do Not Call Registry; email shows CAN-SPAM. **Text messaging is not a supported channel.** These are product safeguards, not legal advice. |
| Configurable service packages (one-page site, hosting, maintenance, GBP setup, SEO …) and pricing | 🆕 | `package` / `package_item`; no hard-coded prices |
| GBP setup requires owner authorization | 🆕 | A package item cannot be marked delivered without a recorded owner-authorization note |
| Estimated cost, profit, MRR | 🆕 | Derived from packages and deals |

### 1.10 Acquisition performance and outcome learning — v0.3 §11

| Requirement | Status | Basis / delta |
|---|---|---|
| Funnel metrics (discovered → qualified → contacted → replied → meeting → proposal → won), revenue, MRR, average sale, time to close | 🆕 | Computed from CRM tables |
| Conversion by industry, lead source, presence category | 🆕 | Needs attribution fields frozen at contact time (score snapshot) |
| Outcome-based scoring improvement, inspectable and reversible | 📋 + 🆕 | Scores already store `config_version` (v0.1). New: proposed weight changes are generated as a diff with the evidence, **applied only after user approval**, one-click revert. Small-sample guard: no proposal below a minimum number of outcomes per segment. |

### 1.11 Google Places (optional) — v0.3 §12

| Requirement | Status | Basis / delta |
|---|---|---|
| Disabled paid adapter, blocked by cost guard | ✅ (guard) / 📋 (adapter) | `paid_providers` in `network.yaml`; `CostGuard` tested; adapter is a stub in the plan |
| Field-level billing | ✅ (partly verified) | Google docs, checked 2026-11-02: `websiteUri`, `nationalPhoneNumber`, `rating`, `userRatingCount` are **Enterprise** fields; Text Search returns at most 60 results per query |
| Pricing, storage/caching terms, map-display terms, full-area cost estimate, explicit budget + approval | 🔭 | Pre-enable checklist (§4); prices re-verified at that time, never assumed |

### 1.12 Cross-cutting (unchanged requirements, current status)

| Capability | Status | Notes |
|---|---|---|
| Network registry, staged diagnostics, required/optional classification | ✅ | PR #1 |
| Overture three-mirror failover + bounding-box reader | ✅ / 📋 | Reader done; normalizing adapter and local per-release cache planned (§6 F6) |
| Partial-download recovery, MD5/size integrity, atomic install (OSM) | ✅ | PR #1 |
| Paid-provider blocking | ✅ | PR #1; every outbound path goes through the cost guard |
| SSRF-safe crawling client | ✅ / 📋 | Client done; crawler (robots.txt, page selection) planned |
| Explicit incomplete-search / coverage states | ✅ / 📋 | Done in diagnostics and OSM planning; coverage records per discovery run planned |
| Map (MapLibre + OpenFreeMap), area search, dashboard | 📋 | |
| DBPR, BTR, NPI adapters | 📋 | Endpoints verified. Duval's report portal is behind a bot challenge. St. Johns and Clay are under investigation (non-blocking); a **generic BTR manual-CSV importer** covers any county |
| Local AI provider (Ollama) / `none` provider | 📋 | |
| Coverage dashboard | 📋 | |
| Configurable geography: counties, cities, ZIP codes, radius, polygons, other states | 📋 + 🆕 | v0.2 area resolver on TIGER boundaries. **Decided initial targets: Duval, St. Johns and Clay counties.** Targets are configuration (`config/areas.yaml`), not code; adding a Florida county is config plus any county-specific record adapter (e.g. BTR). Other states need their own registry adapters (Sunbiz is Florida-only); Overture, OSM, NPI and Census work nationwide. |
| Network diagnostics runnable without the main database | ✅ | Own store (`LEADINTEL_NETDIAG_DB_URL`), memory fallback, `--no-store`; tested |

### 1.13 Data quality (emphasized in the 2026-11-03 decisions)

| Requirement | Status | Design |
|---|---|---|
| Successful ingestion ≠ verified business information | 🆕 | Each fact carries a **verification state**: `ingested` (one source) → `corroborated` (≥ 2 independent sources agree) → `verified` (confirmed on the business's own site or by the user) → `conflicting` / `stale`. The UI and exports show the state next to every contact field. An import's success counts are reported separately from verified-field counts. |
| Source reliability | 📋 + 🆕 | Per-source reliability defaults in `config/sources.yaml` (e.g. registry filing fields high; listing websites medium until verified; inferred fields low). They are used in field confidence and documented, not hidden. |
| Field-level confidence and timestamps | 📋 | `fact` rows: `observed_at`, `fetched_at`, `last_verified_at`, `confidence`, provenance |
| Incomplete records tracked | 🆕 | `completeness` per location (which key fields are missing, unverified or conflicting). Quarantined source lines are counted per import. Incomplete records stay visible but are labelled; they are never silently dropped or padded with guesses. |
| Staleness | 📋 + 🆕 | Per-field TTLs drive refresh; facts past their TTL become `stale` and lower LPS actionability. Manual observations are never auto-refreshed (§1.3). |

---

## 2. Revised phases

The v0.3 priority order is adopted. Each phase ends with real-data acceptance tests and a short report. Items carried over from v0.2 phases are marked (v0.2).

### Phase 1 — Discovery and qualification
1. **Foundation** (v0.2):
   - Postgres + PostGIS + Alembic for the **business** database. Network diagnostics keep their own independent store (PR #1) and are not migrated into it.
   - `config/areas.yaml` with Duval, St. Johns and Clay.
   - TIGER boundaries; area resolver.
   - `SourceAdapter` interface with coverage records; provenance `fact` model; cost guard wiring.
2. **Sunbiz adapter** (corporate + fictitious name + both event files):
   - date ledger and validating parser with quarantine;
   - filing-kind mapping and address roles;
   - registered-agent / mass-filer detection;
   - DBA → entity linking.
3. **Overture adapter**: normalize (empty-string websites, phone E.164, social/listing URL classification) on top of the ✅ reader, plus a **local per-release cache** of the target counties so searches don't re-read remote files.
4. **DBPR adapter** (licence CSVs). **BTR:** a generic manual-CSV importer for any county; automated Duval import where obtainable (the portal challenge is never bypassed); St. Johns and Clay investigated in parallel, **not blocking Phase 1**.
5. **Entity resolution v1**: organization / location / alias, `location_organization` and DBA edges, review queue, reversible merges (`merge_event`).
6. **Keyword engine + industry targeting v1**: `keywords.yaml`, `industries/*.yaml` with the initial niches.
7. **County filtering, CSV export, minimal map + list UI** (v0.2), so results are inspectable.
8. **Data-quality layer**: fact verification states, source reliability, completeness flags, quarantine counts in coverage.
   - Import success counts and verified-field counts are always reported separately.
   - The provisional scoring pipeline (SCORING.md §4.4) with the evidence cap runs from Phase 1, so no early ranking can bypass the cap.
9. **Niche comparison report v1** (discovery-level metrics for cleaning and landscaping across the three counties).

**Acceptance criteria:**
- Duval, St. Johns and Clay imports reproduce every source line from the database.
- A rerun creates 0 duplicates.
- A DBA owned by an LLC shows as one business with both filings.
- Registered-agent addresses never appear as operating locations.
- A day with a missing file shows coverage `partial`, never "no new businesses".
- Malformed lines land in quarantine.
- The stacked-signal cap test (SCORING.md §7, invariant 11) passes on provisional scores.
- Import success counts are never presented as verified-business counts.
- Network diagnostics still run with the business database stopped.
- 0 paid calls.

### Phase 2 — Website and online-presence research
- Website candidate gathering and association confidence (v0.2 §5).
- **Presence classification** (two axes, SCORING.md §1): Facebook / Instagram / social / directory-only.
- Polite crawler on the ✅ safe-egress client: robots.txt, page selection, Lighthouse, axe.
- WQS with the `none` provider (v0.2).
- **Manual observations** for listing / review data: labelled, dated, never auto-refreshed.
- Niche comparison gains presence metrics (no website, social-only, outdated).
- Website discovery confidence and the "investigation incomplete" state.

**Acceptance:** association precision ≥ 0.97 on a labelled sample; no Facebook URL ever counted as an independent website; fixture sites A/B/B′/C scored as specified.

### Phase 3 — Scoring and competitor analysis
- Industry benchmark profiles (v0.1).
- Peer selection and the gap table.
- BOS / LPS with the factor ledger and operating-evidence gate.
- Optional local AI (Ollama) for subjective criteria, behind calibration.

**Acceptance:**
- Scoring invariants (SCORING.md §7) pass, **including the explicit test that a new LLC with a recent filing and no trading evidence never outranks an established business**.
- Repeat scoring is identical for unchanged inputs.
- Every score displays its factors and evidence.
- The niche comparison gains average BOS / LPS and qualified-prospect counts.

### Phase 4 — Sales workflow
- Campaigns and the first-campaign wizard (niche + area → 20–30 shortlist). The user chooses the niche using the comparison report.
- Campaign-ready export.
- CRM pipeline, contact history, follow-up dates, notes, outcomes.
- Do-not-contact enforcement across all four manual channels (phone, email, mail, in-person).
- No automated messaging of any kind.

### Phase 5 — Business performance
- Service packages and deals.
- Revenue / MRR reporting.
- Funnel and conversion analytics.
- Outcome-based weight proposals (approve / revert).

### Later / optional (🔭)
- Google Places adapter (after the §4 checklist).
- *Not planned at all:* website generation, mockups, automated outreach.
- Foursquare OS Places.
- Additional counties and states.
- Self-hosted PMTiles.
- Learning-to-rank once enough outcomes exist.

---

## 3. Design changes forced by v0.3 or by new evidence

| Change | Why |
|---|---|
| Industry targeting and website-benchmark profiles become **one** profile file per niche | Avoids two parallel industry systems |
| Website status and **presence type** become two separate axes (replacing the single social-only flag) | v0.3 needs Facebook-only vs Instagram-only vs directory-only, independent of website quality |
| v0.1 lead statuses are replaced by the v0.3 CRM pipeline | Superset; mapping in DATA_MODEL.md §5 |
| Chain detection no longer treats Overture `brand` presence as a chain signal | Verified: `brand` is present on 24,927 of 51,352 sampled places, but empty-named on 20,503; only 600 carry a Wikidata brand ID. Use Wikidata ID, recurring brand names and multi-location counts instead. |
| Reviews treated as unavailable by default | No free source we use provides them (verified); manual entry or the paid adapter only |
| BOS gains an **operating-evidence gate** | v0.3: "no website" must not produce a high score on its own |
| Corporate location from principal address + ZIP, not the state field | Principal state is blank in all daily records checked |
| Network diagnostics keep a separate store instead of moving into the business database | Decided 2026-11-03: diagnostics must work when the main database doesn't |
| Website generation / mockups removed from scope | Decided 2026-11-03 |
| No niche is fixed; a comparison report decides | Decided 2026-11-03 |

---

## 4. Google Places pre-enable checklist (🔭)

All steps must be completed and recorded before `allow_paid` can be set for `google_places`:

1. Re-verify current SKU prices and free-usage caps on Google's own pricing page (never assume a free allowance).
2. Re-verify field → SKU mapping. As of 2026-11-02, `websiteUri`, `nationalPhoneNumber`, `rating` and `userRatingCount` are Enterprise.
3. Re-verify storage and caching terms (which fields may be stored and for how long; `place_id` persistence).
4. Re-verify display terms (Google map requirement) and decide the map mode.
5. Run the **cost estimator**: tiles × categories × pages for the chosen area, both worst-case and expected.
6. Set a monthly budget; the cost guard hard-stops at the budget.
7. Record explicit user approval (who, when, amount) in the cost ledger.

---

## 5. Decisions

### Recorded (2026-11-03)

| # | Decision |
|---|---|
| D1 | Initial geography: **Duval, St. Johns and Clay** counties; geography stays configurable (counties, cities, ZIP codes, other states later). |
| D2 | **No niche fixed yet.** Cleaning and landscaping are the first candidates; choose after the comparison report on real discovery results. Size alone is not a criterion. |
| D3 | **Manual review observations: yes.** Labelled, dated, with a verification status, never auto-refreshed. |
| D4 | PR #1 may be merged **after code review and Windows testing**; secret audit done (clean). Phase 1 builds on the network layer and does not replace it. |
| D5 | v0.3 §8 redacted; **website generation and mockups are out of scope.** |
| D6 | **Manual outreach only:** phone, email, physical mail, in-person. Contact history, follow-ups, notes, outcomes, do-not-contact enforcement, pipeline. No automated bulk email, calls or unsolicited messaging. |
| D7 | Data quality: ingestion ≠ verification; source reliability, field confidence, timestamps and completeness tracked (§1.13). |
| D8 | BOS cap for records without trading evidence is preserved and tested explicitly. |
| D9 | Network diagnostics must run independently of the business database (implemented in PR #1). |
| D10 | Manual observations are append-only. After 180 days only the review evidence stops counting; history is kept for audit (2026-11-03). |
| D11 | St. Johns / Clay BTR availability **does not block Phase 1**; a manual CSV import covers any county (2026-11-03). |
| D12 | The imported-vs-verified distinction is kept everywhere: UI, exports, scores (LPS verification multipliers), and reports (2026-11-03). |
| D13 | The evidence cap applies after all positive filing, keyword and recency signals, as the last step of the one scoring pipeline (2026-11-03). |

### Still open

| # | Item |
|---|---|
| O1 | ~~Approval of foundation decisions and sequence~~ **Approved 2026-11-03.** Phase 1 in progress ([PHASE1.md](PHASE1.md)). |
| O2 | Code review and Windows test run of PR #1 (procedure in PR #1 and `SUNBIZ.md` §7). |
| O3 | Verification items in [VERIFICATION.md](VERIFICATION.md). None block Phase 1 except V4 (Geofabrik) for the OSM source specifically. |

---

## 6. Foundation decisions — **approved 2026-11-03**

F1–F11 and the five-phase sequence were approved, with these clarifications:
- **F1:** packaged defaults must always let the app start and recover when the DB is unavailable. Network settings stay independent. Scoring, geography, industry priorities and exclusions have version history; scoring-weight changes need approval and are reversible.
- **F2:** native Windows install, with setup, backup, restore and recovery instructions (`docs/WINDOWS_SETUP.md`).
- **F6:** Overture snapshots per release, with checksums, release metadata and safe cache cleanup; the same area is never downloaded twice.
- **F7:** Postgres job queue; network diagnostics stay independent.
- **F8:** qualification is **separate from ranking**. Qualified = meets the minimum evidence, opportunity, contactability, geography and exclusion rules; LPS ranks qualified prospects. A legitimate business mailing address counts as a contact route.

Phase 1 execution plan: [PHASE1.md](PHASE1.md).

| # | Decision | Recommendation | Why it matters |
|---|---|---|---|
| F1 | **Where user-editable configuration lives** (keywords, industries, scoring weights, areas) | Postgres `config_version` rows are the source of truth, each with full content, hash, author and approval. The YAML files in `config/` are **seed defaults** and an export/import format. `network.yaml` stays file-only, so diagnostics need no database. | The UI must edit keywords and weights, and outcome tuning must be approved and reverted. Making files canonical now would force a rewrite when UI editing arrives. |
| F2 | **Deployment on your Windows computer** | Native installs: PostgreSQL 16 + PostGIS (EDB installer / Stack Builder), Python 3.13, Node LTS; the app runs as local processes. Docker Compose stays optional. | Docker Desktop's terms make it free only for personal use and small businesses (under 250 employees *and* under $10M revenue) [secondary; re-check Docker's current terms], and it adds a VM layer. Native installs avoid both. Needs your confirmation of the target machine. |
| F3 | **Address normalization library** | `usaddress` (pure Python), plus Census geocoder results | libpostal needs a large C library that is difficult on Windows. Changing the normalizer later changes every match key. |
| F4 | **Users and access** | Single-user local app. The API binds to `127.0.0.1` only, actor columns exist (DATA_MODEL.md §0), and there is no login in v1. | Multi-user later is additive (auth layer) because actor columns already exist. Binding to localhost matters because the API has no authentication. |
| F5 | **Schema conventions** | As in DATA_MODEL.md §0: bigint keys + natural-key uniques, UTC `timestamptz`, EPSG:4326, append-only history tables, soft retraction | These are expensive to change once data exists |
| F6 | **Overture access pattern** | Per release, download only row groups intersecting the enabled target areas into a local GeoParquet cache; discovery queries the cache; refresh on new monthly releases | Avoids re-reading hundreds of MB per search; makes discovery repeatable and offline-capable |
| F7 | **Background jobs** | Procrastinate (Postgres-backed) for imports, crawling and scoring. Network diagnostics stay a plain CLI/API call outside the queue. | Keeps diagnostics independent (D9) |
| F8 | **"Qualified prospect" definition** | As in SCORING.md §6 (operating evidence ≥ 3, website-related opportunity, usable non-conflicting contact, in target area, not excluded) | The niche comparison and campaigns both depend on it |
| F9 | **Meaning of industry priority** | Orders work and default sorting only; **never changes BOS/LPS** | Prevents user preference from distorting opportunity scores |
| F10 | **Fact TTLs (staleness)** | Phone / email / website 90 days; operating status 60 days; registry status from events (daily); manual observations 180 days for scoring (never auto-refreshed) | Drives refresh work and LPS freshness |
| F11 | **Weights are starting points** | Ship the documented defaults; first calibration uses your labelled sample in Phase 3 | Avoids treating initial numbers as validated |
