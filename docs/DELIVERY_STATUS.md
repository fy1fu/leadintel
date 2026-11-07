# LeadIntel delivery status

Engineering progress tracker (kept short; the plan lives in [PLAN.md](PLAN.md) and [ROADMAP.md](ROADMAP.md)).
Updated: 2026-11-06 (M3 complete; Windows TEST session reported successful).

## M1 - salesperson interface: accepted as the baseline (2026-11-06)

| Stage | What | State | Commit |
|---|---|---|---|
| S1 | Local web-app hardening: Host check, per-launch token, same-origin rule, CSP with nonces, safe links | done | `2b419529` |
| S2a | Shared `ProspectFilter`; paginated list, summary and map-point endpoints | done | `bf181e6c` |
| S2b | Verified on PostgreSQL (`leadintel_test`, synthetic data); list query rewrite (deep pages 1.25 s -> 0.18 s at 100k rows); `limit=0` compatibility | done | `8d1b6fba` |
| S3 | CSV export endpoint and CLI export on the same filter | done | `8d1b6fba` |
| S4-S7 | Interface redesign: filter panel, paging, honest counts, map caption, salesperson detail view, Advanced section, themes, keyboard use | done; Windows reported successful by the user (combined M1 → M3 session, 2026-11-06) | `b64ba3a9` |
| S8 | Edge app-mode window (maximized), full-screen button and F11, `-Fullscreen` test switch, default-browser fallback | done; Windows reported successful by the user (combined M1 → M3 session, 2026-11-06) | `aa4eb2de` |
| S9 | M1 TEST ZIP (`LeadIntel-1.1.0-m1-TEST.zip`, from `aa4eb2de`) and the consolidated Windows checklist | handed over; superseded by the combined M1 → M3 session | - |

## M2 - local CRM (single user): done; Windows session reported successful

| Part | What | State |
|---|---|---|
| Schema | Migration `0006` (single head): `crm_organization`, `crm_user` (UUID ids), `crm_prospect` (one row per organization and business, unique), `crm_activity` (append-only), `crm_list`, `crm_list_item`. Additive; downgrade drops only these tables | done |
| Service | Profile setup, claim / release / reassign, pipeline status, next action, follow-up date, notes and contact history (call, email, meeting, visit), lists and campaigns, dashboard | done |
| Concurrency | Claim is a conditional update (two simultaneous claims: exactly one wins); edits use an optimistic `version` (stale saves get a clear "changed elsewhere" message) | done |
| Write safety | Security review (see [security/M2_CRM_WRITE_PATH.md](security/M2_CRM_WRITE_PATH.md)): the original text-only guard was bypassable (8 demonstrated ways) and is replaced. Default: CRM writes take the maintenance lock (B0 unchanged; "paused" during Load Data). Writes during Load Data only through a separate `leadintel_crm` login that PostgreSQL restricts to the CRM tables, verified by a self-check before use; any doubt falls back to the lock. Upgrades exclude CRM writes via an advisory key | done (`03080b3f`, fix `4edf41ac`) |
| API | `/api/crm/*` (token and same-origin rules apply to every write); sales filters on list, counts, map and export (`sales`, `claim`, `follow_up`, `list_id`, sort by follow-up); CRM columns in the CSV export | done |
| Interface | Profile dialog, Sales section in the business view (claim, status, next action, follow-up date, activity log and timeline, lists), sales filters and list tags, Sales overview (pipeline, follow-ups, recent activity, lists and campaigns) | done |
| Setup | Creates the `leadintel_crm` login (no role attributes, NOINHERIT, own DPAPI secret); `db upgrade` sets its privileges | done; Windows reported successful by the user (combined M1 → M3 session, 2026-11-06) |
| Builder | Secret scan: one exact allow-list entry for the DPAPI-backed sales URL template in `common.ps1`; patterns unchanged | done (`ffa76b92`) |
| Windows | Combined M1 → M3 procedure on a separate Windows test account ([windows/M1_M3_TEST_PROCEDURE.md](windows/M1_M3_TEST_PROCEDURE.md)); profile dialog states whether sales changes continue during Load Data | reported successful by the user (combined M1 → M3 session, 2026-11-06) with `LeadIntel-1.3.0-m3-TEST.zip` |

## Test results (latest)

- Full suite on `leadintel_test` (synthetic data, migrated to 0007), latest: **1055 passed, 6 skipped** (7.5 min).
  One earlier run had a single failure in a timing test (`test_crm_security.py::
  test_lock_mode_waits_briefly_behind_another_crm_write`, 5 s retry window under load); it passed 5/5 in isolation
  and in the following full run. Watch for it; if it recurs, widen the test's margin, not the product's retry.
