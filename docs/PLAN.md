# LeadIntel: Architecture & Implementation Plan

Status: **v0.2: free-first architecture (2026-11-02).** This supersedes v0.1, which assumed Google Places, Google Maps and paid search APIs.

**Guiding constraint:** the application must be fully useful with **zero recurring charges and no paid API dependencies**.
- Paid integrations exist only as disabled-by-default adapters that a configuration change can enable later.
- Accuracy beats volume: fewer, well-verified leads are preferred over many mediocre ones.

Evidence tags:
- **[tested]**: exercised from the build environment with real data.
- **[verified]**: confirmed on the provider's own documentation.
- **[secondary]**: third-party sources only; re-verify before relying on it.
- **[unverified]**: not yet checked (often because the host is unreachable from the build sandbox).

---

## 1. What changed from v0.1, and why it still works

| Area | v0.1 | v0.2 (free-first) |
|---|---|---|
| Operating-business discovery | Google Places (paid) | **Overture Maps Places** + OpenStreetMap + state licence registers + county tax receipts + NPI registry. Optional later: Foursquare OS Places, Google Places (paid, off). |
| New-business discovery | Sunbiz | Sunbiz **plus county business-tax-receipt (BTR) "new receipts" reports**. A BTR is required to operate at a physical location, so it is a much stronger "actually opening" signal than an LLC filing. |
| Map | Google Maps | **MapLibre GL** + OpenFreeMap vector tiles, with a self-hosted Protomaps PMTiles fallback |
| Website discovery | Places field + paid search API | Listed-website fields from free sources + domain inference with DNS/HTTP verification + Common Crawl index + social-profile links. **No search-engine API.** |
| Geocoding | Census + Google coordinates | Census Geocoder (free, batch) + coordinates already present in Overture/OSM. Optional self-hosted Nominatim/Photon. |
| Performance audits | Lighthouse + PSI (Google key) | **Local Lighthouse only.** No Google key. |
| AI analysis | Claude API | **Pluggable provider. Default `none` (deterministic analysis only). Recommended free option `ollama` (local open vision model).** The Claude API is an opt-in paid provider with a hard budget cap; off by default. |
| Reviews / ratings | Google | **Not available for free in bulk.** We will not scrape Google/Yelp reviews (terms of service). This is an accepted coverage loss; viability uses other signals (§7). |

**Overture test result (2026-10-21 release, bounding box −81.75…−81.45, 30.15…30.40) [tested]:**
- 51,352 places, downloaded in ~3 s through anonymous S3 with a bounding-box filter, no key and no charge.
- Field coverage:

  | Field | Share of places |
  |---|---|
  | Website | 80% |
  | Phone | 89% |
  | Social link | 50% |
  | Email | 33% |
  | `brand` (useful for chain detection) | 49% |

- Operating status: open 37,340 / permanently closed 2,263 / unknown 11,749.
- Contributing sources: Meta, BrightQuery, Microsoft, Foursquare, AllThePlaces, others.
- Licences present: CDLA-Permissive-2.0, Apache-2.0, CC0. All allow storage and commercial use with attribution.

**Caveat:** Overture is a conflation of commercial feeds. Its "website" field can be stale or wrong, and absence of a website in Overture means nothing on its own. Every association still goes through verification (§5).

---

## 2. Cost inventory: every component that could generate a charge

Legend:
- 🟢 **Free**: no account, key or payment; fair use only.
- 🟡 **Free with limits or conditions**: free, but rate-limited, gated, donation-funded, or with data-use terms.
- 🔴 **Paid**: will not be used unless explicitly enabled.

