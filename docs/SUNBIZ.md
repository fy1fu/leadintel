# Sunbiz bulk data: verified requirements

Verified on **2026-11-02** by three methods:
- Reading the official Data Downloads pages on `dos.fl.gov` (Data Downloads, Daily Data, Quarterly Data, Data Usage Guide).
- Logging in to the live server with the public credentials published on those pages.
- Parsing real files.

Evidence levels used below:
- **Official**: stated on the official page.
- **Server**: observed on the live server.
- **Data**: validated against real records.
- **Unverified**: not yet confirmed.

> Credentials are never committed or logged. The public username and password appear on the official Data Downloads page. LeadIntel reads them only from `SUNBIZ_SFTP_USER` / `SUNBIZ_SFTP_PASSWORD`.

## 1. Host, port and protocol

| Item | Finding | Evidence |
|---|---|---|
| Host | `sftp.floridados.gov`. The official pages list it as `Host: https://sftp.floridados.gov`. | Official |
| Server software | VanDyke **VShell** (`<VanDyke-VShell>` error documents, `vandyke-auth-token` cookie) | Server |
| HTTPS (port 443) | Works. HTTP Basic auth (`WWW-Authenticate: Basic realm="VShell"`), **WebDAV** (`DAV: 1`; `PROPFIND` lists directories) and `GET` with **Range** support (`206 Partial Content`). Fronted by Cloudflare; no bot challenge was presented to authenticated API requests. | Server |
| SFTP (port 22) | The official page says "a secure FTP client" can be used. From this sandbox the proxy tunnel opened, but no SSH banner arrived and the connection was reset. **Unverified**: test on your network (§6). LeadIntel treats port 22 as a fallback only. | Official / Unverified |
| Login root | After login you are in `/Public/` (contains `doc/` and `WELCOME.TXT`) | Official + Server |

**Decision:** LeadIntel downloads over **HTTPS/WebDAV** (service `sunbiz_https`, priority 1) and falls back to SFTP (`sunbiz_sftp`, priority 2). HTTPS needs only ordinary web egress.

## 2. Authentication procedure

