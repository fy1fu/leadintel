# LeadIntel delivery status

Engineering progress tracker (kept short; the plan lives in [PLAN.md](PLAN.md) and [ROADMAP.md](ROADMAP.md)).
Updated: 2026-11-06.

## M1 - salesperson interface: accepted as the baseline (2026-11-06)

| Stage | What | State | Commit |
|---|---|---|---|
| S1 | Local web-app hardening: Host check, per-launch token, same-origin rule, CSP with nonces, safe links | done | `2b419529` |
| S2a | Shared `ProspectFilter`; paginated list, summary and map-point endpoints | done | `bf181e6c` |
| S2b | Verified on PostgreSQL (`leadintel_test`, synthetic data); list query rewrite (deep pages 1.25 s -> 0.18 s at 100k rows); `limit=0` compatibility | done | `8d1b6fba` |
| S3 | CSV export endpoint and CLI export on the same filter | done | `8d1b6fba` |
| S4-S7 | Interface redesign: filter panel, paging, honest counts, map caption, salesperson detail view, Advanced section, themes, keyboard use | done, Windows check pending | `b64ba3a9` |
| S8 | Edge app-mode window (maximized), full-screen button and F11, `-Fullscreen` test switch, default-browser fallback | done, Windows check pending | `aa4eb2de` |
| S9 | M1 TEST ZIP (`LeadIntel-1.1.0-m1-TEST.zip`, from `aa4eb2de`) and the consolidated Windows checklist | handed over; results pending | - |

## Current milestone: M2 - local CRM (single user)

| Part | What | State |
|---|---|---|
| Schema | Migration `0006` (single head): `crm_organization`, `crm_user` (UUID ids), `crm_prospect` (one row per organization and business, unique), `crm_activity` (append-only), `crm_list`, `crm_list_item`. Additive; downgrade drops only these tables | done |
| Service | Profile setup, claim / release / reassign, pipeline status, next action, follow-up date, notes and contact history (call, email, meeting, visit), lists and campaigns, dashboard | done |
| Concurrency | Claim is a conditional update (two simultaneous claims: exactly one wins); edits use an optimistic `version` (stale saves get a clear "changed elsewhere" message) | done |
| Write safety | Security review (see [security/M2_CRM_WRITE_PATH.md](security/M2_CRM_WRITE_PATH.md)): the original text-only guard was bypassable (8 demonstrated ways) and is replaced. Default: CRM writes take the maintenance lock (B0 unchanged; "paused" during Load Data). Writes during Load Data only through a separate `leadintel_crm` login that PostgreSQL restricts to the CRM tables, verified by a self-check before use; any doubt falls back to the lock. Upgrades exclude CRM writes via an advisory key | done (`03080b3f`, fix `4edf41ac`) |
| API | `/api/crm/*` (token and same-origin rules apply to every write); sales filters on list, counts, map and export (`sales`, `claim`, `follow_up`, `list_id`, sort by follow-up); CRM columns in the CSV export | done |
| Interface | Profile dialog, Sales section in the business view (claim, status, next action, follow-up date, activity log and timeline, lists), sales filters and list tags, Sales overview (pipeline, follow-ups, recent activity, lists and campaigns) | done |
| Setup | Creates the `leadintel_crm` login (no role attributes, NOINHERIT, own DPAPI secret); `db upgrade` sets its privileges | done, Windows check pending |
| Builder | Secret scan: one exact allow-list entry for the DPAPI-backed sales URL template in `common.ps1`; patterns unchanged | done (`ffa76b92`) |
| Windows | Combined M1+M2 procedure on a separate Windows test account ([windows/M1_M2_TEST_PROCEDURE.md](windows/M1_M2_TEST_PROCEDURE.md)); profile dialog states whether sales changes continue during Load Data | procedure ready; M2 TEST ZIP not built (awaiting your decision) |

## Test results (latest)

- Full suite on `leadintel_test` (synthetic data, migrated to 0006): 976 passed, 6 skipped (Search Area, center
  search, crawler egress proxy, crawl runner and website checks).
- Crawl runner (`tests/test_crawl_runner.py`, 12 tests, real headless Chromium through the egress proxy, synthetic
  sites):
  - robots.txt honoured (path rules, full and bot-specific disallow, 5xx = not crawled, Crawl-delay);
  - page limit and request interval;
  - off-site, non-HTML and mailto links not followed;
  - a redirect into the private network refused, and the private service never contacted, also not via a pop-up;
  - unsafe start URLs refused before any request;
  - JPEG screenshots (desktop for every page, mobile for the home page); writing without the maintenance lock fails
    with nothing written.

  Python Playwright 1.63.0 (Apache-2.0) is an optional `crawl` extra; these tests skip where it is not installed.
- Website checks (`tests/test_website_checks.py`, 5 tests): a good and a bad fixture site crawled through the real
  browser and proxy; each of the 19 criteria reports exactly the planted outcome; what cannot be assessed stays
  "not assessed"; a site that could not be crawled has no criteria.