| Component | Default choice | Class | Notes / limits |
|---|---|---|---|
| **Discovery: registry** | Florida Sunbiz bulk files (daily + quarterly, corporations and fictitious names) | 🟢 | Free public login. **Verified 2026-11-02**: served over HTTPS/WebDAV from `sftp.floridados.gov` (works from the sandbox), with SFTP on port 22 as a fallback. Formats and gap handling: [SUNBIZ.md](SUNBIZ.md). |
| **Discovery: places** | Overture Maps Places (AWS/Azure open data) | 🟢 | [tested]. Monthly releases. No egress charge to us (AWS Open Data programme). |
| Discovery: places | OpenStreetMap via Geofabrik extract (preferred) or Overpass | 🟢 / 🟡 | Free. The public Overpass instance is fair-use and rate-limited. Both were unreachable from this sandbox; they work from a normal internet connection. ODbL attribution. |
| Discovery: places | Foursquare OS Places | 🟡 | Apache-2.0, but the Hugging Face copy is **gated** (free account + access form) [secondary]. Optional adapter, off until you opt in. |
| Discovery: licences | Florida DBPR licensee downloads (construction, real estate, cosmetology, hotels & restaurants, etc.) | 🟢 | Free weekly CSVs under Florida's public-records law [secondary; per-board files to be confirmed]. |
| Discovery: licences | FDACS registrations (e.g. motor-vehicle repair, pest control) | 🟢 / 🟡 | [unverified]. Bulk availability per programme still to be confirmed. |
| Discovery: health | NPPES NPI Registry API (medical/dental/therapy practices) | 🟢 | Free, no key [secondary]. Also has a full monthly download. |
| Discovery: local | Duval County Tax Collector BTR reports (monthly/weekly new receipts, master listing) | 🟢 | Downloadable from the Tax Collector [secondary]. Other counties vary; some only respond to public-records requests, **which can carry copying/labour fees** (🟡, case by case, never automatic). |
| Discovery: directories | Chamber / association member lists | 🟢 | Only where the terms permit. Manual CSV import otherwise. |
| **Geocoding** | US Census Geocoder (single + batch up to 10k) | 🟢 | No key. |
| Geocoding | Nominatim public API | 🟡 | 1 request/s, no bulk use. Used only for interactive place-name lookup. |
| Geocoding (optional) | Self-hosted Nominatim/Photon (Florida extract) | 🟢 | Free; costs disk/RAM only. |
| **Boundaries** | Census TIGER/Line (states, counties, places, ZCTAs, CBSAs) | 🟢 | [tested reachable] |
| **Map tiles** | OpenFreeMap public vector tiles | 🟡 | No key, no stated usage limits, commercial use allowed, but **donation-funded with no SLA** [secondary]. |
| Map tiles (fallback) | Self-hosted Protomaps PMTiles extract (e.g. Florida) | 🟢 | One static file served by the app; no third party at runtime. |
| Map library | MapLibre GL JS + Supercluster + Terra Draw | 🟢 | Open source (BSD/MIT). |
| **Website discovery** | Listed fields, domain inference + DNS + HTTP, social links | 🟢 | Local. |
| Website discovery | Common Crawl URL index | 🟢 | Free, rate-limited politely. |
| Website discovery (optional) | Self-hosted SearXNG metasearch | 🟡 | Software is free, but it queries search engines whose terms generally prohibit automated use. **Off by default**; documented, not recommended. |
| Website history | Wayback Machine CDX API; RDAP | 🟢 | Free, rate-limited politely. |
| **Crawling and audits** | Playwright Chromium, Lighthouse CLI, axe-core, tech fingerprints | 🟢 | Local CPU. PageSpeed Insights API is **not used** (it needs a Google key). |
| **Contact research** | Website/contact-page extraction, schema.org, Overture/OSM/registry fields; email domain MX check | 🟢 | No paid email finders or verifiers. No SMTP probing. Emails never guessed. |
| **AI analysis** | `none`: deterministic + heuristic analysis | 🟢 | Subjective dimensions are shown as "not assessed" rather than invented. |
| AI analysis (recommended) | `ollama`: local open vision model (e.g. Qwen3-VL 8B; ~8 GB VRAM) | 🟢 | Cost is your hardware and electricity. CPU-only works but is slow (minutes per site). Quality is below frontier models, so it is used only for anchored-rubric scoring with evidence checks [secondary VRAM figures]. |
| AI analysis (optional) | Gemini API free tier | 🟡 | Requires a **Google API key**; low daily limits; **free-tier content may be used to improve Google products** [secondary]. Not used, per your instructions. |
| AI analysis (optional) | Claude API | 🔴 | Paid per token. Disabled; when enabled it requires an explicit monthly budget, and the cost guard (§9.4) blocks calls beyond it. |
| **Database** | PostgreSQL 16 + PostGIS on the host | 🟢 | Self-hosted. Managed free tiers (Supabase/Neon) are not used. |
| **Object storage** | Local disk (screenshots, HTML, Lighthouse JSON) | 🟢 | Budget roughly 2–5 MB per analysed site. |
| **Hosting** | Your own computer via Docker Compose | 🟢 | Recommended: 4+ cores, 16 GB RAM, plus a GPU if using local AI. |
| Hosting (optional) | A small VPS | 🔴 | ~$5–40/month. Not needed. |
| Hosting (optional) | Oracle Cloud Always Free ARM VM | 🟡 | Free, but capacity is not guaranteed and idle instances can be reclaimed. |
| **CI** | GitHub Actions | 🟡 | Free for public repos; **private repos get a free monthly minute quota**. Our test suite is sized to fit. |
| Paid, disabled | Google Places / Google Maps / PSI / Brave / Serper / Claude API | 🔴 | Adapters exist only as stubs behind `allow_paid: false`. |

