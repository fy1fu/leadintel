# Network requirements, diagnostics and egress safety

This document covers four things:
- The external hosts LeadIntel uses, defined in [`config/network.yaml`](../config/network.yaml).
- How their health is diagnosed.
- How business-website crawling is kept separate and protected.
- What the build sandbox could and could not reach on 2026-11-02.

The full machine-generated report from that run is in [`network-diagnostics-sandbox.md`](network-diagnostics-sandbox.md).

## 1. Two separate egress paths

| Path | Used for | Rule |
|---|---|---|
| **Infrastructure client** (`app/net/http.py`) | Fixed data sources, tiles, geocoders | Only hosts registered **and enabled** in `config/network.yaml`. Every redirect hop is re-checked. Paid hosts are rejected by the cost guard before any connection is made. |
| **Safe egress client** (`app/net/safe_egress.py`) | Discovered business websites (any public site) | Any public HTTP(S) host on ports 80/443. Private, loopback, link-local, CGNAT, multicast, reserved and metadata addresses are refused, along with internal hostnames, credentials in URLs, unsafe redirects and paid hosts. |

Business websites are deliberately **not** added to any allowlist. Crawling is governed by the safe-egress policy, plus robots.txt and rate limits (crawler module, Phase 2).

## 2. Host inventory

The "Sandbox result" column comes from a real request made on 2026-11-02. "Allowed" means the egress proxy established the tunnel. "Operational" means a real request completed and its content was verified.

### Required capabilities

| Capability | Host(s), in failover order | Endpoint(s) probed | Sandbox result |
|---|---|---|---|
| Overture Places | `overturemaps-us-west-2.s3.us-west-2.amazonaws.com` → `overturemapswestus2.blob.core.windows.net` → `overturemapswestus2.dfs.core.windows.net` | Release listing + 4 KB range of a real places parquet (`PAR1` magic). Live test also read real places from each mirror individually and failed over when S3 was broken. | **All three operational** |
| Sunbiz bulk | `sftp.floridados.gov:443` (HTTPS/WebDAV, primary) → `sftp.floridados.gov:22` (SFTP, fallback) | Unauthenticated `GET /` must return the VShell 401 challenge. Authenticated `PROPFIND /Public/doc/` uses `SUNBIZ_SFTP_USER`/`SUNBIZ_SFTP_PASSWORD`; it is skipped when they are unset. | **HTTPS operational** (authenticated listing succeeded). Port 22: tunnel allowed, connection reset before the SSH banner; unverified. See [SUNBIZ.md](SUNBIZ.md). |
| Census geocoder | `geocoding.geo.census.gov` | Real geocode of 117 W Duval St | Operational |
| Census files | `www2.census.gov` | 1 KB range of `TIGER2025/STATE/tl_2025_us_state.zip` (`PK` magic) | Operational |
| OSM extracts | `download.geofabrik.de` → `download-ext2.geofabrik.de` | `florida-latest.osm.pbf.md5`; 2 KB of the PBF (`OSMHeader`) | Main: allowed, **reset during TLS handshake**. ext2: **proxy upstream 502** (the hostname may not exist; unverified). Capability unavailable here. |
| Map tiles | `tiles.openfreemap.org` | Style JSON + a real z0 vector tile | Operational |
| DBPR licences | `www2.myfloridalicense.com` | 1 KB range of `sto/file_download/extracts/cilb_certified.csv` | Operational |

### Optional capabilities

| Capability | Host(s) | Sandbox result |
|---|---|---|
| Duval BTR | `taxcollector.jacksonville.gov` → `duval.county-taxes.com` | Data-request page operational. The report portal returns a **Cloudflare bot challenge** (classified `bot_challenge`, never bypassed). Bulk reports may need a manual export or a public-records request. |
| NPI registry | `npiregistry.cms.hhs.gov` → `download.cms.gov` | API operational. The bulk page currently says *"down for maintenance until further notice"*; correctly **not** reported as operational. |
| Common Crawl data | `data.commoncrawl.org` → `ds5q9oxwqwsfj.cloudfront.net` | Both operational. The CloudFront host is the distribution behind `data.commoncrawl.org`, so it is a fallback alias, not a separate service. |
| Common Crawl index | `index.commoncrawl.org` | Allowed, reset during TLS |
| Wayback | `web.archive.org` | Allowed, reset during TLS |
| RDAP | `data.iana.org` → `rdap.verisign.com` → `rdap.org` | IANA and Verisign **refused by the proxy (403)**. `rdap.org` is reachable, but it only redirects to Verisign, so lookups fail. |
| Nominatim | `nominatim.openstreetmap.org` | Operational |
| Overpass | `overpass-api.de`, `overpass.private.coffee`, `overpass.kumi.systems` | Allowed. The main instance resets during TLS; the others time out. Fallback only. |
| Foursquare OS | `huggingface.co` | Disabled (opt-in); not contacted |
| Local AI | `localhost:11434` (Ollama) | Disabled (opt-in); not contacted |