1. Get the public username and password from the official [Data Downloads page](https://dos.fl.gov/sunbiz/other-services/data-downloads/). They are the same for the browser portal and SFTP.
2. Put them in environment variables `SUNBIZ_SFTP_USER` and `SUNBIZ_SFTP_PASSWORD` (or a local `.env` that is git-ignored).
3. HTTPS sends them only as an HTTP Basic `Authorization` header to `sftp.floridados.gov`:
   - They are never forwarded on a redirect to another host.
   - They are never written to results, logs, the database or reports. Tests assert this.
   - Wrong credentials are reported as `authentication_failed`.
4. An unauthenticated request returns `401` with a VShell body. Diagnostics use this to prove the portal is up without credentials.

## 3. Directory layout (actual, case-sensitive)

The official pages describe paths informally (e.g. "doc > quarterly > cor"). The real paths differ in **case** and, for fictitious-name events, in **folder**:

| Data | Official description | Actual path | Filename |
|---|---|---|---|
| Daily corporate filings | doc > cor | `/Public/doc/cor/` | `yyyymmddc.txt` |
| Daily corporate events | doc > cor > events | `/Public/doc/cor/Events/` | `yyyymmddce.txt` |
| Daily fictitious-name filings | doc > fic | `/Public/doc/fic/` | `yyyymmddf.txt` |
| Daily fictitious-name events | doc > fic | **`/Public/doc/fic/Events/`** | `yyyymmddfe.txt` |
| Quarterly corporate | doc > quarterly > cor | `/Public/doc/Quarterly/Cor/` | `cordata.zip` (10 members), `corevent.zip` |
| Quarterly fictitious name | doc > quarterly > fic | `/Public/doc/Quarterly/Fic/` | `ficdata.zip`, `ficevt.zip` |

Other observations:
- Folders also contain `README.TXT`, `99-README.TXT`, `WELCOME.TXT`, `Prior to 2011/` and `Filings/`. These must be ignored by the importer.
- Layout READMEs with COBOL copybooks are at `/Public/doc/fic/Events/Ficreadme.txt` (fictitious-name filings + events) and `/Public/doc/cor/Events/COREVENTREADME.txt` (corporate events).
- **No copybook for the main corporate file exists on the server.** Its official definition is at `dos.sunbiz.org/data-definitions/cor.html`, which is unreachable from the sandbox.

Snapshot on 2026-11-02:
- Daily corporate files run from 2022-01-03 to 2026-11-01 (1,399 files).
- Daily fictitious-name files run from 2022-01-02 to 2026-11-01 (1,716 files).
- The quarterly files are dated October 2026: `cordata.zip` 1.79 GB, `corevent.zip` 195 MB, `ficdata.zip` 77.8 MB, `ficevt.zip` 53.2 MB. All four have a valid ZIP end-of-central-directory record.

## 4. File formats

General (Official + Data):
- **Fixed-width** text; each record is one line.
- Line terminator is **LF** (`0x0A`). Every record had exactly the documented byte length.
- Field positions are **byte** positions. Text is UTF-8 in practice (e.g. `0xC3 0x81` = `Á`), so slice bytes first and decode each field. 9 of 6,352 corporate records contained non-ASCII bytes.
- Dates are **`MMDDYYYY`**. Blank or all-zero means no date.
- The corporate record ends in 4 bytes of filler containing **NUL** bytes, which must be stripped.

| File | Record length | Layout source | Validation |
|---|---|---|---|
| Corporate filings | 1,440 | Official page (partial excerpts) + reconstructed layout | 6,352 records from 3 daily files. All dates, years and flags parse. Officers start at field 37, as the official page states. 9 officer-state values are malformed in the source data (e.g. `F `, `..`), not misaligned. |
| Corporate events | 662 | Copybook on server (`COREVENTREADME.txt`) | 25 fields sum to 662. 3,242 records: all numeric and date fields valid. |
| Fictitious-name filings | 2,098 | Copybook on server (`Ficreadme.txt`) | 17 + 10 × 10 owner fields sum to 2,098. Owners start at field 18, as the official page states. 1,018 records: **zero** format failures. |
| Fictitious-name events | 762 | Copybook on server | 37 fields sum to 762. 54 records: all numeric and date fields valid. |

#### Corporate filings (`yyyymmddc.txt`, quarterly `cordata.zip`), 1,440 bytes

| # | Field | Start | Length | Source |
|---|---|---|---|---|
| 1 | `doc` | 1 | 12 | official excerpt + data |
| 2 | `name` | 13 | 192 | official excerpt + data |
| 3 | `status` | 205 | 1 | official excerpt + data |
| 4 | `filing_type` | 206 | 15 | inferred, validated on data |
| 5 | `addr1` | 221 | 42 | official excerpt + data |
| 6 | `addr2` | 263 | 42 | inferred, validated on data |
| 7 | `city` | 305 | 28 | inferred, validated on data |
| 8 | `state` | 333 | 2 | inferred, validated on data |
| 9 | `zip` | 335 | 10 | inferred, validated on data |
| 10 | `country` | 345 | 2 | inferred, validated on data |
| 11 | `mail_addr1` | 347 | 42 | inferred, validated on data |
| 12 | `mail_addr2` | 389 | 42 | inferred, validated on data |
| 13 | `mail_city` | 431 | 28 | inferred, validated on data |
| 14 | `mail_state` | 459 | 2 | inferred, validated on data |
| 15 | `mail_zip` | 461 | 10 | inferred, validated on data |
| 16 | `mail_country` | 471 | 2 | inferred, validated on data |
| 17 | `file_date` | 473 | 8 | official excerpt + data |
| 18 | `fei` | 481 | 14 | inferred, validated on data |
| 19 | `more_than_6_officers` | 495 | 1 | inferred, validated on data |
| 20 | `last_txn_date` | 496 | 8 | inferred, validated on data |
| 21 | `state_country` | 504 | 2 | inferred, validated on data |
| 22 | `rpt_year1` | 506 | 4 | inferred, validated on data |
| 23 | `house_flag1` | 510 | 1 | inferred, validated on data |
| 24 | `rpt_date1` | 511 | 8 | inferred, validated on data |
| 25 | `rpt_year2` | 519 | 4 | inferred, validated on data |
| 26 | `house_flag2` | 523 | 1 | inferred, validated on data |
| 27 | `rpt_date2` | 524 | 8 | inferred, validated on data |
| 28 | `rpt_year3` | 532 | 4 | inferred, validated on data |
| 29 | `house_flag3` | 536 | 1 | inferred, validated on data |
| 30 | `rpt_date3` | 537 | 8 | inferred, validated on data |
| 31 | `ra_name` | 545 | 42 | inferred, validated on data |
| 32 | `ra_name_type` | 587 | 1 | inferred, validated on data |
| 33 | `ra_addr` | 588 | 42 | inferred, validated on data |
| 34 | `ra_city` | 630 | 28 | inferred, validated on data |
| 35 | `ra_state` | 658 | 2 | inferred, validated on data |
| 36 | `ra_zip` | 660 | 9 | inferred, validated on data |
| 37 | `offN_title` | 669 | 4 | inferred, validated on data |
| 38 | `offN_type` | 673 | 1 | inferred, validated on data |
| 39 | `offN_name` | 674 | 42 | inferred, validated on data |
| 40 | `offN_addr` | 716 | 42 | inferred, validated on data |
| 41 | `offN_city` | 758 | 28 | inferred, validated on data |
| 42 | `offN_state` | 786 | 2 | inferred, validated on data |
| 43 | `offN_zip` | 788 | 9 | inferred, validated on data |
| 44 | `filler` | 1437 | 4 | inferred, validated on data |

Officer block `offN_*` (128 bytes) repeats 6 times, starting at byte 669 (N = 1..6, stride 128). The final 4 bytes are filler and contain NUL (`0x00`) bytes.

#### Fictitious-name filings (`yyyymmddf.txt`, quarterly `ficdata.zip`), 2,098 bytes

| # | Field | Start | Length | Source |
|---|---|---|---|---|
| 1 | `doc` | 1 | 12 | server copybook + data |
| 2 | `name` | 13 | 192 | server copybook + data |
| 3 | `county` | 205 | 12 | server copybook + data |
| 4 | `addr1` | 217 | 40 | server copybook + data |
| 5 | `addr2` | 257 | 40 | server copybook + data |
| 6 | `city` | 297 | 28 | server copybook + data |
| 7 | `state` | 325 | 2 | server copybook + data |
| 8 | `zip` | 327 | 10 | server copybook + data |
| 9 | `country` | 337 | 2 | server copybook + data |
| 10 | `file_date` | 339 | 8 | server copybook + data |
| 11 | `pages` | 347 | 5 | server copybook + data |
| 12 | `status` | 352 | 1 | server copybook + data |
| 13 | `cancel_date` | 353 | 8 | server copybook + data |
| 14 | `expire_date` | 361 | 8 | server copybook + data |
| 15 | `owner_count` | 369 | 5 | server copybook + data |
| 16 | `fei` | 374 | 14 | server copybook + data |
| 17 | `gt10_owners` | 388 | 1 | server copybook + data |
| 18 | `ownN_doc` | 389 | 12 | server copybook + data |
| 19 | `ownN_name` | 401 | 55 | server copybook + data |
| 20 | `ownN_fmt` | 456 | 1 | server copybook + data |
| 21 | `ownN_addr` | 457 | 40 | server copybook + data |
| 22 | `ownN_city` | 497 | 28 | server copybook + data |
| 23 | `ownN_state` | 525 | 2 | server copybook + data |
| 24 | `ownN_zip` | 527 | 10 | server copybook + data |
| 25 | `ownN_country` | 537 | 2 | server copybook + data |
| 26 | `ownN_fei` | 539 | 9 | server copybook + data |
| 27 | `ownN_charter` | 548 | 12 | server copybook + data |

Owner block `ownN_*` (171 bytes) repeats 10 times, starting at byte 389 (stride 171). For company owners (`own_fmt` = `C`), `own_charter` holds the owner's Sunbiz document number.


Corporate and fictitious-name **event** layouts: see the copybooks referenced above. They will be encoded in the parser with the same validation.

### Observed data characteristics that affect the design

- **Principal-address state is blank in 100% of daily corporate records** (6,352/6,352), while mailing state is populated. Location must therefore come from the principal address, city and ZIP via geocoding, or from the mailing address as a fallback. This is not yet checked in quarterly files.
- **Daily corporate files contain new filings, not changes.** In the sampled days, ~87% of records had a formation date within 7 days, a few were 8–60 days old, and under 1% were older (late-processed filings). No document number appeared in two daily files, and all records were status `A`. The official page states that annual reports and address changes are not included; changes arrive as **events**.
- **Fictitious names link directly to entities.** All 706 company owners (`own_fmt = C`) carried a Sunbiz document number in `own_charter` (`L…` LLC, `P…` corporation, `M…`, `F…`, `N…`). DBA ↔ entity linking therefore needs no fuzzy matching.
- `county = MULTIPLE` on 39% of fictitious-name filings, so county is not a reliable location field; use the address.
- Corporate filing types seen: `FLAL` (Florida LLC), `DOMP`, `FORL`, `DOMNP`, `FORP`, `FORNP`, `DOMLP`. Corporate event codes seen include `CORAPREIN` (reinstatement), `CORAPVOLDS` (voluntary dissolution), `CORLCAMND`/`CORAPAMND` (amendments) and `CORLCNC` (name change).

## 5. Incremental vs complete, missing dates, incomplete downloads

**Daily files are incremental:** filings entered into the database that day (Official). **Quarterly files are complete snapshots** of active records. The official pages disagree on wording: the Quarterly page says "all data on record", the Data Downloads page says "all active data". Treat them as active-only until checked.

**Missing dates.** The official page says no file is produced when nothing was entered (holidays, closures). The server shows more than that:

| Observation (May–Nov 2026) | Corporate | Fictitious name |
|---|---|---|
| Weekend files present | 42 | 48 |
| Weekday dates with no file | 2026-05-25, 07-02, 07-03, 07-06, 09-07 | 07-02, 07-03, 07-06, 09-07 |

25 May, 3 Jul and 7 Sep are state holidays. **2 Jul and 6 Jul are not**, so a missing date cannot be assumed to mean "no filings". Corporate **event** files also lag: the latest was 2026-10-30 while filings reached 2026-11-01. Files can also be regenerated: `20261026c.txt` was last modified the next afternoon rather than at the usual ~02:30.

**Required handling (to implement in Phase 1, `sunbiz` adapter):**

1. **Date ledger** per (file type, date) with these states:
   - `verified`: downloaded and validated.
   - `not_yet_published`: no later file exists yet.
   - `absent_holiday`: no file, and the date is a published state holiday.
   - `absent_unexplained`: no file, but later files exist.
   - `failed`.

   Discovery coverage for a registration-date filter is **partial** whenever its range touches `absent_unexplained` or `failed` dates. It is never reported as "no new businesses".
2. **Re-check unexplained gaps** daily for 14 days (files can appear late), then keep them flagged.
3. **Reconcile with the quarterly snapshot.** Entities in the snapshot with filing dates inside a gap are back-filled and marked `source = quarterly`. Entities that formed and dissolved inside a gap cannot be recovered; this is documented as a known blind spot.
4. **Download integrity:**
   - Compare received bytes with the WebDAV `getcontentlength` / `Content-Length`.
   - Require `size % (record_length + 1) == 0` and every line exactly `record_length` bytes.
   - Download to a `.part` file with Range resume, install atomically, and store the SHA-256 hash plus the server `Last-Modified`.
5. **Change detection:** re-list recent folders. A file whose size or `Last-Modified` changed is re-downloaded and re-imported idempotently (upsert by document number + event sequence).
6. **ZIPs:** check the end-of-central-directory record and every member's CRC before import. A truncated or failed ZIP never replaces the previous snapshot.
7. Connectivity failures put the source's coverage into `failed` / `not_run`. **They never produce empty discovery results.**

## 6. What remains unverified

| Item | Why | How to verify |
|---|---|---|
| Exact official positions for corporate fields not shown in the excerpts (filing type through officers) | `dos.sunbiz.org` is unreachable from the sandbox | Open https://dos.sunbiz.org/data-definitions/cor.html and compare with the table above. Every field already parses consistently on 6,352 real records. |
| Port 22 SFTP access | Sandbox proxy resets the connection | Windows procedure below |
| Quarterly file contents (record layouts inside the ZIPs, active-only vs all records, principal-state population) | Not downloaded (1.8 GB) | Phase 1 import on your machine |
| Whether VShell rate-limits or blocks bulk HTTPS downloads | Only small requests made | First full quarterly download |
| Official meaning of all filing-type and event codes | Not published on the reachable pages | Collect distinct codes from the quarterly files; map them to the official glossary when available |

## 7. Windows testing procedure

These commands work in PowerShell on Windows 10/11, which include `curl.exe` and the OpenSSH client.

```powershell
# 1. Credentials for this session only. Copy them from the official Data Downloads page.
$env:SUNBIZ_SFTP_USER = "<username from the page>"
$env:SUNBIZ_SFTP_PASSWORD = Read-Host "Sunbiz public password"   # avoids putting it in history
```

```powershell
# 2. HTTPS portal, unauthenticated: expect HTTP 401 and a VShell body
curl.exe -s -o NUL -w "%{http_code}`n" https://sftp.floridados.gov/
```

```powershell
# 3. HTTPS listing; curl prompts for the password so it never appears on the command line
curl.exe -u $env:SUNBIZ_SFTP_USER -X PROPFIND -H "Depth: 1" https://sftp.floridados.gov/Public/doc/
```

```powershell
# 4. SFTP on port 22 (fallback path)
Test-NetConnection sftp.floridados.gov -Port 22
sftp.exe "$($env:SUNBIZ_SFTP_USER)@sftp.floridados.gov"    # then: ls /Public/doc ; bye
```

```powershell
# 5. LeadIntel diagnostics (from the repository root)
cd backend
py -3.13 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\leadintel net-check --only sunbiz_https,sunbiz_sftp
```

Expected result:
- `sunbiz_https` is **operational**.
- `sunbiz_sftp` is operational if port 22 is open on your network. Otherwise it is `reachable_not_operational` or unreachable, which is acceptable because HTTPS is primary.