**Net recurring cost of the default configuration: $0.** Two items need your judgement:
- Optional donations to OpenFreeMap; the self-hosted PMTiles fallback removes the dependency.
- Possible fees for county public-records requests, which are never triggered automatically.

---

## 3. Discovery sources and how they complement each other

| Need | Primary | Corroborating |
|---|---|---|
| Newly registered entities | Sunbiz daily corporate + fictitious-name files | — |
| Newly **opened** physical businesses | County BTR new-receipt reports (Duval first) | DBPR new licences (contractors, salons, restaurants), NPI new enumerations |
| Established operating businesses | Overture Places | OSM, DBPR active licences, BTR master listing, NPI |
| Industry classification | Overture taxonomy, OSM tags | DBPR licence type, BTR occupation code, NPI taxonomy, website content |
| Phone / website / social / email | Overture, OSM, the business's own website | NPI (phone), DBPR (address) |
| Operating status | Overture `operating_status`, BTR currency, licence status, Sunbiz status | Website liveness |
| Age / establishment | Sunbiz filing date, licence original-issue date, BTR start date | Wayback first-seen, RDAP domain creation |

**Licence and receipt registers are the key compensation for not having Google.**
- They cover trades that are under-represented in map data, such as home-based contractors, cleaners and mobile groomers.
- They come with **authoritative industry codes and issue dates**.
- DBPR and BTR lists of *new* licences and receipts are the best free signal that a business is genuinely starting to trade.

**Registry noise filter (Sunbiz).** These records are kept but **down-ranked**, never deleted:
- principal address is a registered-agent or mass-filer address;
- holding / SPV / real-estate name patterns;
- no corroboration from any operating-business source within N days.

### 3.1 Adapter architecture

Every source implements one interface:

```python
class SourceAdapter(Protocol):
    key: str                      # "overture", "osm", "sunbiz", "dbpr", "btr_duval", "npi", "google_places", ...
    cost_class: CostClass         # FREE | FREE_LIMITED | PAID
    requires_credentials: bool
    jurisdictions: Coverage       # e.g. global, US, FL, FL-Duval
    record_kinds: set[RecordKind] # ENTITY, LOCATION, LICENCE, CONTACT
    licence: str                  # data licence, drives retention + attribution
    def plan(self, area: Area, filters: Filters) -> list[Task]        # how the area will be covered
    def fetch(self, task: Task) -> Iterator[SourceRecord]             # raw records with provenance
    def coverage(self, area: Area) -> CoverageReport                  # what was searched, when, how completely
```

