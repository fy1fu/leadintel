# LeadIntel — Research, Architecture & Implementation Plan

Status: **pre-development plan, v0.1 (2026-11-02)** — awaiting decisions in §12 before Phase 1 code.

Conventions used below:
- **[verified]**: confirmed against the vendor's own docs during this research.
- **[secondary]**: from third-party sources; re-verify before relying on it.
- **[unverified]**: could not reach the source from the build sandbox (network policy). Must be checked first thing in Phase 1.

---

## 1. Executive summary and the main critiques of the brief

The brief is sound. Seven points will change the design substantially if we ignore them:

1. **No single source covers the market. Each source answers a different question.**
   - State registries tell us *who registered recently*. They have no phone, website or industry.
   - Google Places tells us *who is operating and visible*. Its terms restrict how long its data may be stored and what map it may be shown on.
   - OpenStreetMap is free and permissively storable, but its coverage of small service businesses is thin.

   The engine is therefore a **funnel** that joins these three, not a crawler of one "best" source.

2. **"Newly registered" ≠ "new business needing a website".** A large share of new Florida LLCs are holding companies, real-estate SPVs, registered-agent shells, or businesses that have operated for years and just re-filed. We need a **"likely operating, customer-facing business" classifier** before any of these become leads. Without it, the new-registration feed will bury good leads in noise.

3. **Franchises, chains and multi-location brands must be detected and down-weighted.** A local Jiffy Lube or State Farm agent has a bad (or no) local site because corporate owns the website. They are not prospects. The brief doesn't mention this, and it is one of the biggest sources of false-positive "opportunities".

4. **Purely relative (peer-benchmarked) website scores are dangerous.** If every landscaper in a small market has a 2009-era site, the best of them must not score 85. The design uses three numbers:
   - An **absolute, industry-specific rubric score**, so each industry has its own expectations but the scale is shared.
   - A **separately reported local-peer percentile**.
   - A **competitive gap** that feeds the Business Opportunity Score.

   This satisfies "industry-relative" without letting a weak market inflate scores.

5. **AI "visual professionalism" scores are not repeatable by default.** Sampling parameters are no longer adjustable on current Claude models. Repeatability will come from four things instead:
   - Anchored rubrics: each criterion is scored on a 1–5 scale with written definitions and required evidence.
   - Deterministic aggregation done in code.
   - Caching assessments by content hash: the same site and the same rubric version reuse the same stored result.
   - Calibration against a human-labelled golden set.

   Test 10 in the brief is met by construction plus a measured variance budget.

6. **The Google Maps Platform terms restrict how Places data is used:**
   - Places content generally may not be shown on a non-Google map.
   - Only `place_id` may be cached indefinitely. Other fields must be refreshed.

   This directly affects the mapping-library choice (§7.3) and the database design (§6). **[secondary/recall — confirm against the current Service Specific Terms before Phase 1]**

7. **The first version is US-only in practice.** Geographic *search* can be global: boundaries come from Nominatim and Natural Earth. But registration data, Census geocoding and boundaries, and most directories are US-specific. We will design the source adapters per jurisdiction rather than pretending "Country" works everywhere on day one.

---

## 2. Data source research

### 2.1 Business registration (new businesses)

| Source | Access | What it gives | Cost | Notes |
|---|---|---|---|---|
| **Florida Division of Corporations (Sunbiz) bulk data** | SFTP: daily files (`doc/cor/yyyymmddc.txt`) plus quarterly full snapshots (Jan/Apr/Jul/Oct, split into 10 parts) [secondary: official dos.fl.gov pages via search] | Fixed-width records: document number, name, status, filing date, entity type, principal and mailing address, registered agent, officers/managers, FEI number [field layout **unverified**: dos.fl.gov blocked from sandbox] | Free | **Primary Florida "newly registered" source.** No phone, email, website or industry. A separate **fictitious-name (DBA) file** links trade names to entities, which matters for matching. Daily "events" files capture status changes (dissolutions, reinstatements). |
| Sunbiz web search | HTML search | Same data, one entity at a time | Free | Do not scrape. Use bulk files. Deep-link profiles for users. |
| Other states | Varies widely | Varies | Free to paid | Free open-data feeds exist in several states, e.g. NY and CO open-data portals (Socrata), OR and WA [**unverified per state**]. Many states sell bulk data or offer none. **OpenCorporates API** aggregates across states but is paid and licence-restricted. Plan: one `RegistryAdapter` per state, starting with FL, then adding states by demand. |
| County / municipal business tax receipts (e.g. Duval Tax Collector BTR) | Usually public-records requests or portals; inconsistent | Local business address, often *category* (BTR classification), start date | Free to low | Valuable because it has an industry code and a physical location. Not automatable everywhere, so Phase 4+ per county. |

