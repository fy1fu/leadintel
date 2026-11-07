# DBPR licences: county resolution and the county-code audit

**Status: V13, open.** The DBPR county-code map is **inferred, not officially verified.** No authoritative DBPR code list or extract layout has been found. Everything below is a consistency check against Census data. It is not a verification. The map stays labelled `inferred_v13` (`COUNTY_CODE_MAP_STATUS` in `backend/app/sources/dbpr/importer.py`) until an official DBPR document or another reliable official source confirms it.

## 1. Inferred code map

The rule: Florida's 67 counties in alphabetical order, with Dade listed under D, numbered from 11. Alachua = 11, Clay = 20, Duval = 26, St. Johns = 65 and Washington = 77.

FIPS follows the same alphabetical order in odd numbers, except Miami-Dade, which is 12086.

Codes `7xx` are out-of-state addresses (GA, TX, NC, AL, CA, …). A blank code is mostly out-of-state as well.

## 2. Resolution policy (`resolve_geo`)

The inputs are the raw county code, the ZIP (as a ZCTA mapped to its Florida counties) and the state.

| Status | When | County assigned | Flags |
|---|---|---|---|
| `agree` | Code maps to a county the ZIP lies in | Yes | `zip_spans_counties` if the ZIP covers several counties |
| `code_only` | Code known; ZIP missing or not a Florida ZCTA (e.g. PO-box-only ZIPs) | Yes, **inferred** | `county_from_inferred_code_map`, plus `zip_missing` or `zip_not_in_florida_zcta` |
| `zip_only` | Code blank or unknown; ZIP lies in exactly one county | Yes, **never silently** | `county_from_zip_only`, plus `county_code_blank` or `county_code_unknown:<code>` |
| `conflict` | Code county is not among the ZIP's counties | **No** | `county_zip_conflict` |
| `ambiguous` | Code blank or unknown; ZIP spans several counties | **No** | |
| `out_of_state` | State is not FL, or the code is `7xx` | **No** | |
| `unresolved` | Nothing usable | **No** | |

Other rules:

- **No county means excluded.** A row without a county is never counted in a target county and can never attach evidence there.
- **Matching stays in the resolved county.** Matching only considers locations in the resolved county, so a licence cannot attach to a same-named business across a county line within a shared ZIP.
- **Lower confidence for partial evidence.** Any assignment other than `agree`, or any flag (including `address_incomplete`), lowers the fact's confidence (× 0.8).
- **Audit trail.** For every accepted record, the following are stored in `source_record.payload.geo` and on the licence fact (`geo_status`, `geo_flags`, `geo_reason`, `county_code_map`):
  - raw `county_code`, `zip` and `state`;
  - the status and flags;
  - a human-readable reason;
  - `code_map: "inferred_v13"`.

  The source values themselves are never modified.
- **Tests.** The policy is covered by `backend/tests/test_dbpr_geo.py`:
  - every status;
  - invariants over every code × ZIP × state combination;
  - stored audit fields;
  - the county-restricted matching.

## 3. Audit of the 2026-11 extracts

Reproduce from `backend/` with:

```bash
python scripts/dbpr_geo_audit.py ../data ../data/dbpr/CONSTRUCTIONLICENSE_1.csv ../data/dbpr/COSMETOLOGYLICENSE_1.csv
```

The script reads only local files: the two extracts, plus the Census ZCTA-county relationship file and the county boundaries that the pipeline already downloads.

| Measure | Result |
|---|---|
| Rows read | 695,419 |
| Rows with a mapped code and a Florida ZCTA ZIP | 626,509 |
| Code county agrees with the ZIP's counties | 621,542 (**99.21%**) |
| Disagrees | 4,967 (**0.79%**) |
| Codes seen | 67 of 67 |
| Lowest per-code agreement | 88.8% (code 49, Liberty: a small county with ZIPs that cross its borders) |

### 3.1 The 0.79% disagreements

| Kind | Rows | Interpretation |
|---|---|---|
| ZIP lies in a county **adjacent** to the code's county | 3,290 (66%) | Border ZIPs. ZCTAs and postal delivery areas do not follow county lines exactly, so either value may be right. |
| ZIP lies in a **non-adjacent** county | 1,677 (34%) | In the sampled rows the city name matches the ZIP, not the code. The code is probably stale (the licensee moved and the code was not updated) or the ZIP is mistyped. Either way the row cannot be trusted. |

Sampled non-adjacent rows (code, code county → ZIP, ZIP county, city):

- 23 Miami-Dade → 32226 Duval, "Jacksonville"
- 16 Broward → 32216 Duval, "Jacksonville"
- 60 Palm Beach → 32082 St. Johns, "Ponte Vedra Beach"
- 37 Hernando → 32003 Clay, "Orange Park"
- 26 Duval → 32428 Bay/Jackson/Washington, "Chipley"
- 23 Miami-Dade → 32091 Bradford/Clay, "North Miami". This looks like a ZIP typo.

**Rows touching Duval, St. Johns or Clay** (the code county or any ZIP county is a target):

- 422 disagree: 211 adjacent and 211 non-adjacent.
- 270 of them would otherwise be active-licence evidence.

**All of these are excluded** (status `conflict`). None is placed in a target county, not even where the city name suggests one.

The most frequent target pairs:

| Code county | ZIP county | Rows |
|---|---|---|
| Duval | Clay | 35 |
| Flagler | St. Johns | 32 |
| Duval | Broward | 28 |
| St. Johns | Duval | 28 |
| Duval | St. Johns | 27 |
| Clay | Duval | 22 |
| Duval | Nassau | 21 |

### 3.2 Resolution status of rows touching the target counties

| Status | Rows | Of which otherwise active evidence |
|---|---|---|
| `agree` | 39,849 | 21,103 |
| `code_only` (inferred, flagged) | 821 | 229 |
| `conflict` (excluded) | 422 | 270 |
| `zip_only` (flagged) | 117 | 81 |
| `out_of_state` (excluded) | 23 | 16 |
| `ambiguous` (excluded) | 1 | 1 |

`code_only` rows are mostly PO-box-only ZIPs that have no ZCTA, such as 32241, 32236, 32085, 32240, 32004 and 32067. Their county rests on the inferred code map alone. They are flagged `county_from_inferred_code_map`, carry lower confidence, and should be read as inferred.

### 3.3 Cost of the conservative policy

**Excluded evidence.** 270 + 16 + 1 = 287 otherwise-active licences touching the target counties are excluded. That is 1.3% of the 21,700 eligible.

- These rows can never be wrong evidence in a target county.
- A later improvement could recover the adjacent-county cases with a geocoded street address. That is not part of Phase 1.

**Limitation.** Each row was attributed to one source in the audit, but some fall in border ZIPs where the code may be right and the ZCTA wrong. Excluding them may drop genuine local licensees. This is a trade-off chosen for correctness, not a claim that the code is wrong.

## 4. What closes V13

V13 closes only when one of the following confirms both the code list and the column meanings:

- an official DBPR extract layout or code table;
- a written DBPR answer;
- another reliable official source.

Until then:

- the map stays `inferred_v13`;
- code-only assignments stay flagged;
- this audit is the evidence the inference rests on.