- Adapters are registered in `sources.yaml` with `enabled`, schedule and per-adapter rate limits.
- Adding a free source means adding one module + one YAML entry.
- `google_places` ships as a `PAID` adapter. It normalizes Places data into the same `SourceRecord` shape, so **enabling it is configuration only**: `enabled: true`, `allow_paid: true`, an API key and a budget. The cost guard (§9.4) refuses to run any `PAID` adapter otherwise.
- Google's display rules would then require switching the map to Google Maps; the frontend's `MapView` abstraction (§9.3) makes that a configuration option too.

---

## 4. "Not found" vs "not searched": coverage is a first-class concept

Every adapter run writes **coverage records** per (source, area cell, filter set):
- `complete`
- `partial(reason)`
- `failed(error)`
- `not_run`
- `unsupported(jurisdiction)`

Each carries timestamps.

For every business the UI shows a **source checklist**, e.g. "Overture ✓ found · OSM ✓ searched, not found · DBPR – not applicable · Sunbiz ✓ matched · BTR ⚠ not searched (county not yet supported)". Statuses mean:
- "Searched, not found" is only stated when coverage for that area is `complete`.
- A business absent from every source is never asserted to "not exist". It is simply not in our data.
- Website status "No website found" requires the full website-discovery checklist to have completed. Otherwise it is "Investigation incomplete".

### 4.1 Discovery coverage dashboard

- **Per-source counts:** businesses found, unique to that source, last run, coverage state, errors.
- **Overlap:** a matrix (and UpSet-style chart) of how many businesses each pair or set of sources share. This is the honest measure of how much each free source adds.
- **Geographic coverage:** a map layer coloured by coverage state per county / ZIP / grid cell and per source, plus business density.
- **Industry coverage:** businesses per industry by source. This highlights industries where free data is thin (e.g. mobile services).
- **Contact completeness:** % with verified phone / website / email / address, by source and industry.
- **Benchmark against expected totals:** Census County Business Patterns establishment counts by NAICS and county (free bulk files on `www2.census.gov`; the Census API now requires a key and is not used) give a rough "expected vs found" ratio per industry. This is clearly labelled as an estimate because the definitions differ.

---

## 5. Website discovery and contact research (free)

Candidates are gathered in order; each step is recorded so incompleteness is visible:
1. Website fields from Overture, OSM, NPI and licence records.
2. Website links on public social profiles already listed for the business (profile metadata only; no logged-in scraping).
3. **Domain inference:** generate candidates from name, DBA, city and trade words (`abclandscaping.com`, `abclandscapingjax.com`, `abc-landscaping.net`, …), resolve DNS, fetch.
4. **Common Crawl URL index** lookup of inferred domains: confirms existence and age.
5. Reverse checks: a phone number found on a candidate site is matched against the business.

**Association confidence**:

| Evidence | Effect |
|---|---|
| Phone match | strong |
| Address or ZIP match | strong |
| Name in title / schema / logo | medium |
| City or service-area mention | weak |
| Directory, aggregator, franchise-corporate, parked or for-sale domain | reject |

Classifications are as in v0.1:
- Confirmed
- Inactive
- Inaccessible (no score)
- Association uncertain (review queue)
- No website found (only after the full checklist)
- Investigation incomplete

A social-only presence is recorded as `independent_website = false` with the social links kept.

**Contacts.**
- Collected only from published sources: the site's contact page, footer, schema.org data, `mailto:`/`tel:` links, and registry/licence/Overture/OSM fields.
- Each value stores its source, observed date, last-verified date and provenance class: verified-on-website / directory / registry / inferred / unavailable.
- Email checks are limited to syntax plus an **MX record check**.
- **No guessed emails. No SMTP probing.**
- Personal data of officers in registry files is not surfaced as business contact information.

---

## 6. Website intelligence engine

The crawl, measured-vs-assessed separation, industry profiles, local peer benchmarks and the WQS formula are **unchanged from v0.1**. They were never Google-dependent.