### Missing from the current allowlist

| Host | Why it's needed |
|---|---|
| `data.iana.org` | RDAP bootstrap file, which maps each TLD to its registry's RDAP server |
| `rdap.verisign.com` | RDAP for `.com` / `.net` |
| Other registry RDAP servers (e.g. `rdap.publicinterestregistry.org` for `.org`) | Only if domain-age signals are wanted for those TLDs |

RDAP is optional; domain age is a weak signal, and Wayback/Common Crawl are alternatives.

### Not required (can be removed from the allowlist)

| Host | Reason |
|---|---|
| `dos.fl.gov`, `search.sunbiz.org` | Documentation and per-entity search pages only. Bulk data comes from `sftp.floridados.gov` over HTTPS. Never scraped. |
| `wiki.openstreetmap.org` | Documentation only |
| `tigerweb.geo.census.gov` | TIGER/Line files on `www2.census.gov` suffice |
| `api.census.gov` | Now redirects to a "missing key" page; County Business Patterns bulk files on `www2.census.gov` are used instead |
| `extensions.duckdb.org` | Overture is read with PyArrow over HTTPS range requests |
| `build.protomaps.com` | Only for building an optional self-hosted PMTiles file |
| `www.fdacs.gov` | No confirmed bulk endpoint yet |

### Paid providers (blocked; never contacted)

Google Places / Maps JS / PageSpeed / Gemini, Anthropic, OpenAI, Brave, Serper, SerpApi, Mapbox, MapTiler, Stadia, Foursquare Places API, and Yelp Fusion.
- Enabling one requires `LEADINTEL_ALLOW_PAID=true` **and** the provider key in `LEADINTEL_PAID_PROVIDERS`.
- The diagnostics report always shows the number of paid requests made. In this run it was **0**, also asserted by `tests/test_cost_guard.py` and the live test.

## 3. How diagnostics classify failures

Each probe walks the connection stage by stage (`app/net/probe.py`), so errors land in separate buckets:

| Stage | Failure kinds |
|---|---|
| `policy` | `paid_provider_blocked` (no network activity at all) |
| `dns` | `dns_resolution_failed`, `dns_timeout`. Behind an egress proxy a local DNS miss is recorded as *info*, because the proxy resolves names. |
| `proxy` | `proxy_denied` (403/407: allowlist), `proxy_upstream_error` (5xx), `proxy_unreachable`, timeouts |
| `tcp` | `connection_refused`, `tcp_timeout`, `network_unreachable` |
| `tls` | `tls_certificate_invalid`, `tls_handshake_failed`, `connection_reset`, `connection_closed_during_tls` |
| `http` | `http_status`, `authentication_failed`, `bot_challenge`, `http_timeout`, `redirect_to_unregistered_host`, `too_many_redirects`, `protocol_error` |
| `download` | `content_mismatch` (magic bytes / expected text), `short_download`, `download_interrupted` |
| `ssh` | `ssh_banner_timeout`, `not_ssh`, `sftp_auth_failed`, `sftp_error` |

Service status values:
- `operational`: every probe passed.
- `partially_verified`: every probe that ran passed, but some were skipped (for example, credentials not configured). Never shown as operational.
- `degraded`: some probes passed.
- `reachable_not_operational`: the network path was allowed but the service failed.
- `blocked_by_network_policy`: the proxy refused the connection.
- `dns_failure`, `unreachable_timeout`, `unreachable`: connection failures.
- `not_tested`: probes skipped, e.g. credentials missing.
- `disabled`: optional integration, never contacted.

A historical successful download does **not** make a service operational now. It is reported separately as *last successful download*.