- M3 website checks and audits (all against synthetic local sites on 127.0.0.2; a "private" service on 127.0.0.1
  must never be reached):
  - 33 fixture sites (`tests/test_website_fixtures.py`): good, bad, inaccessible (robots, 404, 500, redirect into
    the private network, PDF home), partially assessable, unusual (JS-rendered, redirects, @graph data, Flash,
    dialogs, Crawl-delay), malicious (exfiltration attempts, page-initiated POST/beacon/form submit, meta refresh to
    a private address), misleading metadata, broken and empty links, missing content, responsive problems, a 3 MB
    page. Each asserts the outcome and the planted findings; what cannot be assessed is "not assessed", never a
    failure; nothing but GET reaches the sites;
  - watchdog (`tests/test_crawl_watchdog.py`): a page that never yields stops the crawl in about 10 s with the pages
    loaded before; no browser processes are left;
  - domain history (`tests/test_domain_history.py`): Wayback/RDAP parsing; junk, errors and timeouts become
    "unavailable" / "not found", never a guessed date;
  - WQS, recommendations, calibration (`tests/test_wqs.py`, offline);
  - audits on PostgreSQL (`tests/test_website_audits.py`): migration 0007 up/down keeps business data; end-to-end
    audit with real Chromium (findings, evidence, WQS score row and factors, screenshots with checksums); bad site
    gives 3-5 ordered recommendations; re-run keeps history; a failed re-run keeps the previous results and WQS;
    robots-blocked and partial outcomes; stale "running" becomes interrupted; browser start failure is explained;
    screenshot retention (newest 3 audits); artifact path traversal and checksum refusal; one audit at a time; an
    audit and Load Data exclude each other; benchmark threshold (7 peers: none; 8: county; widening to the target
    region, never beyond, never other niches); BOS/LPS rescoring keeps the WQS; API token rules; `crawl` command;
    saved searches (round trip, validation like a live search, token rules).
- M3 interface walkthrough (headless, real MapLibre, 3,000 synthetic businesses), 29 checks each at 1920×1080,
  1440×900 and 1366×768: run an audit, running state, WQS with Provisional badge, report (7 areas, grouped findings,
  benchmark, screenshots served by the app, live site only as an external-browser link, no iframe), bad site with
  ordered recommendations, re-run history, save / restore / delete a search (area in metres, unit, label, filters,
  sort, layout), no page errors, no CSP violations. The 97-check Search Area walkthrough still passes.
- M3 performance:
  - audit queries at 6,000 audits / 156k findings / 60k pages (synthetic, rolled back): history 0.2 ms, full report
    1.6 ms, benchmark 7.7 ms; each 0007 index is used by its query (EXPLAIN); the peer query's sequential scan takes
    2.3 ms, so no further index is justified;
  - one 15-page audit: 15.4 s at one page per second; peak browser memory about 0.6 GB (sum over processes);
    3.6 MB of screenshots (kept for the newest 3 audits per business).
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

## Windows validation

The combined session in [windows/M1_M3_TEST_PROCEDURE.md](windows/M1_M3_TEST_PROCEDURE.md) (fresh M1 install, upgrade
to M3 with migrations 0006 and 0007, then M1, M2 sales, Search Area, website audits with Edge, saved searches) was
run on a separate Windows test account with `LeadIntel-1.3.0-m3-TEST.zip` (from `bebe72f`, 497,076 bytes, SHA-256
`7095416ab10cfeb89167bc683adeddb2a170124a7d4210d36adfa79e934cfca6`) and **reported successful by the user**. Only that
summary confirmation was given: per-step results, business counts, `test-dataload.ps1` output and Setup output were
not recorded here. The real Friend Edition has not been upgraded.

## Open security findings

- R1 (low): the CRM login can create temporary tables / large objects and take advisory locks (PostgreSQL defaults;
  no business data). Pinned by a test.
- R2 (pre-existing): B0 can allow one commit after a lost lock; fencing token (B0.5) deferred, next free migration
  number after 0007.
