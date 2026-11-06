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
| Write safety | CRM writes use their own engine, exempt from the maintenance-lock gate so sales work continues during Load Data; it refuses any write outside `crm_*` tables (including writes inside CTEs) and any structure change, and refuses all writes while `db upgrade` runs | done |
| API | `/api/crm/*` (token and same-origin rules apply to every write); sales filters on list, counts, map and export (`sales`, `claim`, `follow_up`, `list_id`, sort by follow-up); CRM columns in the CSV export | done |
| Interface | Profile dialog, Sales section in the business view (claim, status, next action, follow-up date, activity log and timeline, lists), sales filters and list tags, Sales overview (pipeline, follow-ups, recent activity, lists and campaigns) | done |
| Windows | M2 TEST ZIP and checklist | not built yet - installing it runs migration 0006 on the real Friend Edition database, which needs explicit approval |

Decision to review: the CRM write engine is exempt from the B0 commit gate (narrowly, as above). Without the
exemption, claims and notes would be blocked for the whole of a Load Data run.

## Test results (latest)

- Full suite on `leadintel_test` (synthetic data, migrated to 0006): see the latest commit message for counts.
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
- B0.5 (fencing token) migration: deferred, will take the next number (0007) if still needed.
- Licensing review before distribution beyond friends (PostGIS GPL, app-local VC++ runtime).

## Next exact action

Commit M2 locally; build an M2 TEST ZIP (not installed without approval); continue parallel research
(website-viewing isolation, revenue data, directory sources, OAuth) and M3 preparation.