The crawl:
- Is polite: respects robots.txt, 1 request/s per host, at most 15 pages, never bypasses protections.
- Renders with Playwright and takes desktop and mobile screenshots.
- Runs local Lighthouse and axe.
- Checks links, fingerprints the tech stack, and looks up Wayback and RDAP history.

Changes:

**6.1 AI provider abstraction.** `AnalysisProvider` has three implementations:

| Provider | Role | Behaviour |
|---|---|---|
| `none` | Default | Deterministic analysis. Covers all *measured* criteria (technical, SEO, mobile, accessibility, maintenance signals, conversion-path features detected from the DOM). It also covers **heuristic** design signals: viewport, layout technique (tables/floats/flex/grid), font count, colour-contrast stats, image resolution vs display size, above-the-fold CTA detection, outdated libraries, copyright year (weak). AI-only criteria (e.g. visual professionalism, copy quality) are marked **not assessed**. They are excluded from the WQS and lower its confidence. |
| `ollama` | Recommended free option | Local vision model scores the same anchored rubrics using screenshots + extracted text. Output is JSON-schema constrained, and any claim without cited evidence (URL + quote/region) is dropped. Scraped content is treated as untrusted data; the model has no tools. |
| `anthropic` | Optional, paid, off | Same prompts and schema, gated by the cost guard and a monthly cap. |

All three produce the same `Assessment` schema with `provider`, `model` and `prompt_version` stored. Scores therefore show which kind of judgement they contain, and results from different providers are never silently mixed in one benchmark set.

**6.2 Calibration matters more with local models.** The fixture-site suite and the human-labelled golden set (§10) are the acceptance gate for whichever provider is enabled. We publish per-provider accuracy, and a provider that fails calibration is not used for scoring.

---

## 7. Scoring (updated inputs, same structure)

Three separate scores, unchanged in principle:
- **WQS:** website quality, 0–100, or N/A when there is no website or it is inaccessible.
- **BOS:** business opportunity, 0–100.
- **LPS:** lead priority, BOS × actionability.

Weights and industry tables live in versioned `scoring.yaml`.

Free-data substitutions for the signals that used to come from Google:

| Signal | v0.1 source | v0.2 free source |
|---|---|---|
| Operating / viability | Places `businessStatus`, reviews | Overture `operating_status` + source count/confidence; active licence; current BTR; active Sunbiz status; live website |
| Establishment / age | Review history | Sunbiz filing date, licence original-issue date, BTR start date, Wayback first-seen, RDAP |
| Size / scale | Review count | Licence class (e.g. certified vs registered contractor), number of locations, BrightQuery presence in Overture sources, site complexity. All weak signals, labelled as such. |
| Reputation | Ratings | **Not available.** Excluded and noted; it does not count against the business. |
| Chain / franchise | Brand lists | Overture `brand` (present on 49% of places in the test area) + multi-location detection + brand list |

**Verification confidence** (feeds LPS) is now explicitly **multi-source agreement**. A business confirmed independently by e.g. Overture + DBPR + BTR, with a matching phone on its own website, is high confidence. A single-source record is low.

---

## 8. Data model changes vs v0.1

v0.1 tables kept as designed: `organization`, `location`, `source_record`, `location_source_link`, `fact`, `website`, `location_website`, `crawl`, `page`, `measurement`, `assessment`, `score`, `industry_profile`, `benchmark_set`, `search_area`, `discovery_run`, `lead*`, `review_queue`, boundaries.

Additions:
- `source` (key, cost_class, licence, attribution, enabled, config)
- `coverage` (source_key, area geometry or cell id, filter_hash, state, detail, started_at, finished_at, record_count)
- `licence_record` (location/organization, issuer, licence type, number, status, original_issue_date, expiry). DBPR, BTR and NPI all land here as well as in `fact`.
- `cost_ledger` (provider, operation, units, estimated_usd, run_id, at). Always written, even for free providers, so any non-zero entry is visible.
- `attribution` rendering per licence (ODbL, CDLA, Apache) on the map and in exports.

---

## 9. Architecture

### 9.1 Stack (unchanged except where noted)