**Registration date vs operating start date.** We store `registration_date` exactly as filed. `operating_since` is a separate *inferred* fact with its own evidence: earliest review date, earliest Wayback snapshot of the site, domain creation date (RDAP), and Google "first seen" in our own sweeps. Every inference is labelled as one.

**Filtering registry noise:**
- Drop entities whose principal address equals a known registered-agent or mass-filer address. We build that list from address frequency in the files themselves.
- Drop names matching holding/SPV patterns ("Holdings", "Properties LLC", "Capital", "Investments", or numbered entities). Down-rank them rather than delete them when the evidence is weak.
- Down-rank entities with no customer-facing evidence: no matching Places/OSM listing and no domain after N days.
- Keep the rest as "registered, not yet visible" candidates. Re-check them on a schedule, because a new LLC often appears on Google Maps weeks after filing.

### 2.2 Operating businesses (established and new)

| Source | Coverage | Fields | Cost / limits | Licensing |
|---|---|---|---|---|
| **Google Places API (New)**: Text Search / Nearby Search / Place Details | Best US local coverage | `formattedAddress`, `location`, `types`, `businessStatus` (**Pro** tier). `websiteUri`, `nationalPhoneNumber`, `rating`, `userRatingCount`, `regularOpeningHours` (**Enterprise** tier) [verified] | Text Search caps at **60 results per query** (pages of 20) [verified]. Free monthly caps: 10k Essentials / 5k Pro / 1k Enterprise. Text Search Pro ≈ $32/1k; Text Search Enterprise ≈ $35/1k, falling with volume [secondary] | Restrictive. `place_id` may be stored indefinitely; most other content cached only short-term; display on Google maps. Store `place_id` permanently, treat the other fields as refreshable cache, and record the fetch timestamp. |
| **OpenStreetMap via Overpass API** | Good for storefronts in cities, weak for home-based trades | `name`, `shop`/`amenity`/`craft`/`office` tags, `website`, `phone`, `opening_hours`, address | Free. The public instance's fair-use limits are roughly 10k queries/day; self-host for volume [recall, **unverified**: wiki blocked] | ODbL. Attribution is required, and share-alike applies to a public derivative *database*. Internal use is fine. |
| Search-engine results (for website discovery, not enumeration) | Universal | Candidate URLs | Bing API retired Aug 2025 [secondary]. Brave Search API ~$5/1k with small monthly credit; Serper ~$1/1k [secondary; vendors' pricing must be checked] | Per vendor terms. Used only for targeted "name + city" lookups. |
| Chamber of Commerce and industry directories | Patchy, high quality where present | Name, category, site, phone | Free | Many prohibit scraping. Add per directory only after a terms review. Manual import (CSV) is supported from day one. |
| Yelp Fusion / Foursquare Places | Good | Similar to Google | Yelp now paid; Foursquare has a free tier [**unverified**] | Possible future adapter. Not v1. |

**Why the 60-result cap matters.** A query like "plumber in Jacksonville" returns at most 60 places. Full coverage needs **spatial tiling × category**: a quadtree that splits any tile returning 60 results. Discovery planning (§5.1) has to budget for this.

### 2.3 Website discovery and analysis sources

| Tool | Purpose | Cost |
|---|---|---|
| Playwright + Chromium (self-hosted) | Crawl, render, screenshot desktop and mobile, detect forms and CTAs | Free (CPU) |
| Lighthouse (CLI, self-hosted), with **PageSpeed Insights API** as fallback/cross-check | Performance, SEO, best-practices and accessibility audits | Free. PSI: 25k requests/day per project, ~240/min [secondary] |
| **CrUX API** | Real-user Core Web Vitals | Free. Most small businesses have no CrUX data; treat absence as "not available", not as bad. |
| axe-core (inside Playwright) | Accessibility violations | Free |
| Technology fingerprints (open-source Wappalyzer-style ruleset) | CMS and builder (Wix, Squarespace, WordPress, GoDaddy builder), jQuery or Bootstrap versions, Flash, tables-layout | Free |
| Wayback Machine CDX API | First-seen date, how often content changes, i.e. maintenance evidence | Free (rate-limit politely) |
| RDAP | Domain creation and expiry (weak age signal; also catches expiring or parked domains) | Free |
| Claude API (vision + structured outputs) | Industry and business-model classification, rubric scoring of subjective dimensions, overviews, recommendations | See §8 |

### 2.4 Geography

- **US boundaries are loaded into PostGIS** from Census TIGER/Line: states, counties, places (cities), ZCTAs (ZIP areas), CBSAs (metro areas). Free and authoritative. This powers city, county, state, ZIP, metro, multi-select, radius and drawn-polygon search with one spatial query type.
- **Geocoding:**
  - **US Census Geocoder** (free; batch up to 10k addresses) for registry addresses.
  - Google results already carry coordinates.
  - Nominatim for non-US place lookup. Its usage policy is 1 req/s, so self-host if heavy.

---

## 3. Discovery workflow (funnel)

```
Search area (polygon) + industries + date range
        │
        ├─► A. Registry feed (Sunbiz daily/quarterly → geocode → point-in-polygon)
        ├─► B. OSM/Overpass (free sweep of area, all POI business tags)
        └─► C. Google Places (tiled Text/Nearby search per category; budget-capped)
                │
        Entity resolution (§4) ──► canonical Business + Location records with provenance
                │
        Pre-qualification (cheap, no paid calls)
          – operating? (businessStatus, registry status)
          – chain/franchise? (brand list + multi-location detection)
          – customer-facing industry? (category map)
          – holding/SPV registry noise?
                │
        Website discovery (§5) for qualified records
                │
        Website analysis (§6), only for confirmed associations
                │
        Competitor set + benchmarks (§6.4) ──► scores (§7) ──► map / lists
```

**Incremental by design:**
- Every external fetch is recorded as a `source_record` with a content hash. Re-searching an area reuses records still within their TTL.
- Re-crawls skip analysis when the page-content hash is unchanged.
- "New since last run" is a first-class filter.

**Cost control:**
- Each discovery run gets a **dollar budget** and shows an estimate before it starts.
- Paid Enterprise-field lookups run only *after* the free pre-qualification step.

---

## 4. Entity resolution and duplicates

The model separates three things:
- **Organization:** legal entity, possibly with DBAs.
- **Location:** a physical place doing business. This is what the user prospects.
- **Website:** a domain, shared across locations.

Locations are separate prospects. A shared domain lowers *website opportunity* for each location, because one sale covers all of them.

Matching pipeline:
1. **Normalize**:
   - Names: lowercase, strip legal suffixes, `&`→`and`, punctuation.
   - Phones: E.164.
   - Addresses: `usaddress` / libpostal, plus suite handling.
   - Domains: registrable domain via the Public Suffix List.
2. **Block** candidates by geohash-6 neighbourhood, phone, domain, or name trigram (`pg_trgm`).
3. **Score** pairs:

   | Signal | Weight |
   |---|---|
   | Same phone | strong |
   | Same domain | strong (unless it's a franchise / platform domain such as facebook.com or wixsite) |
   | Name similarity (Jaro-Winkler + token-set) | medium |
   | Distance < 75 m | medium |
   | Distance > 1 km | strong negative |
   | Registry ↔ Places link by name + address, or DBA file | medium |

4. **Act on the score:**
   - Auto-merge above a high threshold.
   - Below that, a **review queue** in the UI.
   - Never auto-merge different street addresses.

5. **Keep every source record.** Merges are reversible, because the canonical record is rebuilt from its linked source records.

---

## 5. Website discovery and association

### 5.1 Candidate gathering, cheapest first

1. `websiteUri` from Places.
2. `website` tag from OSM.
3. Links found on the business's own social profiles. Only public profile metadata is used; no logged-in scraping.
4. Targeted search query: `"<name>" <city> <state>` and `"<phone>"`.
5. Domain guessing: name-derived candidates (`abclandscaping.com`, `abc-landscaping.com`, `abclandscapingjax.com`) checked by DNS, then a fetch.

### 5.2 Verification

For each candidate URL, fetch the homepage and contact page and compute an **association confidence** from:

| Evidence | Effect |
|---|---|
| Phone match | strong |
| Street address or ZIP match | strong |
| Business name in `<title>`, logo alt text, or schema.org `LocalBusiness` | medium |
| City / service-area mention | weak |
| Domain is a directory, aggregator, franchise corporate site, or parked page | **reject** |

**Website classifications** (from the brief) map to rules:

| Classification | Rule |
|---|---|
| Website confirmed | association ≥ 0.8 and site responds with real content |
| Website association uncertain | 0.4–0.8 → goes to the human review queue; no quality score shown as fact |
| Website appears inactive | parked page, for-sale page, "coming soon", default CMS page, expired-domain redirect, or no content change in years + broken core pages |
| Website inaccessible | DNS / TLS / 5xx / timeouts on ≥ 2 attempts on different days. **No score.** Rechecked. |
| No website found | every search step ran and returned nothing; this records *search coverage*, not proof of absence |
| Website investigation incomplete | some steps not yet run (budget, rate limit) |

**Social-only presence:**
- A Facebook, Instagram, TikTok or Linktree URL as the "website" ⇒ `independent_website = false`.
- The social links are stored on the record.
- Website-builder subdomains (e.g. `*.wixsite.com`) count as a website, but carry an "unbranded domain" deficiency.

---

## 6. Website intelligence engine

### 6.1 Crawl (polite by construction)

| Setting | Value |
|---|---|
| robots.txt | Obeyed (RFC 9309 parser) |
| User-agent | Identifying, with a contact URL |
| Rate | 1 request/s per host, plus a host concurrency of 1 |
| Page budget | 15 pages per site (configurable), chosen by priority: home → contact → services/products (via nav and anchor text) → about → gallery/portfolio → booking/menu/shop → location pages |
| Re-crawl | Only after TTL or a change signal |
| Protections | Never bypassed (no CAPTCHA solving, no WAF evasion). A blocked crawl is classified "inaccessible: blocked", not scored. |

**Per page we capture:**
- Final URL, status, headers, HTML hash, rendered DOM text.
- Links and broken-link checks (HEAD with GET fallback, internal links only, plus a sample of external).
- Forms and their fields.
- Images (size, alt text, upscaled/blurry heuristics).
- Fonts.
- Viewport meta.
- Schema.org JSON-LD.

**Screenshots:**
- Desktop 1366×900 and mobile 390×844.
- Full-page and above-the-fold.

**Lighthouse:**
- Mobile and desktop for the homepage.
- Mobile only for one key interior page.

### 6.2 Measured vs assessed: kept separate everywhere

| Measured (objective) | Assessed (AI, subjective, labelled as such) |
|---|---|
| HTTPS, valid cert, mixed content | Visual professionalism, hierarchy, modernity |
| Lighthouse perf, SEO, a11y, best-practices | Brand presentation, imagery quality |
| CWV (CrUX when available) | Clarity of offering and value proposition |
| Viewport and responsive breakpoints; horizontal scroll at 390 px; tap-target sizes | Copy quality, completeness of service descriptions |
| Broken links count | Trust-signal strength, CTA effectiveness |
| Title, meta description, H1 structure, indexability (robots meta, sitemap), schema presence | Overall UX friction |
| Click-to-call link, form presence, booking widget detected, e-commerce detected | Business-model and purpose inference |
| Tech stack and versions, copyright year, Wayback change frequency | "Looks dated" judgement |

The UI badges every finding **Measured**, **Detected** (heuristic) or **AI assessment**, each with a confidence.

### 6.3 Industry profiles (benchmarks)

Each profile is a versioned YAML file. It contains:

- **Purpose archetype**, one of:
  - `lead_gen_service`: contractors, landscapers, cleaners
  - `local_visit`: restaurants, retail, salons
  - `appointment`: medical, dental, spa, veterinary
  - `trust_professional`: law, accounting, financial
  - `ecommerce_catalog`: jewelry and specialty retail with online sales
  - `portfolio`: photographers, event businesses
- **Features**, each tagged `required` / `expected` / `bonus` / `irrelevant` with a weight:
  - Landscaper: `quote_form` required, `service_area` required, `gallery` expected, `ecommerce` irrelevant.
  - Jewelry: `product_catalog` expected, `high_quality_photography` required, `ecommerce` expected if online-sales model, `quote_form` irrelevant.
- **Dimension weights:** visual, UX, effectiveness, technical, content, SEO, modernity.
- **Rubric anchors per criterion**: the text of what 1/3/5 looks like for *this* industry, e.g. "5 = dedicated page per core service with photos, pricing guidance or process, service-area list".
- **Reference sites**: human-curated exemplars and anti-exemplars with labels. These are used for calibration and as few-shot visual anchors.

**Profile assignment:**
1. Classify industry and business model: Places `types` + registry name + site content → a taxonomy loosely mapped to NAICS.
2. Choose a base profile.
3. Apply **modifiers**, e.g. "restaurant + online ordering", "retail + sells online", "multi-location", "premium positioning" detected from pricing language and brand cues.

So the profile adapts rather than being a fixed template. Missing a feature costs points **only** if it is `required` or `expected` for the resolved profile. That covers brief test 3.

### 6.4 Local competitive benchmark

For each (profile, market) pair, e.g. landscaping × Jacksonville CBSA:
- Select comparable peers: same profile, same metro, similar size band (review count / location count), **excluding national chains**.
- Analyze 5–15 peers. These are cached and reused by every prospect in that pair.
- Store the distribution of dimension scores.

Each prospect report shows:
- Percentile vs peers.
- Gap to the peer top-quartile in each dimension.
- 2–3 named comparable peers with their key differences.

**Pairwise calibration.** The AI also does a **pairwise comparison** of the prospect against the median and top peers (screenshots + feature table). Pairwise judgements are far more stable than absolute ones. The prospect's assessed scores are then adjusted within bounds to be consistent with those orderings.

### 6.5 Website Quality Score (WQS)

```
criterion score  c_i ∈ [0,1]   (measured → deterministic transform; assessed → anchored 1–5 rubric → (r−1)/4)
dimension score  D_k = Σ w_ik·c_i / Σ w_ik     over criteria applicable to the profile
WQS              = 100 · Σ W_k·D_k / Σ W_k     with profile dimension weights W_k
confidence       = f(criteria coverage, association confidence, crawl completeness, assessment agreement)
```

- Unknown or unmeasurable criteria are **excluded and lower confidence**. They are never scored as 0.
- No website ⇒ WQS = **N/A**.
- Inaccessible ⇒ WQS = **N/A, status inaccessible**.
- Every score row stores the profile version, weights version, model and prompt versions, and input hashes. Scores are therefore reproducible and diffable over time.

**The A/B/C landscaper requirement:**
- Anchored rubrics separate A (≈30–45) from B (≈72–80).
- C beats B through effectiveness and polish criteria plus the pairwise check, by a small margin (≈3–8).
- Automated fixture tests (§10) enforce this.

---

## 7. Opportunity and priority scoring

### 7.1 Business Opportunity Score (BOS, 0–100)

Weighted components, all configurable in `scoring.yaml`:

| Component | Default weight | Inputs |
|---|---|---|
| Website need | 0.30 | No site → high base, adjusted down if a strong social/booking presence suits the business. Otherwise based on 100 − WQS, the count of severe deficiencies, and the peer gap. |
| Business value | 0.20 | Industry customer-value tier (configurable table: e.g. roofing/legal/dental high; nail salon/food truck lower); premium positioning signals |
| Acquisition dependence | 0.15 | How much customers in this industry search online before buying (industry table) + local search competition |
| Competitive pressure | 0.15 | Peer median and top-quartile WQS vs the prospect: "losing to competitors online" |
| Viability and establishment | 0.20 | Operating status, review count and recency, rating, years operating (inferred), growth signals (new location, hiring, new registry DBA). Newly registered: moderate, with low confidence. |

Gates and multipliers applied after the weighted sum:

| Condition | Effect |
|---|---|
| Closed / inactive | × 0 |
| National chain or franchise location | × 0.1 (shown, but deprioritized) |
| Clearly not customer-facing (holding entity) | × 0 |
| Excellent website (WQS ≥ 88 and peer percentile ≥ 80) | BOS capped at 25 |

### 7.2 Lead Priority Score (LPS, 0–100)

```
actionability A = 0.35·contact_completeness + 0.25·business_verification
                + 0.25·assessment_confidence + 0.15·freshness            (each 0–1)
LPS = BOS · (0.4 + 0.6·A)
Priority band: High ≥ 70, Medium 45–69, Low < 45  (configurable)
```

A high-BOS lead with an unverified phone and an uncertain website association cannot reach "High".

Each score has a generated **explanation panel**: component contributions, top evidence, and what would change the score.

---

## 8. AI layer

**Model.** Claude via the Anthropic SDK, with the model configurable per task.

Default for subjective assessment and narrative: `claude-opus-5-5` ($4 / $20 per M input/output tokens; cache reads $0.20/M) [verified via the current Claude API reference]. Cheaper tiers are a configuration choice, not a code change, and are your call (§12):
- `claude-sonnet-5-5`: $2 / $10
- `claude-haiku-5-5`: $0.10 / $0.50

**Tasks:**
- Industry and business-model classification (cheap, short).
- Rubric scoring (vision: screenshots + extracted text + measured facts).
- Pairwise peer comparison.
- Business overview.
- Deficiency → recommendation synthesis.

**Untrusted-content handling:**
- Scraped text and images go only in user-content data blocks, delimited and labelled as untrusted.
- The analysis calls have **no tools**.
- Output is constrained by **structured outputs (JSON schema)**.
- Instructions found inside site content are ignored by policy and never reach any tool-capable component.
- Every AI claim must cite evidence: page URL plus quoted snippet or screenshot region. Claims without evidence are dropped in post-processing.

**Cost per analysed site** (rough):
- Input: ~4 screenshots ≈ 6k image tokens, ~8k text tokens, ~3k cached rubric tokens.
- Output: ~3–4k tokens.
- Opus 5.5 ≈ **$0.12–0.16 per site**, about half that via the **Batch API** (50% off; fine because analysis is not interactive). So ~$60–80 per 1,000 sites batched.
- Sonnet 5.5 is ~half of Opus; Haiku 5.5 is ~1/40th.

**Repeatability:**
- Assessments are cached by `(content_hash, profile_version, prompt_version, model)`.
- An optional 3-sample median for borderline cases, used only when confidence is low.

---

## 9. Architecture

### 9.1 Stack

| Layer | Choice | Why |
|---|---|---|
| API | **Python 3.13 + FastAPI + SQLAlchemy 2 + Alembic + Pydantic v2** | Python has the best tooling for scraping, address parsing, geo (Shapely, GeoAlchemy2), entity resolution and ML. FastAPI gives typed OpenAPI, which generates the TS client. |
| Jobs | **Procrastinate** (PostgreSQL-backed task queue) | No Redis or RabbitMQ to run. Jobs are transactional with data, and have retries, scheduling and locks. Switch to Celery/Arq only if throughput demands it. |
| Browser workers | Separate worker process/container with Playwright Chromium + Node Lighthouse | Isolates heavy and crash-prone work, and scales independently. |
| DB | **PostgreSQL 16 + PostGIS + pg_trgm** | Spatial search, fuzzy matching and full provenance in one store. |
| Object storage | Local disk in dev; S3-compatible (Cloudflare R2 / Backblaze B2) in prod | Screenshots, raw HTML, Lighthouse JSON |
| Frontend | **React + Vite + TypeScript + Tailwind + TanStack Query/Table + Recharts** | An internal tool doesn't need Next.js SSR. Vite is simpler, and it can be wrapped in Tauri later for a "desktop app". |
| Map | See §9.3 | |
| Deploy | Docker Compose. One VPS (4 vCPU / 8 GB, ~$20–45/mo) runs everything for one or a few users. | Chromium plus Lighthouse are the RAM drivers. |

The backend is modular, with one package per bounded area:
```
backend/app/
  geo/            boundaries, geocoding, area resolution
  sources/        adapters: sunbiz/, osm/, google_places/, search/, manual_csv/   (common SourceAdapter interface)
  resolution/     normalization, matching, merge/review queue
  websites/       discovery, association, classification
  crawl/          robots, fetcher, page selection, playwright, lighthouse, axe, techdetect
  analysis/       profiles, rubric engine, AI client, benchmarks, peers
  scoring/        wqs, bos, lps, config, explanations
  leads/          status, notes, lists, exports
  refresh/        TTL policies, schedulers
  api/            routers
```

### 9.2 Core schema (abridged)

- `organization`
  - (id, legal_name, entity_type, jurisdiction, registry_id, registration_date, status)
- `location`: **the prospect unit**
  - (id, organization_id?, display_name, geom POINT, address fields, county_fips, place_fips, cbsa, industry_code, profile_id, chain_flag, operating_status, first_seen_at)
- `source_record`
  - (id, source, external_id, fetched_at, expires_at, payload JSONB, payload_hash, licence_tag)
  - `licence_tag` drives retention, e.g. Google payloads are purged or refreshed after their TTL and only `place_id` is retained.
- `location_source_link`
  - (location_id, source_record_id, match_score, method, confirmed_by)
- `fact`: per-field provenance
  - (entity_type, entity_id, field, value JSONB, source_record_id, provenance ENUM[verified, directory, website, registry, inferred, user], observed_at, last_verified_at, confidence, superseded_at)
  - Conflicting facts are kept. The "current value" resolver picks the best one and flags the conflict.
- `website` (id, registrable_domain, root_url)
- `location_website` (location_id, website_id, association_confidence, evidence JSONB, classification, independent BOOL)
- `social_profile` (location_id, platform, url, source)
- `crawl` (id, website_id, started_at, robots_allowed, pages_attempted, status)
- `page` (crawl_id, url, status, content_hash, screenshot keys, extracted JSONB)
- `measurement` (crawl_id, kind [lighthouse, axe, links, tech, seo, wayback, rdap], data JSONB)
- `assessment` (crawl_id, profile_version, prompt_version, model, input_hash, output JSONB, cost_usd)
- `score` (location_id, kind [wqs, bos, lps], value, components JSONB, confidence, config_version, computed_at). History is kept.
- `industry_profile` (id, version, yaml, active)
- `benchmark_set` (profile_id, market_cbsa, peer_location_ids[], distribution JSONB, computed_at)
- `search_area` (id, name, geom MULTIPOLYGON, spec JSONB)
- `discovery_run` (id, area_id, params, budget_usd, spent_usd, status, counts JSONB)
- `lead` (location_id, status ENUM[new, saved, reviewed, contacted, interested, not_interested, excluded], updated_at)
- `lead_note`
- `lead_list`, `lead_list_item`
- `review_queue` (kind [merge, website_association], payload, resolution)
- Boundary tables: `boundary_state`, `boundary_county`, `boundary_place`, `boundary_zcta`, `boundary_cbsa`

### 9.3 Map: a decision driven by licensing

| Option | Pros | Cons |
|---|---|---|
| **A. Google Maps JS API** (`@vis.gl/react-google-maps` + MarkerClusterer + Terra Draw's Google adapter for drawing) | Compliant display of Places-sourced data. Free tier (≈10k dynamic map loads/mo [secondary]) is plenty for an internal tool. Familiar UI. | Vendor lock-in for the base map; per-load cost if the tool grows to many users. |
| **B. MapLibre GL + OpenFreeMap/Protomaps tiles + Supercluster + Terra Draw** | Free, fast WebGL clustering at 50k+ points, full styling control. | Can't show Google Places content on it. If Google is a discovery source, those records would need to be shown as OSM/registry-derived only, which is impractical. |

**Recommendation:** A if Google Places is a source (I recommend it is, for coverage). Wrap the map behind a `MapView` interface so B can be swapped in for an OSM/registry-only deployment.

---

## 10. Testing and validation

1. **Unit tests** (pytest): normalizers, matchers, association scoring, robots compliance, rubric aggregation math, score configuration, profile resolution, Sunbiz fixed-width parser (fixtures built from the official layout).
2. **Controlled fixture websites.** A set of locally served synthetic sites with deliberate, known differences, crawled by the real pipeline:
   - Landscaper A (dated: table layout, no viewport, no CTA)
   - B (modern, clean, service pages, quote form)
   - B′ (≈B, near-identical copy)
   - C (B + better CTAs, gallery, reviews, schema)
   - Jewelry store without e-commerce vs with catalog
   - Broken site (5xx); parked domain; Facebook-only business

   These let us assert, deterministically for measured parts and statistically for AI parts:
   - **Brief test 1:** B ≈ B′
   - **Brief test 2:** C > B
   - **Brief test 4:** A ≪ B
   - **Brief test 3:** the landscaper isn't penalized for lacking e-commerce
   - **Brief test 5:** jewelry is judged by a different profile
   - **Brief test 6:** no-site → N/A
   - **Brief test 7:** inaccessible → N/A, not 0
3. **Integration tests**: Postgres+PostGIS in Docker. Covers area search, dedup across repeated runs (**brief test 9**), provenance completeness, budget enforcement. External APIs are replayed from recorded cassettes (VCR) so the tests are hermetic.
4. **Golden real-world set**: ~80–120 real Northeast Florida businesses across 6–8 industries, **human-labelled** for website class and a 1–5 quality band.
   - Metrics: classification accuracy, website-association precision (target ≥ 0.97: wrong-site attribution is the worst error), Spearman correlation of WQS with labels, re-run stability (**brief test 10**: |Δ| ≤ 3 points on unchanged sites).
   - **Brief test 8**: the unverified-data checks assert lowered confidence and no fabricated fields.
5. **E2E**: Playwright against the running app. Search an area → markers appear → open a profile → save lead → export CSV/XLSX.

Results are reported from actual runs. Gaps are listed as gaps.

---

## 11. Phased delivery (revised)

Each phase ends with a demo on real data, plus a test report.

### Phase 1: Foundation and discovery (vertical slice)
- Repo, Docker Compose, CI, migrations, boundary loader (FL first, all states supported).
- Area resolver: city, county, ZIP, metro, state, multi-select, radius, drawn polygon.
- Source adapters: **OSM/Overpass**, **Google Places** (tiled, budgeted, ToS-aware retention), **Sunbiz** daily + quarterly import with registry-noise filter and Census geocoding, **manual CSV import**.
- Entity resolution v1 + review queue. Provenance `fact` model.
- Pre-qualification: chain detection, operating status, customer-facing category.
- UI:
  - Discovery page: map with clustering, drawing and synced results list.
  - Basic profile with contact facts and their sources.
  - Saved leads.
  - CSV export (pulled forward because it's cheap and immediately useful).
- Acceptance: Duval + St. Johns + one non-FL city searched; real businesses on the map; repeated search creates 0 duplicates; every field shows its source and date.

### Phase 2: Website discovery and analysis
- Candidate gathering + association verification + six classifications + social-only detection.
- Polite crawler, Playwright screenshots, Lighthouse/PSI, axe, tech detection, link checker, Wayback/RDAP.
- Measured-criteria scoring; the AI rubric for 2 profiles (lead-gen service, local-visit) to start.
- Fixture-site test suite.

### Phase 3: Benchmarks and opportunity intelligence
- Full profile library (6 archetypes + modifiers), industry classifier.
- Peer selection, benchmark sets, pairwise calibration.
- BOS + LPS with explanations; configurable weights UI.
- AI overviews and recommendations with evidence citations.
- Golden-set labelling (with you) and calibration report.

### Phase 4: Lead management and refinement
- Dashboard and charts, advanced filters/sorts, statuses, notes, custom lists, XLSX export.
- Refresh scheduler: TTL by field volatility × lead value.
- History views, performance tuning, Batch-API analysis pipeline.
- Additional state registry adapters on demand.

---

## 12. Decisions needed from you before Phase 1

1. **Google Places as a source:** yes or no, and the monthly spend cap. A first full sweep of a metro the size of Jacksonville across ~30 categories, with spatial tiling, is roughly 2–4k Text Search calls ≈ **$60–150** after the free cap. Incremental refreshes are much less.
2. **Map provider:** Google Maps (recommended if #1 is yes) or MapLibre (OSM/registry-only).
3. **AI model and budget:** Opus 5.5 by default (~$0.06–0.08/site batched). Sonnet 5.5 or Haiku 5.5 to cut cost further is your choice.
4. **Search API** for website discovery: Brave or Serper (both ≈ $1–5 per 1k lookups).
5. **Deployment target:** local Docker on your machine, or a small VPS.
6. **Golden-set labelling:** willingness to spend ~2–3 hours rating ~100 sites in a labelling screen I'll build. This is what makes "accurate" measurable.
7. **Build-environment network access.** This cloud sandbox currently blocks `dos.fl.gov`, `overpass-api.de`, `nominatim.openstreetmap.org`, `geocoding.geo.census.gov`/`tigerweb.geo.census.gov`/`www2.census.gov`, and `wiki.openstreetmap.org`. Allowing them is required to build and test against real data here.
