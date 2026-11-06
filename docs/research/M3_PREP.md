# M3 preparation: website intelligence, WQS, and data-source research

Date: 2026-11-06. Read-only research and design. Nothing was installed, no account was created, nothing paid was
used. The existing design stays authoritative:
- [PLAN.md §6](../PLAN.md): crawl, `none` / `ollama` / `anthropic` analysis providers, calibration;
- [SCORING.md §5](../SCORING.md): WQS = weighted mean of seven dimensions, N/A when there is no website or it is
  inaccessible, never 0.

This note fills the gaps: how to crawl and show websites safely, which checks feed each dimension, how to calibrate,
how to benchmark peers, and what the revenue, directory, Gmail and licensing research found. It also designs the
**Search Area** (adjustable radius) feature (§7). The product plan and roadmap carry the summary:
[PLAN.md §14](../PLAN.md), [ROADMAP.md §7 and D14–D21](../ROADMAP.md).

## 1. Secure crawling architecture

| Concern | Decision |
|---|---|
| Browser | Playwright for Python driving the **already-installed Microsoft Edge** (`channel="msedge"`). The Friend Edition already depends on Edge, so no extra ~150 MB Chromium download. Chromium's own sandbox stays on. |
| Network egress (SSRF) | All browser traffic goes through a small **LeadIntel egress proxy** (Playwright `proxy=`). For each connection it resolves the name, rejects loopback, private, link-local, CGNAT, multicast and metadata ranges (the existing safe-egress rules), then connects to the **checked IP**. This closes DNS rebinding, which a URL-level allowlist cannot. Non-HTTP(S) schemes are refused. |
| Isolation | One fresh browser context per site, with no persistent profile, no stored cookies and all permissions denied. Downloads, service workers, `file:` URLs and pop-ups are blocked. The crawler never runs inside the LeadIntel web server process. |
| Politeness | `robots.txt` is honoured (stdlib `urllib.robotparser`). At most 1 request per second per host and 15 pages per site; hard limits per page on time and bytes. Logins, CAPTCHAs and bot walls are never bypassed: they are recorded as "not assessed". |
| Writes | Crawling is a long operation that writes business data, so it runs under the **B0 maintenance lock** like Load Data. It writes results in batches, and the CRM keeps working through the restricted login (M2). |
| Storage | Per page: URL, final URL, status, timing, size, a hash of the extracted text, and desktop and mobile screenshots (WebP, size-capped) in the managed data folder. Every derived signal keeps its evidence (URL plus selector or quote) and provenance. |
| Untrusted content | Page text and HTML are data. They are never executed in LeadIntel's origin. AI providers (if enabled) get no tools, and claims without cited evidence are dropped (PLAN §6.1). |

## 2. Viewing websites from LeadIntel

The Friend Edition shell is an Edge **app-mode window**, not a WebView2 host, so the options are:

| Option | Assessment |
|---|---|
| **A. Evidence viewer (recommended default)** | LeadIntel shows the crawler's screenshots, the extracted facts and the check results. No third-party code ever runs in LeadIntel's window or origin. Works offline after the crawl. |
| **B. "Open live site" in an isolated window** | Opens the site in a separate Edge window with a **throwaway profile** (its own `--user-data-dir`, deleted afterwards). It shares no cookies or storage with LeadIntel's window, and the LeadIntel token never reaches it. This is what the current "Open website" link improves on. |
| C. iframe inside LeadIntel | Rejected. Many sites forbid framing. It would put third-party pages inside the app's frame and weaken the clear boundary of the CSP. |
| D. Native WebView2 shell | Only if a native shell is wanted later. Following [Microsoft's WebView2 security guidance](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security), the viewer would use: a separate environment and user-data folder; `AreHostObjectsAllowed`, `IsWebMessageEnabled` and `AreDefaultScriptDialogsEnabled` off; a `NavigationStarting` / `FrameNavigationStarting` policy; and no `AddScriptToExecuteOnDocumentCreated` script that exposes app data. That means a .NET or C++ host and a packaging change. Not needed for M3. |

**Protecting the local API from websites** (already strong; M3 adds tests, not new trust):
- **Existing (M1 S1):** the Host check stops DNS rebinding. Writes need the per-launch token and same-origin. CORS is never enabled, so other origins cannot read API responses.
- **Browser layer:** since Chrome 142 (and Edge, which follows Chromium), [Local Network Access](https://developer.chrome.com/blog/local-network-access) asks the user before a public site can reach loopback. Chrome 145 split it into local-network and loopback permissions, and enforcement later also covered WebSockets ([overview, 2026](https://piunikaweb.com/2026/02/09/chrome-local-network-permission-explained/)). The isolated window in option B keeps those prompts away from LeadIntel's own profile.
- **To add in M3:** a test that no `/api` response carries `Access-Control-Allow-Origin` and that cross-site requests are refused, plus a test that the throwaway profile is never LeadIntel's `edge-profile`.

## 3. Audit methodology (`none` provider: measured and heuristic)

Each criterion gives a value, evidence, a confidence, and a kind: **measured**, **heuristic** or **AI-only**.
A criterion that could not be assessed is excluded and lowers confidence. It never counts as poor ("unknown is not
poor").

| Dimension (SCORING §5) | Checks |
|---|---|
| Technical | Valid HTTPS and certificate dates, redirect chain, HTTP→HTTPS, response time, page weight, request count, mixed content, broken internal links, 4xx/5xx pages, console errors |
| Mobile / UX | Viewport meta, horizontal overflow at 375 px, tap-target size and spacing, font size, navigation reachable, click-to-call |
| Accessibility (feeds UX) | axe-core violations by impact (critical/serious weighted), image alt text, contrast failures, form labels, `lang` |
| SEO | Title and meta description (presence, length, uniqueness), one H1, canonical, sitemap, robots, LocalBusiness schema.org, NAP consistency with the registry and licence records |
| Effectiveness | Phone visible above the fold, contact form or booking, a clear call to action, service area named, hours, reviews or testimonials present (presence only) |
| Content | Pages for each core service (by niche profile), word count bands, duplicate or placeholder text ("lorem ipsum", default theme pages) |
| Modernity / maintenance | Layout technique (tables/floats vs flex/grid), outdated library versions (jQuery, WordPress core) by fingerprint, Flash, copyright year (weak), Wayback first and last capture, RDAP domain age |

Performance: lab metrics (LCP, CLS, long tasks as a proxy for total blocking time) from the browser's own
Performance APIs during the Playwright run. **Lighthouse is deferred (decided 2026-11-06)**: no Node.js is bundled.
[CrUX](https://developer.chrome.com/docs/crux/guides/crux-api) field data is free but needs a Google Cloud API key,
and returns 404 for origins without enough traffic, which covers most small local sites. Not used.

What the lighter approach **cannot measure reliably** without Lighthouse. These are reported as "not assessed" or
as low-confidence, never as poor:
- Lighthouse's calibrated performance score, its simulated throttling, and repeatable mobile CPU/network conditions
  (our timings depend on the PC and connection, so they feed only coarse bands);
- Lighthouse's audit catalogue beyond what we implement ourselves (for example unused JavaScript/CSS byte
  estimates, render-blocking resource savings, HTTP/2 and caching opportunity estimates);
- Lighthouse's SEO and best-practice scores as such (our own SEO checks cover the items that matter for local
  businesses).

## 4. WQS design and calibration

- **Formula unchanged:** WQS = 100 · Σ W_k·D_k / Σ W_k over the assessed dimensions. Confidence comes from the share
  of weight actually assessed. If less than 50 % of the weight is assessed, the result is "WQS provisional", shown
  with that label.
- **Fixture sites:** about 30 small synthetic HTML sites are committed to the repository. Each has known defects
  (no viewport, mixed content, missing H1, a table layout, …). Each check must report exactly the planted defects,
  as deterministic tests.
- **Golden set:** 60–100 real sites across the target niches, labelled by you in LeadIntel on a 1–5 rubric per
  dimension, with 20 of them labelled twice to measure consistency. A labelling screen is part of M3.
- **Acceptance gate:** Spearman ρ ≥ 0.7 between WQS and the human overall score, and ≥ 80 % agreement on the
  bands (< 60 / 60–69 / ≥ 70) that drive SCORING §1.2. The weights are re-fitted (non-negative, on the golden set)
  only if the gate fails, and changes are versioned in `scoring.yaml`. Any provider that fails the gate is not used
  for scoring (PLAN §6.2).
- **Invariants (tests):**
  - no website or inaccessible ⇒ N/A;
  - a criterion that was not assessed never lowers the WQS;
  - WQS ≥ 85 with a high peer percentile ⇒ BOS ≤ 25 (already in SCORING §7).

## 5. Industry peer benchmarking

- **Peers:** same niche profile, same county, and a confirmed independent website. If there are fewer than 8 peers,
  widen to the target region. If there are still fewer than 8, report "not enough peers" and use no peer factor.
- **Reported:** the prospect's percentile, the peer median and top-quartile WQS, the share of peers with a website,
  and the per-dimension gaps. The largest gaps become plain-language sales talking points, each with its evidence.
- **Feeds:** BOS "competitive pressure" and "website need: gap to local peers" (SCORING §4.1). Peer sets are stored
  with the scoring run, so scores stay reproducible.

## 6. Data-source research

**Revenue** (influences mainly LPS; no free per-business source exists):

| Source | What it gives | Cost / access | Use |
|---|---|---|---|
| [Census Nonemployer Statistics](https://www.census.gov/data/developers/data-sets/nonemp-api.html) | Receipts and establishment counts by NAICS, at **county** level (no ZIP level); disclosure avoidance from reference year 2025 | Free; **all Census API queries now need a free key** (email registration) | An area-and-industry prior ("typical receipts for a sole proprietor in this niche and county"), labelled as an estimate |
| County / ZIP Code Business Patterns | Establishments, employment and payroll by NAICS (ZIP level: establishments only) | Free (same key) | Market size and density for peer context; not revenue |
| [SAM.gov entity data](https://open.gsa.gov/api/entity-api) | Federal registrants: UEI, NAICS, small-business flags | Free key; 10 requests a day without a SAM role; [bulk extracts](https://open.gsa.gov/api/sam-entity-extracts-api/) recommended | **Not approved (2026-11-06)**: possible future source only; no account, no dependency |
| Data Axle | Modelled revenue and employee ranges | Paid: reportedly from $99 a month self-serve, about $0.065 a record for lists ([2026 comparison](https://prospeo.io/s/data-axle-pricing-reviews-pros-and-cons)) | Only as an approved paid trial |
| ZoomInfo / D&B / Apollo | Company revenue estimates, strongest for larger firms | ZoomInfo is annual and custom, reportedly about $15k–40k a year; Apollo publishes per-user tiers ([comparison](https://crustdata.com/blog/zoominfo-vs-apollo)) | Poor fit for micro-businesses; not recommended |

**Decided (2026-11-06):** an LPS revenue prior from Census county × NAICS data, plus existing size signals
(licence class, locations, filing age), all labelled "estimated". The free Census API key is approved; no paid
services. The key is configuration, never source: `LEADINTEL_CENSUS_API_KEY` in development, and on Windows a
DPAPI-encrypted secret that Setup asks for, like the other secrets. Without a key the prior is simply "not
available". A paid provider only after a priced trial is approved.

**Official directories and public datasets** to evaluate next (free):
- SAM.gov bulk entity extract (future only; not approved);
- SBA Dynamic Small Business Search;
- Florida Office of Supplier Diversity certified vendors;
- Florida Department of Health and FDACS licence lists for the relevant niches;
- remaining county business-tax-receipt exports.

Each needs a terms-of-use check and a field mapping before ingestion. None changes WQS.

**Google OAuth and Gmail** (official pages checked):
- **Scope classes** ([Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)):
  - **sensitive** (verification with a justification and demo, no security assessment): `gmail.send`;
  - **restricted**: `mail.google.com`, `gmail.readonly`, `gmail.compose` (drafts), `gmail.insert`, `gmail.modify`,
    `gmail.metadata`, `gmail.settings.*`. Google requires a security assessment if restricted-scope data is stored
    on or transmitted through servers.
- **Testing mode:** an external consent screen in "Testing" gets refresh tokens that **expire after 7 days**
  ([OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2)), so weekly re-consent would be
  needed.
- **Desktop apps:** use the "Desktop app" client type with loopback redirect and PKCE ([native-app guide](https://developers.google.com/identity/protocols/oauth2/native-app)).
- **Decided (2026-11-06): `mailto:` only.** No Gmail OAuth and no Gmail credentials are collected.
  - Email actions open a `mailto:` link (recipient, subject and body pre-filled, values URL-encoded and limited in
    length). Windows hands it to the default mail application: Outlook, Mail, or Gmail in the browser if the user
    set Gmail as the `mailto` handler.
  - Fallback when no mail application responds: the same message is shown with **Copy address** and **Copy
    message** buttons, so it can be pasted into any webmail.
  - `gmail.send`-only OAuth stays documented as a possible later option; it would need a new approval.

**Licensing and costs:**

| Item | Licence / cost | Note |
|---|---|---|
| Lighthouse | Apache-2.0 ([licenses.dev](https://licenses.dev/npm/lighthouse/9.6.8)) | Deferred: would need a bundled Node.js runtime |
| axe-core | MPL-2.0 ([licenses.dev](https://licenses.dev/npm/axe-core/4.7.0)) | Ship the unmodified file with its licence notice. MPL is file-level copyleft and does not affect LeadIntel's own code |
| Playwright (Python) | Apache-2.0 (to confirm from the package metadata at implementation time) | Uses the installed Edge, so no browser download |
| Microsoft Edge / WebView2 Runtime | Free, already installed on Windows 10/11 | — |
| Census API | Free key | Approved; kept out of source control |
| CrUX API, SAM.gov API | Free, but each needs a key or account | Not approved / not used |
| Data Axle / ZoomInfo / D&B / Apollo | Paid | Not activated |

## 7. Search Area: adjustable radius search (new, requested 2026-11-06)

Goal: narrow prospecting to a circle on the map. The circle is **one more filter** in the shared `ProspectFilter`,
not a separate search mode. Because the list, counts, summary, map points, CSV export and sales filters are all
built from that one filter (M1 S2a), they cannot disagree about which businesses match.

### 7.1 Behaviour

- **Off until enabled.** Without an area, LeadIntel behaves exactly as today. **Turning it on** places a **10-mile**
  circle (16.09 km) at the chosen center; center and radius are then adjustable.
- **Setting the center.** Any of:
  - a search box for an address, ZIP code, city or business name;
  - **Search around here** on the map, using either the right-click menu or a single click while **Edit area** is on;
  - dragging the center marker.

  Panning or zooming the map **never** moves the center. Setting a center fits the map to the circle once.
- **Radius.**
  - A slider (finer steps at small distances) plus an editable number field.
  - Units: miles or kilometres. Default 10 mi (shown as 16.1 km in kilometres); minimum 0.25 mi / 0.4 km; maximum
    100 mi / 160 km.
  - Switching units converts the shown number without changing the search: the radius is stored in metres.
  - Out-of-range input is clamped, with a short note.
- **On the map.**
  - The circle is drawn as a filled outline with a distinct center marker.
  - The circle is the database's own geodesic buffer (§7.2), so what you see is what is searched. It is
    longitude/latitude GeoJSON projected by the map like every other feature, so it marks the same ground boundary
    at every zoom level.
  - The circle and the results update together: after any change of center or radius, the circle, the list, the
    counts and the map points come from the same request generation (older answers are discarded).
  - While dragging, a local approximation is drawn, then replaced by the server's circle on release.
- **Always visible when active.**
  - A chip in the filter panel and the result bar, for example "Within 10 mi of 32207 · 1,234 businesses", with
    **✕** to clear it.
  - The map caption adds "inside the search area".
  - Businesses that match the other filters but have no location are counted separately ("N have no location and
    are not included").
- **Persistence.**
  - The area lives in the URL state (`near=lat,lon`, `r` in metres, `ru=mi|km`, plus a display label), like every
    other filter.
  - It stays active while you inspect a business, switch list / map / split, change other filters, or reload.
  - **Clear all** clears it too. Clearing the area returns to the normal search with the other filters kept.
- **Sorting.** "Distance" becomes available while an area is active, and each list row shows its distance.

### 7.2 Server design: no migration needed

Facts checked on `leadintel_test` (same schema as installations): `location.geom` is `geometry(Point,4326)` with a
GiST index (`idx_location_geom`); the Alembic graph is linear with head `0006`.

- **Filter fields.** `ProspectFilter.near_lat`, `near_lon` and `radius_m`: all three or none. Validated: finite
  values, latitude within ±85°, radius 400–160,934 m. Anything else returns 422.
- **WHERE clause**, added to the shared `where()`, so every view inherits it:
  1. `l.geom IS NOT NULL`;
  2. an index-backed bounding-box prefilter on the geometry, `l.geom && ST_Expand(center, dx, dy)`. `dx` and `dy` are
     computed in degrees with a 1 % safety margin, using the widest longitude span within the box;
  3. the exact test, `ST_DWithin(l.geom::geography, center::geography, radius_m)`. This is geodesic on the WGS84
     spheroid and **inclusive**: distance ≤ radius counts as inside.

  Center and radius are bound parameters only.
- **Distance.** `ST_Distance(...)` is returned as `distance_m` in list rows and the export; there is a new sort
  `distance`. Distances are shown in the chosen unit.
- **Circle endpoint.** `GET /api/geo/circle` returns GeoJSON from `ST_Buffer(center::geography, r, 'quad_segs=32')`.
  It also returns whether the circle extends beyond the loaded target counties, which drives the hint "part of this
  area is outside the counties LeadIntel has data for". This is a read-only GET under the existing security rules.
- **Finding the center** (`GET /api/geo/search?q=`), in order:
  1. a business name in the database (its own coordinates);
  2. a 5-digit ZIP code, using the Census ZCTA gazetteer centroid;
  3. a city or place, using the Census places gazetteer centroid;
  4. a street address, using the Census Geocoder one-line endpoint.

  Steps 2 and 3 use files that Load Data downloads once from `www2.census.gov`, a host already used. They are free
  and need no key. Step 4 uses the same free, keyless Census geocoder the pipeline already uses, through the
  existing safe-egress client. Each result shows its source and precision. Steps 1–3 work offline.
- **Coordinate quality.**
  - Businesses without coordinates are excluded from the area and counted (§7.1).
  - Businesses whose coordinates come only from a ZIP-centroid geocode of their operating address are kept but
    flagged **approximate location**, in the list, details and export. This is computed at query time from
    `address.geocode_method`, so no column is added.
  - An optional "precise locations only" switch excludes them.
- **Combining.** Area AND county AND every other filter (industry, website status, WQS, BOS, LPS, revenue,
  contact, sales status / ownership / follow-up / list). WQS and revenue are added when M3 provides them. The circle
  never widens a county filter, and no filter silently overrides another: every active constraint is shown.
- **Sales overview.** The pipeline and follow-up figures stay global (they describe your whole book of business).
  Each overview figure links to the list with the current area kept, so "my overdue follow-ups in this area" is one
  click.
- **Large results.** Paging, the map-point cap (120,000) and the export cap (100,000) are unchanged, and truncation
  is reported as today. Performance target at 100k synthetic locations, 25-mile radius: count and page 1 ≤ 250 ms,
  map points ≤ 300 ms.

### 7.3 Saved searches

A saved search stores a name and the full filter state, including center, radius, unit and label. Storing them
needs a table: `crm_saved_search` (organization, name, filter JSON with a version number, created by, timestamps).
It is planned for migration **0007** together with the M3 schema, and only if M3 needs 0007 anyway. The `crm_`
prefix puts it under the M2 restricted sales login: the grant list and self-check are extended, with tests. Until
then the URL state already keeps an area across reloads.

### 7.4 Dependencies

- **Already present:** PostGIS geography functions, the GiST index, MapLibre (GeoJSON fill and line layers; a
  draggable `Marker`), the Census geocoder client and safe egress.
- **New:** two free Census gazetteer files (ZCTA and places), downloaded by Load Data. No new library, no account,
  no migration for the core feature.
- **Without internet:** the map tiles need internet, but the area works without the map. Set the center with the
  search box (business, ZIP or city from local files) and the radius with the number field.

### 7.5 Acceptance tests (written before implementation; `leadintel_test`, synthetic data)

1. **Boundary.**
   - Points placed by `ST_Project` at radius − 1 m and at exactly the radius are inside; radius + 1 m is outside.
   - This holds at several bearings and at Florida latitudes, and in both units for the same metre value.
2. **Geodesic correctness.** For about 200 random synthetic points, the API's inside/outside set equals an
   independent Python computation of geodesic distance on the WGS84 ellipsoid. Points within 0.5 m of the boundary
   are skipped, because ellipsoid formulas differ there by centimetres.
3. **Index safety.** No point inside the radius is lost by the bounding-box prefilter. This is tested with radii up
   to 160 km and centers at different latitudes (the widest longitude span).
4. **One answer everywhere.** With the area combined with each existing filter (county, niche, qualification,
   website status, operating status, phone, email, BOS / LPS ranges, sales status, claim, follow-up, list, and later
   WQS and revenue ranges), these all agree with each other and with an independent Python model, on every page with
   no gaps or duplicates:
   - list total and pages;
   - count;
   - summary;
   - map points;
   - CSV rows.
5. **Coordinates.** Businesses without coordinates are never inside, and are reported in the summary count.
   Approximate-location businesses are flagged; "precise only" excludes exactly those.
6. **Combining.** County ∧ area is the intersection; a circle reaching outside the target counties sets the hint;
   clearing the area restores the previous results exactly.
7. **Validation.** Missing one of the three parameters, NaN/∞, latitude > 85°, a radius outside the limits, or text
   in numeric fields: all return 422 and never reach SQL.
8. **Circle.** The endpoint's GeoJSON vertices lie at radius ± 0.1 % from the center. The radius label and the
   circle are drawn from the same metre value.
9. **Center movement and units.**
   - Panning or zooming never changes the center or the counts; a dragged marker updates the center once, on release.
   - The same radius in miles and kilometres gives identical results; switching units never changes counts, and the
     displayed value round-trips (10 mi → 16.1 km → 10 mi).
   - The circle and the results stay synchronised: a late answer for an old center is never shown.
   - Off by default: with no area parameters, every result equals today's.
10. **Persistence.** The area survives inspecting a business, switching view, changing filters and reloading.
    **Clear all** and **✕** remove it.
11. **Large sets.** 100k synthetic locations: the performance targets in §7.2 are met; truncation is reported
    consistently in the points and export headers.
12. **Center search.** Business, ZIP and city lookups work offline; the address lookup goes only through the
    safe-egress client (no private or loopback destinations).
13. **Saved searches** (with 0007): the area and all filters are saved and restored exactly, and the restricted
    sales login can write only its own table.
14. **Headless walkthrough.**
    - Set the center by search, by map click and by drag; adjust the slider and the number field; switch units.
    - Export, and compare the CSV row count with the shown count.
    - Clear the area.
    - No security-policy violations and no horizontal overflow; light and dark themes.

## 8. Proposed M3 implementation order

1. **Search Area (radius)**: §7, without saved searches. It depends only on existing coordinates and needs no
   migration.
2. Egress proxy for the browser, with SSRF and DNS-rebinding tests (synthetic local servers only).
3. Crawl runner: robots, rate, limits, fresh contexts, screenshots, under the maintenance lock.
4. Measured and heuristic checks, plus axe-core, with the fixture-site suite.
5. Migration **0007** (additive, tested only on `leadintel_test`): crawl, page and evidence tables, WQS results, and
   `crm_saved_search`.
6. WQS with confidence and invariants. Presence categories 7 and 8 and BOS/LPS inputs switch from provisional to
   measured. WQS range filter added to `ProspectFilter`, which the area therefore combines with.
7. Peer benchmarks and sales talking points; Census revenue prior (with the approved key) and a revenue-range filter.
8. Evidence viewer (option A) and the isolated live-site window (option B); `mailto:` actions with copy fallback.
9. Saved searches; golden-set labelling screen and the calibration report.

## 9. Decisions

Recorded 2026-11-06:

| Question | Decision |
|---|---|
| Migration number | 0007 is reserved for M3, used only if M3 really needs a schema change (it does, from step 5: crawl/WQS results and saved searches). Additive, tested only on `leadintel_test`. B0.5 deferred; it takes the next free number if still needed. |
| Census API key | Approved (free). Configurable, never in source control. |
| SAM.gov | Not approved; documented as a possible future source only. |
| Lighthouse | Deferred; no Node.js bundled. Limits documented in §3. |
| Gmail | `mailto:` with a copy fallback; no Gmail OAuth and no Gmail credentials. |
| Search Area | Approved as a first-class filter (§7); polygons out of scope. |

Open (none blocking the next steps): on Windows, the Census key will be asked for by Setup and stored with DPAPI.
Creating the key itself (an email registration at census.gov) is something you do; LeadIntel only stores it.