- **Backend:** Python 3.13, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2.
- **Jobs:** Procrastinate (Postgres-backed).
- **Database:** PostgreSQL 16 + PostGIS + pg_trgm.
- **Bulk data:** PyArrow/GeoParquet readers for Overture **[tested]**. DuckDB is optional where its extensions can be installed.
- **Browser worker:** Playwright Chromium + Lighthouse CLI + axe-core.
- **Frontend:** React + Vite + TypeScript + Tailwind + TanStack Query/Table + Recharts.
- **Map:** **MapLibre GL JS**, Supercluster, Terra Draw.
- **Deployment:** Docker Compose on your machine; optional `ollama` service.

### 9.2 Repository layout

```
backend/app/
  geo/          boundaries (TIGER), area resolver, census geocoder, nominatim (interactive only)
  sources/      base.py (SourceAdapter, CostClass, Coverage), registry.py
                overture/  osm/  sunbiz/  dbpr/  btr/duval/  npi/  csv_import/
                google_places/   (PAID stub, disabled)
  resolution/   normalization, blocking, matching, merge + review queue
  websites/     candidate gathering, domain inference, association, classification
  crawl/        robots, fetcher, page selection, playwright, lighthouse, axe, techdetect, history
  analysis/     profiles, heuristics, providers/{none,ollama,anthropic}, rubric engine, benchmarks
  scoring/      wqs, bos, lps, explanations, scoring.yaml
  coverage/     coverage records + dashboard aggregations
  costs/        cost guard + ledger
  leads/        statuses, notes, lists, CSV/XLSX export
  api/          routers
frontend/src/   pages/{Dashboard,Discovery,Coverage,Profile,Leads,Settings}  map/  components/
config/         sources.yaml  scoring.yaml  profiles/*.yaml
docs/           PLAN.md  data-sources.md  scoring.md
```

### 9.3 Map

- MapLibre with the OpenFreeMap "liberty" style by default. The tile URL is configurable.
- A `make tiles` target extracts a regional PMTiles file (e.g. Florida) for fully self-hosted, offline-capable tiles.
- Clustering uses Supercluster, so 50k+ points stay responsive.
- Drawing (polygon/rectangle/circle for custom areas) uses Terra Draw.
- The map and results list stay synchronized through a shared selection store.
- The `MapView` interface allows a Google Maps implementation later. It is not built now.

### 9.4 Cost guard (the "no silent charges" mechanism)

- Every outbound integration goes through a `CostGuard` that knows each provider's `cost_class`.
- `PAID` calls raise `PaidCallBlocked` unless all three hold: `allow_paid: true` globally, the provider is enabled, and its monthly budget in `cost_ledger` is not exhausted.
- The Settings page shows every provider's class, status, and month-to-date estimated spend (always $0 in the default configuration).
- **Automated test:** with default configuration, the full discovery → analysis pipeline runs against recorded fixtures and asserts zero `PAID` attempts. The HTTP client also blocks a denylist of paid API hosts unless enabled.

---

## 10. Testing and validation (adds to v0.1 §10)

- **Adapter contract tests:** every adapter runs against recorded real samples. Overture is recorded live from this environment; others are built from official file layouts until they can be fetched from the host.
- **Coverage tests:**
  - a failed or partial run never produces "not found";
  - "No website found" requires the full checklist;
  - repeated runs are idempotent (no duplicates, coverage updated).
- **Cost-guard tests** as in §9.4.
- **Cross-source dedup tests** using real Overture records plus synthetic DBPR/BTR/Sunbiz rows for the same businesses (DBA names, suite numbers, multi-location brands).
- **Fixture websites** (A/B/B′/C landscapers, jewelry with/without catalogue, broken, parked, social-only) under the `none` provider (deterministic) and, if available, the `ollama` provider.
- **Golden set:** ~100 real Northeast Florida businesses labelled by you. It reports association precision (target ≥ 0.97), classification accuracy, and score correlation per provider.
- **E2E** (Playwright): select area → discover → map/list sync → profile → coverage dashboard → save → export.