- Crawler egress proxy (`tests/test_browser_proxy.py`, 50 tests, offline, synthetic local servers):
  - credentials required; loopback-only listener;
  - HTTP forwarding and CONNECT tunnels go to the address the proxy checked;
  - 11 kinds of private/special address refused by IP and by name; internal hostnames refused without DNS;
  - DNS answers pointing inside refused, including mixed answers; one resolution per connection, so a changing
    answer cannot redirect;
  - only allowed ports; paid-provider hosts refused;
  - malformed, oversized and credential-bearing requests refused; byte limit; connection cap; DNS failure reported.

  End-to-end with real Chromium: public synthetic site loads; 127.0.0.1, localhost and a rebinding name get the
  proxy's 403; page-script fetches to the private service fail; the private service received 0 requests.
- Center search (`tests/test_geo_search.py`, 18 tests, network mocked):
  - Gazetteer parsing and the Load Data download/reduce step: needs the maintenance lock; keeps the previous file if
    the download looks empty; never makes a run partial;
  - one-line geocoder parsing, with malformed and hostile answers dropped; only registered hosts are reachable;
  - API: business by name (own location; no-location and merged businesses left out); ZIP and city (with a note
    until the first Load Data); address only when asked, with a Florida retry; failures reported; inside/outside
    target counties marked; input bounded; hostile text harmless.
- Center-search walkthrough (real Census Gazetteer subset, address lookup routed in the test), 29 checks at 1920×1080
  and 1366×768:
  - ZIP, city, business and street address each set the center;
  - the radius is kept; outside-county places are marked;
  - stale result lists are discarded; failures are reported;
  - Back works; other filters are kept;
  - no page errors and no CSP violations.

  The 97-check Search Area walkthrough still passes at all three viewports.
- Search Area (`tests/test_search_area.py`, 112 tests):
  - the boundary is inclusive at 8 bearings, with ±1 m checked;
  - results agree with an independent WGS84 (Vincenty) computation at 0.25–18 mi;
  - miles and kilometres give identical results;
  - list, count, summary, map points and CSV agree under 11 filter combinations;
  - an area never widens a filter, and clearing it restores the previous result exactly;
  - distance column and sort, approximate-location flag and precise-only;
  - sales filters combined with the area;
  - the box prefilter loses nothing at 25–60° latitude and 100 mi;
  - 16 kinds of invalid input are refused on 4 endpoints;
  - one canonical range (400 m to 160,934.4 m) accepted at both ends in miles and km, just outside refused;
  - the circle endpoint matches the geodesic radius within 0.1 %.
- Search Area frontend: headless walkthrough with the real MapLibre 4.7.1, 97 checks each at 1920×1080, 1440×900
  and 1366×768. Covered:
  - off by default;
  - marker drag versus map panning and zooming;
  - pick on map;
  - slider and number synchronised; units; limits;
  - other filters combined; selection kept;
  - distance sort; export rows = count shown;
  - Back, Forward, reload and shared URL;
  - radius exact to 0.01 m in state, URL and server (10 mi stays 16,093.44 m through reload, units, Back/Forward);
  - one canonical range in metres (400 m to 160,934.4 m), unit limits derived from it: switching units at either
    limit never changes or clamps the radius; out-of-range input is clamped in metres only;
  - stale responses discarded; error, empty and outside-county notes;
  - no page errors and no CSP violations.

  The M2 CRM walkthrough still passes.
- Search Area performance (100k synthetic locations, about 4× realistic density; rolled back). Every area query
  uses the GiST index:
  - 5 mi: 8–56 ms;
  - 10 mi: 22–152 ms;
  - 15 mi (about 39k matches): 49–230 ms, within the 250 ms target;
  - 25 mi (80k matches, 88 % of all data): count 0.5 s, page 1 about 1.1 s. This is a measured planner limit:
    there are no statistics for the geography expression. The fix, if real data needs it, is a GiST index on
    `geom::geography` (an additive migration). Not added now (no unnecessary migration).
- CRM security (`tests/test_crm_security.py`): 31 attacks refused by PostgreSQL with no application check and again
  through the application; 10 kinds of tampering each force the maintenance-lock mode; Load Data / upgrade
  interplay; the real `db upgrade` command sets exactly the CRM privileges.
- M2 tests (`tests/test_crm.py`): schema and single Alembic head, downgrade/upgrade of 0006 keeps business rows,
  identity, claims (8 concurrent users: exactly one wins), permissions, versions, rollback on failure, activities,
  lists, dashboard, write guard, upgrade refusal, API token rules, sales filters agree across list / count /
  summary / map / export, follow-up periods checked under three server time zones.
- Headless walkthrough (3,000 synthetic businesses): first-use profile, claim, save, overdue tags, activity log,
  new list, stale-save conflict, overview and its filters, reload keeps filters, release; hostile text inert;
  no CSP violations; no horizontal overflow at 1366 / 1280 / 800 px; light and dark.