Capabilities roll up their mirrors:
- `operational` or `operational_via_fallback`.
- `degraded` or `unavailable`.
- A **required** capability that is unavailable is flagged critical, and the CLI exits with status 2.

## 4. Fallbacks

- **Overture** (`app/sources/overture/reader.py`):
  - Mirrors are ordered by latest health, then priority.
  - Listing fails over between mirrors.
  - Each file is read through `FailoverRangeFile`, which continues the *same* file from the next mirror if one fails mid-read. All mirrors publish byte-identical files under the same relative path.
  - Only parquet footers and row groups that intersect the bounding box are fetched.
- **OpenStreetMap** (`app/sources/osm/extracts.py`):
  - Geofabrik extracts are preferred.
  - Downloads resume from `.part` files, are verified against the published MD5, are installed atomically, and are skipped entirely when the MD5 is unchanged.
  - Overpass is used only when no extract mirror is usable **and** the area is small (≤ 250 km² by default).
  - Otherwise the plan is `unavailable`, and OSM coverage must be recorded as *not run*, never as "no businesses found".

## 5. Safe egress details

**Blocked address classes:**
- Loopback, RFC 1918, ULA, link-local, CGNAT `100.64/10`, multicast, reserved, unspecified.
- Cloud metadata: `169.254.169.254`, `169.254.170.2`, `fd00:ec2::254`, `168.63.129.16`, `100.100.100.200`, `192.0.0.192`.
- IPv4 embedded in IPv6: IPv4-mapped, 6to4, Teredo and NAT64 encodings.
- Legacy numeric host forms (`2130706433`, `0x7f.1`, `017700000001`, `127.1`).

**Blocked names:** `localhost`, `*.local`, `*.internal`, `*.localhost`, `*.home.arpa`, `metadata.google.internal`, and single-label names.

**Direct mode (recommended deployment):**
- The hostname is resolved once inside the connection backend.
- **All** returned addresses must be public; mixed answers are blocked.
- The socket connects to that validated address (SNI and Host still use the name), so DNS rebinding cannot slip between check and use.

**Redirects:** each hop is re-validated. Scheme downgrade (https→http), loops, more than 5 hops, and non-80/443 ports are refused.

**Bodies:** capped at 5 MB.

**Proxy mode (e.g. this sandbox):** the proxy resolves DNS and the client only ever connects to the proxy. The hostname and any locally obtainable resolution are validated first.

*Residual risk:* a name the proxy resolves to an internal address is caught only if local DNS also returns it. The proxy's own network policy is the remaining control. Production deployments without a proxy get full pinning.

## 6. Running it

```bash
cd backend && .venv/bin/leadintel net-check            # probe everything enabled, print summary
.venv/bin/leadintel net-check --only sunbiz_https    # one service
.venv/bin/leadintel net-check --markdown ../docs/network-diagnostics-local.md
.venv/bin/uvicorn app.api.main:create_app --factory   # then open /diagnostics/network
LEADINTEL_LIVE_TESTS=1 .venv/bin/python -m pytest -m live   # opt-in live tests
```

## 7. Configuration and environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LEADINTEL_CONFIG_DIR` | `<repo>/config` | Location of `network.yaml` |
| `LEADINTEL_DATA_DIR` | `<repo>/data` (git-ignored) | Downloads, SQLite database |
| `LEADINTEL_DATABASE_URL` | SQLite in the data dir | e.g. `postgresql+psycopg://…` |
| `HTTPS_PROXY` / `NO_PROXY` | unset | Standard egress-proxy settings; honoured automatically |
| `LEADINTEL_IGNORE_PROXY` | `false` | Ignore `HTTPS_PROXY` for diagnostics |
| `LEADINTEL_CA_BUNDLE` (or `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) | system store | Extra CA bundle to trust in addition to the system store; verification is never disabled |
| `SUNBIZ_SFTP_USER` / `SUNBIZ_SFTP_PASSWORD` | unset | Public Sunbiz login from the official page. Never committed or logged. |
| `LEADINTEL_ENABLE_SERVICES` | empty | Comma-separated optional service keys to enable (e.g. `huggingface,ollama_local`) |
| `LEADINTEL_ALLOW_PAID` + `LEADINTEL_PAID_PROVIDERS` | `false` / empty | **Both** are required before any paid provider can be contacted |
| `LEADINTEL_LIVE_TESTS` | unset | `1` runs the opt-in live network tests |