---

## 11. Phases (revised)

### Phase 1: Foundation + free discovery
- Scaffold, Docker Compose, migrations, CI (sized for the free tier).
- TIGER boundary loader. Area resolver for city, county, ZIP, metro, state, multi-select, radius and drawn polygon.
- Adapters:
  - **Overture** (real data)
  - **OSM** (Geofabrik/Overpass; fixtures here, real on the host)
  - **Sunbiz** (daily + quarterly + fictitious names over HTTPS/WebDAV, with the date ledger and integrity checks from [SUNBIZ.md](SUNBIZ.md))
  - **CSV import**
  - Base contracts for DBPR/BTR/NPI
- Census geocoding.
- Entity resolution v1 + review queue. Provenance facts. Coverage records. Cost guard + ledger.
- UI:
  - Discovery page: MapLibre map, clustering, drawing, synced results.
  - Basic profile with the source checklist.
  - Saved leads.
  - CSV export.
  - **Coverage dashboard v1** (per-source counts, overlap, contact completeness).
- Acceptance:
  - Duval + St. Johns + one non-Florida city return real (Overture) businesses on the map.
  - Repeated search creates 0 duplicates.
  - Every field shows its source and date.
  - Zero paid calls (asserted by test).

### Phase 2: More free sources + website discovery and analysis
- Adapters: **DBPR**, **Duval BTR**, **NPI**, and St. Johns BTR if obtainable.
- Website candidate gathering, domain inference, association, classifications.
- Crawler, Lighthouse, axe, heuristics, history; WQS with the `none` provider.
- Fixture-site suite.

### Phase 3: Benchmarks, opportunity, AI (local)
- Industry profiles, peers, benchmarks; BOS/LPS with explanations.
- `ollama` provider + calibration report; golden-set labelling UI.

### Phase 4: Lead management + refinement
- Dashboard charts, advanced filters, statuses/notes/lists, XLSX export.
- Refresh scheduler (TTL by field volatility × lead value), history views, performance.
- Additional counties and states on demand.

---

## 12. Network requirements and diagnostics

Implemented ahead of Phase 1. Full details: [NETWORK.md](NETWORK.md).
- Every fixed external host is registered in [`config/network.yaml`](../config/network.yaml) with its purpose, required/optional status, cost class and real probes.
- `leadintel net-check` and `/diagnostics/network` report DNS, proxy, TCP, TLS, HTTP and download failures separately. They distinguish "network allowed" from "operational", and record the last successful download.
- Overture failover covers S3 → Azure Blob → Azure DFS, with per-file continuation.
- OSM uses Geofabrik extracts with MD5 verification, resume and incremental refresh. Overpass is only a small-area fallback.
- Business-website crawling uses a separate SSRF-protected client.
- Paid providers are blocked and the number of paid requests is reported (0).

## 13. Open items for you

1. **Hardware for local AI:** do you have a GPU, and how much VRAM? About 8 GB runs a capable vision model; without one, the `none` provider is used and subjective criteria are marked "not assessed".
2. **Foursquare OS Places:** willing to create a free Hugging Face account and accept the gated-access form? Optional; adds a third places source.
3. **Counties after Duval:** which ones matter most (St. Johns, Clay, Nassau, Orange, Miami-Dade, …)? Each county's BTR data is a separate adapter and some need a public-records request.
4. **Build-sandbox network** (optional, only affects testing here, not the product):
   - `www2.myfloridalicense.com` (DBPR)
   - `taxcollector.jacksonville.gov` and `duval.county-taxes.com` (Duval BTR)
   - `npiregistry.cms.hhs.gov` and `download.cms.gov` (NPI)
   - `tiles.openfreemap.org` (map tiles)
   - `web.archive.org` (Wayback)
   - `index.commoncrawl.org` (Common Crawl)
   - `rdap.org` (domain history)
   - `extensions.duckdb.org` (optional)

   Without them, those adapters are built against official file layouts and verified when run on your machine.
