# Open verification items (before Phase 1)

Status as of 2026-11-02. Items are closed only by a real request or a real data check, never by assumption.

| # | Item | Current evidence | Blocking? | How to close |
|---|---|---|---|---|
| V1 | Official positions of the corporate fields not shown in the definition excerpts | Reconstructed layout parses 6,352 real records consistently; officer block starts at field 37 as officially stated | No (parser will validate every record) | Compare [SUNBIZ.md §4](SUNBIZ.md) with https://dos.sunbiz.org/data-definitions/cor.html |
| V2 | Sunbiz SFTP on port 22 | Sandbox: tunnel opened, connection reset before the SSH banner. HTTPS/WebDAV (primary) works. | No | Windows procedure in [SUNBIZ.md §7](SUNBIZ.md) |
| V3 | Sunbiz quarterly ZIP contents (member layouts, active-only vs all records, principal-state population) | ZIP directories intact; contents not downloaded | Partly: affects back-fill design | First quarterly import on your machine |
| V4 | Geofabrik extracts (required for OSM) | Sandbox: main host resets during TLS; `download-ext2` returns proxy 502 | Yes for OSM; Overture covers primary discovery | `leadintel net-check --only geofabrik_main,geofabrik_ext2` on Windows |
| V5 | Overpass instances | Reset or timeout from the sandbox | No (optional fallback) | Same, on Windows |
| V6 | Common Crawl index, Wayback, RDAP (`data.iana.org`, `rdap.verisign.com`) | Reset, or refused by the sandbox proxy | No (optional website-history signals) | Same, on Windows |
| V7 | Duval BTR report export | Tax Collector page works; report portal shows a Cloudflare bot challenge (never bypassed) | No | Manual export or public-records request; decide per county |
| V8 | NPI bulk files | Page reports "down for maintenance"; API works | No | Re-run diagnostics later |
| V9 | Sunbiz HTTPS rate limits for bulk downloads | Only small requests made so far | No | Observe during the first quarterly download (resumable) |

**Pending input:** the updated LeadIntel requirements (fictitious-name prioritization, established businesses without independent websites, industry targeting, WQS/BOS/LPS, competitor comparisons, first-campaign lists, CRM, revenue tracking). When they arrive, a consolidated roadmap will classify every item as *already implemented*, *planned but not implemented*, *newly required* or *optional future*, before Phase 1 starts.