- Performance at 100k synthetic locations (M1): list page 1 79 ms, page 400 182 ms, count 124 ms.

## Windows checks still required

- Everything in the M1 checklist (map under the CSP, Edge app window, F11 / `-Fullscreen`, scaling,
  `test-dataload.ps1`, upgrade keeps data).
- M2: upgrade to 0006 on a copy of real data, then claim / notes / overview on Windows.
- M3 Search Area: area panel, marker drag and pick-on-map with the real map tiles, circle, unit switching, Back/Forward.
  ZIP-code and city search need one Load Data after installing (it downloads about 1.2 MB of Census Gazetteer files
  from www2.census.gov, a host already used); street addresses use the free Census geocoder on demand.

## Open security findings

- R1 (low): the CRM login can create temporary tables / large objects and take advisory locks (PostgreSQL defaults;
  no business data). Pinned by a test.
- R2 (pre-existing): B0 can allow one commit after a lost lock; fencing token (B0.5) planned as migration 0007.
- R3: the Setup step that creates the sales login has not run on Windows yet (falls back safely if it fails).

## Known issues / limitations

- WQS does not exist yet (M3); BOS and LPS are provisional (listing data only).
- The map needs internet access (MapLibre library and OpenFreeMap tiles); the list works without it.
- Export is limited to 100,000 rows per file.
- Closing the app window does not stop the server; Ctrl+C in the launcher window or "Stop LeadIntel" does.
- CRM is single-user: one local organization and profile; extra users exist only in the schema (for later).
- Follow-up periods use the database server's clock (the same PC in the Friend Edition).

## Outstanding approvals (high-impact)

- Push of the local `phase-1` commits (GitHub is at `b20f99a`).
- Installing an M2 build on the Windows Friend Edition (applies migration 0006 to the real database).
- Final distributable release ZIP rebuild (only TEST ZIPs are built).
- B0.5 (fencing token) migration: deferred; it takes the next free number after M3's 0007 if still needed.
- Licensing review before distribution beyond friends (PostGIS GPL, app-local VC++ runtime).

## Next milestone: M3 - search area and website intelligence

Plan, architecture, acceptance tests and decisions: [research/M3_PREP.md](research/M3_PREP.md).

| Step | What | Migration | State |
|---|---|---|---|
| 1 | **Search Area** (core feature, [PLAN.md §14](PLAN.md)): off until enabled, then a 10-mile circle with adjustable center (search, map click, marker drag; panning never moves it) and radius (miles/km); server-side PostGIS geodesic filter on the existing index in the shared `ProspectFilter`, so list, counts, map, export and sales filters agree | none | **done** (server filter; Area panel and chip, draggable center marker, server circle, slider and number with mi/km, pick-on-map, URL with Back/Forward, stale-response guards; center search by business, ZIP code, city and street address). Windows check pending |
| 2 | Browser egress proxy (SSRF / DNS rebinding) | none | **done** (`app/net/browser_proxy.py`; same rules as the website fetcher; Chromium flags in `chromium_args()`) |
| 3 | Crawl runner (robots, rate limits, isolated contexts, screenshots, maintenance lock) | none | **runner done** (`app/crawl/runner.py`; results returned as data; storage with 0007 in step 5; not yet run from Load Data or a command) |
| 4 | Measured / heuristic checks and axe-core; fixture-site suite | none | **first slice done** (`app/crawl/checks.py`: 19 criteria across the 7 WQS dimensions from read-only DOM facts, measured or heuristic, unknown = not assessed; 2 fixture sites). Still to do: axe-core (vendored, MPL-2.0), performance timings, Wayback/RDAP history, the fuller ~30-site fixture suite |
| 5 | Schema: crawl / page / evidence, WQS results, saved searches | `0007` (additive, `leadintel_test` only) | planned |
| 6 | WQS with confidence; BOS/LPS inputs from measurement; WQS filter | - | planned |
| 7 | Peer benchmarks; Census revenue prior (approved free key) and revenue filter | - | planned |
| 8 | Evidence viewer, isolated live-site window, `mailto:` with copy fallback | - | planned |
| 9 | Saved searches; golden-set labelling and calibration report | - | planned |

Decisions recorded 2026-11-06:
- 0007 is reserved for M3; B0.5 is deferred.
- Census key approved (free, kept out of source control); SAM.gov not approved.
- Lighthouse deferred (no Node.js).
- Email through `mailto:` only; no Gmail OAuth.

## Next exact action

1. Your decision on building the M2 TEST ZIP (the previous M2 TEST ZIP, from `03080b3f`, was deleted before the
   security fix and never distributed).
2. M3 step 5: migration 0007 (crawl, page, evidence and criteria tables, WQS results, saved searches) on
   `leadintel_test`, and a `crawl` command that runs the crawler under the maintenance lock and stores results.
   In parallel: finish step 4 (axe-core, performance timings, Wayback/RDAP, the fuller fixture suite).
