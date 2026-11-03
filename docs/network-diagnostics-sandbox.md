# LeadIntel network diagnostics

Generated 2026-11-02T23:13:51.783772+00:00 (run `20261102T231330Z-a8812c`). Egress proxy in use: yes.

## Capabilities

| Capability | Required | Status | Active service |
|---|---|---|---|
| overture_places | yes | operational | overture_s3 |
| sunbiz_bulk | yes | operational | sunbiz_https |
| census_geocoder | yes | operational | census_geocoder |
| census_files | yes | operational | census_www2 |
| osm_extract | yes | **unavailable** | — |
| map_tiles | yes | operational | openfreemap |
| dbpr_licences | yes | operational | dbpr |
| duval_btr | no | operational | duval_taxcollector |
| npi_registry | no | operational | npi_api |
| commoncrawl_data | no | operational | commoncrawl_data |
| commoncrawl_index | no | unavailable | — |
| wayback | no | unavailable | — |
| rdap | no | unavailable | — |
| nominatim | no | operational | nominatim |
| osm_overpass | no | unavailable | — |
| foursquare_os | no | disabled | — |
| local_ai | no | disabled | — |

## Services

| Service | Host | Purpose | Required | Status | Network allowed | Last successful download | Latest error |
|---|---|---|---|---|---|---|---|
| overture_s3 | `overturemaps-us-west-2.s3.us-west-2.amazonaws.com:443` | Overture releases on AWS Open Data (S3 REST over HTTPS) | yes | operational | yes | 2026-11-02T23:13:31.075323+00:00 | — |
| overture_azure_blob | `overturemapswestus2.blob.core.windows.net:443` | Overture releases on Azure Open Datasets (Blob endpoint) - fallback mirror | yes | operational | yes | 2026-11-02T23:13:31.136879+00:00 | — |
| overture_azure_dfs | `overturemapswestus2.dfs.core.windows.net:443` | Overture releases on Azure Data Lake (DFS endpoint) - second fallback | yes | operational | yes | 2026-11-02T23:13:31.228108+00:00 | — |
| sunbiz_https | `sftp.floridados.gov:443` | Sunbiz bulk files over HTTPS/WebDAV (Data Access Portal): /Public/doc/{cor,fic,Quarterly} | yes | operational | yes | — | — |
| sunbiz_sftp | `sftp.floridados.gov:22` | Sunbiz bulk files over SFTP (fallback; needs outbound port 22) | yes | reachable_not_operational | yes | — | sftp_list: ssh/sftp_error; ssh_banner: ssh/ssh_banner_timeout |
| census_geocoder | `geocoding.geo.census.gov:443` | US Census Geocoder (single + batch, no key) | yes | operational | yes | — | — |
| census_www2 | `www2.census.gov:443` | TIGER/Line boundary shapefiles and County Business Patterns bulk files | yes | operational | yes | 2026-11-02T23:13:30.544898+00:00 | — |
| geofabrik_main | `download.geofabrik.de:443` | Geofabrik daily OSM extracts (.osm.pbf) and region index | yes | reachable_not_operational | yes | — | md5_file: tls/connection_reset; pbf_range: tls/connection_reset |
| geofabrik_ext2 | `download-ext2.geofabrik.de:443` | Geofabrik secondary download host (fallback mirror; same paths assumed - verified only by probe) | yes | unreachable | unknown | — | md5_file: proxy/proxy_upstream_error |
| overpass_main | `overpass-api.de:443` | Overpass API main instance (fair use; small-area fallback only) | no | reachable_not_operational | yes | — | status: tls/connection_reset |
| overpass_private_coffee | `overpass.private.coffee:443` | Overpass community instance (fallback) | no | reachable_not_operational | yes | — | status: http/http_timeout |
| overpass_kumi | `overpass.kumi.systems:443` | Overpass community instance (fallback) | no | reachable_not_operational | yes | — | status: http/http_timeout |
| openfreemap | `tiles.openfreemap.org:443` | OpenFreeMap vector tiles + style (donation-funded, no key). Self-hosted PMTiles is the offline fallback. | yes | operational | yes | 2026-11-02T23:13:32.941675+00:00 | — |
| dbpr | `www2.myfloridalicense.com:443` | DBPR public-records licensee CSV extracts (weekly) | yes | operational | yes | 2026-11-02T23:13:32.366026+00:00 | — |
| duval_taxcollector | `taxcollector.jacksonville.gov:443` | Duval Tax Collector data-request page (BTR report links) | no | operational | yes | — | — |
| duval_county_taxes | `duval.county-taxes.com:443` | Duval BTR report portal (may present a bot challenge; never bypassed) | no | reachable_not_operational | yes | — | portal_home: http/bot_challenge |
| npi_api | `npiregistry.cms.hhs.gov:443` | NPPES NPI Registry API (no key) | no | operational | yes | — | — |
| npi_bulk | `download.cms.gov:443` | NPPES monthly full-file downloads | no | reachable_not_operational | yes | — | files_page: download/content_mismatch |
| commoncrawl_data | `data.commoncrawl.org:443` | Common Crawl data files | no | operational | yes | 2026-11-02T23:13:33.283444+00:00 | — |
| commoncrawl_cloudfront | `ds5q9oxwqwsfj.cloudfront.net:443` | CloudFront distribution serving the same Common Crawl data (fallback alias of data.commoncrawl.org) | no | operational | yes | 2026-11-02T23:13:33.419798+00:00 | — |
| commoncrawl_index | `index.commoncrawl.org:443` | Common Crawl CDX URL index API | no | reachable_not_operational | yes | — | collinfo: tls/connection_reset |
| wayback_cdx | `web.archive.org:443` | Wayback Machine CDX API | no | reachable_not_operational | yes | — | cdx_query: tls/connection_reset |
| iana_rdap_bootstrap | `data.iana.org:443` | IANA RDAP bootstrap (maps TLD -> registry RDAP server) | no | blocked_by_network_policy | no | — | dns_bootstrap: proxy/proxy_denied |
| rdap_verisign | `rdap.verisign.com:443` | RDAP server for .com/.net domains | no | blocked_by_network_policy | no | — | domain_lookup: proxy/proxy_denied |
| rdap_org | `rdap.org:443` | RDAP redirector (redirects to registry servers; only useful if those hosts are reachable too) | no | reachable_not_operational | yes | — | lookup_via_redirect: proxy/proxy_denied |
| nominatim | `nominatim.openstreetmap.org:443` | Place-name search for the area picker (max 1 req/s, no bulk) | no | operational | yes | — | — |
| huggingface | `huggingface.co:443` | Foursquare OS Places (gated; needs a free account + access approval) | no | disabled | unknown | — | — |
| ollama_local | `localhost:11434` | Local Ollama server (free local AI). Not an internet dependency. | no | disabled | unknown | — | — |