- R3: the Setup step that creates the sales login ran in the Windows test session (reported successful; the profile
  dialog's write mode was not reported separately). It falls back safely to the lock if it fails.
- M3 crawler: all browser traffic through the egress proxy (SSRF / DNS rebinding); only GET/HEAD leave the browser;
  fresh browser per audit, context per site, no stored cookies, service workers blocked, downloads refused, pop-ups
  closed; no form is ever filled or submitted; the audited site's JavaScript runs only in that isolated browser,
  never in the LeadIntel page (screenshots are served as JPEG copies; the live site opens only in the external
  browser). The watchdog relies on a Playwright internal (driver process handle); a test fails if it moves.
- CRM write-engine exemption: unchanged since the M2 security review (`4edf41ac`); `crm_saved_search` was added to
  the CRM tables the restricted login may write; `tests/test_crm_security.py` passes.

## Known issues / limitations

- WQS is **provisional**: weights not yet checked against human ratings (target Spearman ≥ 0.7 on 60-100 rated real
  sites; tooling: `leadintel wqs-calibration export|report`). The app says so on every score.
- Visual design is always "not assessed" (no analysis provider); timing is a lab measurement on the user's PC, not
  Lighthouse (deferred); domain history covers .com/.net registration dates only (RDAP) plus Wayback captures.
- BOS and LPS are unchanged (listing data only); WQS is not yet an input to them or a list filter.
- The phone-width check resizes the loaded page (CSS layout); script-driven resize behaviour is not re-run.
- The map needs internet access (MapLibre library and OpenFreeMap tiles); the list works without it.
- Export is limited to 100,000 rows per file. CRM is single-user. Follow-up periods use the database server's clock.
- Closing the app window does not stop the server; "Stop LeadIntel" does.

## Outstanding approvals (high-impact)

- Push of the local `phase-1` commits (GitHub is at `b20f99a`).
- Any further ZIP (TEST or release). The M3 TEST ZIP was built with approval and tested on a separate account.
  (No M2 TEST ZIP exists: the one built from `03080b3f` was deleted before the security fix and never distributed.)
- Installing any M2/M3 build on the real Friend Edition (applies migrations 0006 and 0007 to the real database).
- Final distributable release ZIP rebuild.
- Licensing review before distribution beyond friends (PostGIS GPL, app-local VC++ runtime; axe-core MPL-2.0 is
  vendored unmodified with its notices; Playwright Apache-2.0 bundles Node.js with notices).

## M3 - search area and website intelligence

Plan and decisions: [research/M3_PREP.md](research/M3_PREP.md). WQS definition: [SCORING.md §5](SCORING.md).

| Step | What | State |
|---|---|---|
| 1 | Search Area and center search | **done** (`9ee6e0a3`, `23ae3815`, `0dd3c42a`, `e2c17d2c`); Windows session reported successful |
| 2 | Browser egress proxy | **done** (`b70e8846`) |
| 3 | Crawl runner | **done** (`ac6d1f71`; hardening in `8213a93`) |
| 4 | Website checks: 26 criteria over 7 dimensions, axe-core 4.14.0, browser timing, domain history, 33 fixture sites, watchdog | **done** (`f9e9dca3`, `8213a93`) |
| 5 | Migration 0007 and storage; audit service (lock, one at a time, interrupted/failed handling, retention); `crawl` command; API | **done** (`c06b876`; `crawl` command `47f486f`) |
| 6 | WQS wqs-1.0 with confidence and explanations (provisional) | **done** (`c06b876`); BOS/LPS inputs and a WQS filter not started |
| 7 | Peer benchmark (≥ 8 peers, county → target region) | **done** (`c06b876`); Census revenue prior not started |
| 8 | Evidence viewer (report dialog, screenshots, history) | **done** (`ba67b67`); `mailto:` not started |
| 9 | Saved searches (server `c06b876`, interface `ba67b67`); calibration tooling done; human labels not collected | done except labels |

Decisions recorded 2026-11-06 (still in force):
- 0007 is used by M3 (audits and saved searches); B0.5 is deferred to the next free number.
- Census key approved (free, kept out of source control); SAM.gov not approved.
- Lighthouse deferred (no bundled Node.js); browser timing is labelled as a lab measurement.
- Email through `mailto:` only; no Gmail OAuth.

Migration 0007 (`backend/migrations/versions/0007_website_audits.py`, down-revision 0006): `website_audit`,
`website_audit_page`, `website_audit_artifact`, `website_audit_finding`, `website_audit_recommendation`,
`website_benchmark`, `crm_saved_search`; indexes on audit location/time and on the child tables' `audit_id`. WQS
also goes into the existing `score` table (kind `wqs`, stage provisional) with `score_factor` rows per dimension.
Screenshots live in `<data>/crawl/` (path, sha256 and size in the database). Tested up/down on `leadintel_test` only.

## Next exact action

See [HANDOFF.md](HANDOFF.md) "Next steps": push decision, real Friend Edition upgrade decision, WQS calibration with
human labels, then versioned website inputs to BOS/LPS before M4.
