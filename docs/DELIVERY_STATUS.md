# LeadIntel delivery status

Engineering progress tracker (kept short; the plan lives in [PLAN.md](PLAN.md) and [ROADMAP.md](ROADMAP.md)).
Updated: 2026-11-06.

## Current milestone: M1 - salesperson interface

| Stage | What | State | Commit |
|---|---|---|---|
| S1 | Local web-app hardening: Host check, per-launch token, same-origin rule, CSP with nonces, safe links | done | `2b419529` |
| S2a | Shared `ProspectFilter`; paginated list, summary and map-point endpoints | done | `bf181e6c` |
| S2b | Verified on PostgreSQL (`leadintel_test`, synthetic data); list query rewrite (deep pages 1.25 s -> 0.18 s at 100k rows); `limit=0` compatibility | done | `8d1b6fba` |
| S3 | CSV export endpoint and CLI export on the same filter | done | `8d1b6fba` |
| S4-S7 | Interface redesign: filter panel, paging, honest counts, map caption, salesperson detail view with clickable/copyable contacts, Advanced section, light/dark/system theme, keyboard use | done, Windows check pending | see git log |
| S8 | Edge app-mode window (maximized), full-screen button and F11, `-Fullscreen` test switch, default-browser fallback | done, Windows check pending | see git log |
| S9 | M1 TEST ZIP and one consolidated Windows acceptance checklist | in progress | - |

## Test results (latest)

- Offline suite: all pass (database tests skipped).
- Full suite on `leadintel_test`: all pass (see the latest commit message for counts).
- Static script checks (`packaging/windows/check_scripts.py`): all pass.
- Headless interface walkthrough (synthetic data, map blocked): filters, paging, counts, export rows = filtered total,
  hostile names and URLs stay inert, no CSP violations, no horizontal overflow at 1440 and 1366 px.
- Performance at 100k synthetic locations (EXPLAIN ANALYZE): list page 1 79 ms, page 400 182 ms, count 124 ms,
  summary 166 ms, map points 180 ms (82k points), name search 50 ms. No index or migration needed.

## Windows checks still required (one session, see the M1 checklist)

- MapLibre map under the new Content Security Policy (blocked in the sandbox).
- Edge app window: startup state, F11 / Esc / full-screen button, minimize / maximize / close, second launch.
- `-Fullscreen` (`--start-fullscreen`): whether F11 and Esc restore normal window controls. Not adopted as the
  default until this is confirmed; maximized is not treated as equivalent to full screen.
- Default-browser fallback when Edge is unavailable.
- Display scaling 100 / 125 / 150 %.
- `test-dataload.ps1` (simulated load outcomes, refresh rules, login fallback, Edge command line).
- Upgrade of an existing Friend Edition installation keeps its data.

## Known issues / limitations

- WQS does not exist yet (M3); BOS and LPS are provisional (listing data only).
- The map needs internet access (MapLibre library and OpenFreeMap tiles); the list works without it.
- Export is limited to 100,000 rows per file (reported in headers, CLI output and before exporting).
- Closing the app window does not stop the server; Ctrl+C in the launcher window or "Stop LeadIntel" does.

## Outstanding approvals (high-impact)

- Push of the local `phase-1` commits (GitHub is at `b20f99a`).
- Final distributable release ZIP rebuild (only TEST ZIPs are built).
- Reserved migration 0006 (B0.5) numbering - not needed by M1.
- Licensing review before distribution beyond friends (PostGIS GPL, app-local VC++ runtime).

## Next exact action

Build the M1 TEST ZIP and hand over the consolidated Windows checklist; then start M2 (local CRM) design and
implementation while the Windows session is pending.