## Paid providers

| Provider | Hosts | Status |
|---|---|---|
| google_places | places.googleapis.com, maps.googleapis.com | blocked_by_policy |
| google_maps_js | maps.googleapis.com, maps.gstatic.com | blocked_by_policy |
| google_pagespeed | pagespeedonline.googleapis.com | blocked_by_policy |
| google_gemini | generativelanguage.googleapis.com | blocked_by_policy |
| anthropic | api.anthropic.com | blocked_by_policy |
| openai | api.openai.com | blocked_by_policy |
| brave_search | api.search.brave.com | blocked_by_policy |
| serper | google.serper.dev | blocked_by_policy |
| serpapi | serpapi.com | blocked_by_policy |
| mapbox | api.mapbox.com | blocked_by_policy |
| maptiler | api.maptiler.com | blocked_by_policy |
| stadia | tiles.stadiamaps.com | blocked_by_policy |
| foursquare_api | places-api.foursquare.com, api.foursquare.com | blocked_by_policy |
| yelp_fusion | api.yelp.com | blocked_by_policy |

Paid requests made: **0**

## Hosts not required

| Host | Reason |
|---|---|
| `dos.fl.gov` | Documentation only (Data Downloads pages). Bulk data comes from sftp.floridados.gov over HTTPS/WebDAV. Never scraped. |
| `search.sunbiz.org` | Per-entity web search; bulk SFTP files replace it. Do not scrape. |
| `wiki.openstreetmap.org` | Documentation only; not used at runtime. |
| `tigerweb.geo.census.gov` | TIGERweb REST is optional; boundaries are loaded from TIGER/Line files on www2.census.gov. |
| `api.census.gov` | Now redirects to a 'missing key' page; County Business Patterns is taken from bulk files on www2.census.gov instead. |
| `extensions.duckdb.org` | DuckDB extensions not needed; Overture is read with PyArrow over HTTPS range requests. |
| `build.protomaps.com` | Only for producing an optional self-hosted PMTiles extract; not a runtime dependency. |
| `www.fdacs.gov` | No confirmed bulk data endpoint yet; add when an FDACS adapter is built. |
