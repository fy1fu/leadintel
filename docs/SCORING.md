# Classification and scoring (v0.3)

This document consolidates the v0.1 scoring design (which survived only in git history after v0.2), the v0.2 free-data substitutions and the v0.3 acquisition signals. All weights shown are **initial defaults** in versioned config (`config/scoring.yaml`, `config/keywords.yaml`, `config/industries/*.yaml`). Every computed score stores its config version.

Three scores stay separate:

| Score | Question it answers | Range |
|---|---|---|
| **WQS**, Website Quality Score | How good is the existing independent website? | 0–100, or **N/A** |
| **BOS**, Business Opportunity Score | How much could this business benefit from a better website, and is it a credible customer? | 0–100 |
| **LPS**, Lead Priority Score | How worthwhile and how actionable is contacting this business now? | 0–100 |

---

## 1. Online presence: two axes

A single label can't express "has a Facebook page *and* the website discovery is incomplete", so each business gets two values.

### 1.1 Website status (about the independent website)

| Value | Meaning |
|---|---|
| `confirmed` | Independent website associated with confidence ≥ 0.8 and serving real content |
| `inaccessible` | DNS/TLS/5xx/timeouts on ≥ 2 attempts on different days. **WQS = N/A**, never 0. |
| `inactive` | Parked, for-sale, "coming soon", or default CMS page |
| `uncertain` | Association confidence 0.4–0.8 → review queue |
| `none_found` | **All** discovery steps ran and found nothing |
| `incomplete` | Some discovery steps not yet run, failed, or blocked |

### 1.2 Presence type (what the public footprint consists of)

| Value | Rule |
|---|---|
| `independent_website` | `website_status = confirmed` (quality then comes from WQS) |
| `facebook_only` | No independent site; Facebook profile found; no other social profile |
| `instagram_only` | No independent site; Instagram profile found; no other social profile |
| `social_only` | No independent site; two or more social profiles (Facebook, Instagram, TikTok, LinkedIn, Nextdoor, Linktree …) |
| `directory_only` | No independent site or social profile; present only in listings/registries (Overture/OSM/DBPR/BTR/NPI/Sunbiz) |
| `unknown` | Website status `incomplete` and no social or directory evidence yet |

**A social-network, link-in-bio, directory or builder-subdomain URL is never an independent website.** Facebook, Instagram, TikTok, Linktree, Yelp, Nextdoor and Google listings are presence, not websites. A builder subdomain such as `*.wixsite.com` counts as an independent website, but with an "unbranded domain" deficiency.

The nine categories required in v0.3 §3 map onto the two axes:

| v0.3 category | Axes |
|---|---|
| 1 No independent website found | `website_status = none_found` |
| 2 Facebook-only | `presence = facebook_only` |
| 3 Instagram-only | `presence = instagram_only` |
| 4 Social-media-only | `presence = social_only` |
| 5 Directory-only | `presence = directory_only` |
| 6 Website exists but inaccessible | `website_status = inaccessible` |
| 7 Website appears outdated | `confirmed` and WQS < 60, or the modernity dimension < 0.4 |
| 8 Website reasonably good | `confirmed` and WQS ≥ 70 |
| 9 Discovery incomplete | `website_status = incomplete` |

### 1.3 Real-data check (free sources only)

Overture release 2026-10-21, central-Jacksonville bounding box, open places only. "Social-only" means social or link URLs without an independent site.

| Niche (taxonomy match) | Open | Independent site | Social-only | No web/social | With phone |
|---|---|---|---|---|---|
| Pressure washing | 32 | 24 | 0 | 8 | 32 |
| Lawn / landscaping | 259 | 202 | 21 | 36 | 255 |
| Cleaning | 435 | 324 | 52 | 59 | 429 |
| Handyman / home repair | 946 | 750 | 60 | 136 | 856 |
| Pest control | 99 | 88 | 1 | 10 | 98 |
| Roofing | 244 | 211 | 2 | 31 | 243 |
| HVAC | 198 | 174 | 3 | 21 | 197 |
| Plumbing | 134 | 116 | 2 | 16 | 130 |
| Electrical | 152 | 115 | 4 | 33 | 150 |
| Auto detailing / tint | 229 | 155 | 44 | 30 | 222 |
| Auto repair | 1013 | 736 | 162 | 115 | 969 |
| Marine / boat / charter | 124 | 100 | 14 | 10 | 121 |

These are *listing-level* counts. A listed website still has to be verified, and some "no web/social" businesses will turn out to have a site, which is why `incomplete` exists. **Overture provides no review counts or ratings.**

---

## 2. Keyword scoring (`config/keywords.yaml`)

```yaml
version: 1
match:
  mode: token_phrase          # word-boundary, phrase-aware; never raw substring
  case_insensitive: true
  variants: true              # wash/washing/washer, clean/cleaning, detail/detailing
groups:
  pressure_washing: {weight: +12, phrases: ["pressure wash", "power wash", "soft wash", "pressure washing"]}
  lawn_landscaping: {weight: +10, phrases: ["lawn", "landscaping", "landscape", "irrigation", "tree service", "sod"]}
  cleaning:         {weight: +10, phrases: ["cleaning", "maid", "janitorial", "carpet cleaning", "upholstery cleaning", "window cleaning", "gutter cleaning"]}
  handyman_repair:  {weight: +10, phrases: ["handyman", "home repair", "remodeling", "flooring", "paint", "painting", "fence", "fencing"]}
  junk_moving:      {weight: +9,  phrases: ["junk", "hauling", "moving"]}
  pest:             {weight: +9,  phrases: ["pest", "termite", "exterminat*"]}
  trades:           {weight: +9,  phrases: ["roof", "roofing", "hvac", "plumbing", "electrical", "appliance repair", "welding", "septic"]}
  pool:             {weight: +9,  phrases: ["pool cleaning", "pool maintenance", "pool service"]}
  mobile_auto:      {weight: +10, phrases: ["mobile detail*", "auto detailing", "car wash", "window tint*", "tint", "mobile mechanic", "auto repair"]}
  marine:           {weight: +10, phrases: ["marine", "boat", "boat repair", "boat detailing", "charter", "fishing charter", "dock construction"]}
  generic_mobile:   {weight: +4,  phrases: ["mobile"]}     # weak alone; only boosts with a trade word
negative:
  holding:     {penalty: -15, phrases: ["holdings", "holding company"]}
  investment:  {penalty: -12, phrases: ["investments", "capital", "ventures", "equities"]}
  realty:      {penalty: -6,  phrases: ["realty"]}         # a realtor can still be a prospect
  trust:       {penalty: -10, phrases: ["trust"]}
  properties:  {penalty: -8,  phrases: ["properties"], neutralized_by: ["maintenance", "cleaning", "lawn", "landscaping", "management", "services"]}
hard_exclude: []              # user-managed; each entry stores a reason
```

Rules:
- **Negative keywords penalize; they never exclude** unless the user adds a `hard_exclude` entry.
- `neutralized_by` removes a penalty when a customer-facing trade word is also present, e.g. "Coastal Properties Lawn Maintenance".
- Matching runs on the legal name, DBA names, Overture/OSM names and categories. Each hit stores the term, matched text, field and source.
- The lead profile lists every keyword that changed a score.
- Tests cover false positives (`prewash`, `Detailed Analytics LLC`, `Capitalize Marketing`) and phrase boundaries.

---

## 3. Entity-type weighting (registry leads)

Initial-priority adjustment for **registry-derived** candidates only. Businesses found through operating-business sources are not affected.

| Filing kind | Weight | Rationale |
|---|---|---|
| Fictitious name (DBA), new | +10 | Trade name: stronger sign of a customer-facing business |
| DBA owned by an existing entity formed ≥ 12 months earlier | +6, plus the link to that entity | Established business with a new trade name. It may already have a website, so it goes through website discovery. |
| Domestic LLC / profit corporation | 0 | Baseline |
| Foreign LLC / corporation | −3 | Often an out-of-state company registering locally; may still be relevant |
| Non-profit | −8 | Usually not a purchase prospect (configurable) |
| Registered at a registered-agent or mass-filer address | −10, and no operating location | Not evidence of a physical business |

---

## 4. Business Opportunity Score (BOS)

### 4.1 Components (v0.1 structure, v0.3 inputs)

| Component | Weight | Inputs |
|---|---|---|
| Website need | 0.30 | Presence type and website status: `none_found` / `facebook_only` / `instagram_only` / `social_only` / `directory_only` high; `inaccessible` / `inactive` high; outdated (low WQS) graded; missing quote/contact path, not mobile-friendly, broken features; gap to local peers |
| Business value | 0.15 | Industry value tier (profile); high-value services; multi-city service area |
| Acquisition dependence | 0.15 | How much the industry relies on customers searching online (profile); peer website adoption rate |
| Competitive pressure | 0.15 | Share of local peers with independent websites; peer median and top-quartile WQS vs the prospect |
| Establishment and activity | 0.25 | Operating evidence (§4.3): years active, multi-source confirmation, active licence / BTR, recent DBA or licence (a growth signal), social activity, **review count and rating if available** (manual or paid source) |

### 4.2 Multipliers and caps

| Condition | Effect |
|---|---|
| Closed / inactive (Overture `permanently_closed`, Sunbiz dissolution event, expired licence) | × 0 |
| National chain or franchise location (Wikidata brand ID, recurring brand, corporate domain) | × 0.1 |
| Holding / investment entity (keywords + no operating evidence) | × 0.2 |
| Not customer-facing (category) | × 0.2 |
| Duplicate (merged into another record) | Not scored |
| Strong website (WQS ≥ 85 and peer percentile ≥ 75) | BOS capped at 25 |

### 4.3 Operating-evidence gate (new)

Evidence points, each with its source:

| Evidence | Points |
|---|---|
| Listed as open by an operating-business source (Overture, OSM) | 2 |
| Listing confirmed by ≥ 2 independent sources (e.g. Overture contributors, DBPR) | 1 |
| Active DBPR licence or current BTR | 2 |
| Phone number published | 1 |
| Social profile | 1 |
| Reviews recorded (manual or paid source) | 2 |
| Registry entity ≥ 12 months old with no dissolution | 1 |

| Total evidence | BOS cap |
|---|---|
| 0 (registry record only) | **35** |
| 1–2 | 60 |
| ≥ 3 | none |

This is what makes "no website" alone insufficient.

---

## 5. Website Quality Score (WQS), restored from v0.1

```
criterion c_i ∈ [0,1]   measured → deterministic transform; assessed → anchored 1–5 rubric → (r−1)/4
dimension  D_k = Σ w_ik·c_i / Σ w_ik            (criteria applicable to the resolved industry profile)
WQS        = 100 · Σ W_k·D_k / Σ W_k            (dimensions: visual, UX, effectiveness, technical, content, SEO, modernity)
confidence = f(criteria coverage, association confidence, crawl completeness, assessment agreement)
```

- Unknown criteria are excluded and lower confidence; they are never scored as 0.
- Missing features count only if they are `required` / `expected` in the profile.
- With the `none` AI provider, subjective criteria are "not assessed".
- Interpretation bands (initial): 90+ excellent · 80–89 very good · 70–79 good · 60–69 average · 40–59 below average · 20–39 poor · < 20 severely deficient.

---

## 6. Lead Priority Score (LPS)

```
actionability A = 0.35·contact_completeness + 0.25·business_verification
                + 0.25·assessment_confidence + 0.15·freshness            (each 0–1)
LPS = BOS · (0.4 + 0.6·A)
bands: High ≥ 70 · Medium 45–69 · Low < 45      (configurable)
```

- **contact_completeness** counts only published contact data, weighted by channel: phone 0.5, email 0.3, website contact form 0.2. A mailing address alone gives 0.2; it enables mail outreach only.
- **Sunbiz-only leads usually have no phone or email**, so their LPS stays low until another source corroborates them. This is intended.

### Factor ledger (explainability)

Every score writes one row per factor, and the lead profile shows them all:

| Field | Example |
|---|---|
| component | `establishment` |
| factor | `active_dbpr_licence` |
| effect | +8.0 |
| evidence | DBPR `cilb_registered.csv`, licence `RG…`, observed 2026-11-02 |
| provenance | `registry` |

---

## 7. Scoring invariants (automated tests)

1. An established business with ≥ 40 recorded reviews, an active listing and no independent website **outranks** a new LLC with no operating evidence (BOS and LPS).
2. A record with no operating evidence never exceeds BOS 35.
3. No website ⇒ WQS = N/A (never 0). Inaccessible ⇒ N/A.
4. A Facebook-only business never has `website_status = confirmed`.
5. Negative keywords alone never remove a lead. Only `hard_exclude` does, and it records a reason.
6. Same inputs + same config version ⇒ identical scores.
7. A strong website (WQS ≥ 85, high peer percentile) ⇒ BOS ≤ 25.
8. Franchise locations score below comparable independents.
9. Every score has ≥ 1 factor-ledger row per non-zero component, and every factor references evidence.
10. Gap statements and recommendations contain no revenue-increase claims (wording lint).

---

## 8. Outcome-based tuning (Phase 5)

- **Snapshots at contact time:** when a lead is first contacted, its scores, factors and config version are frozen, so outcomes are attributed to what was known at the time.
- **Weight proposals:** a monthly analysis compares conversion by industry, source, presence category and factor. It proposes weight changes as a diff with the supporting counts.
- **Small-sample guard:** no proposal is made for segments with fewer than a configurable minimum number of outcomes (default 20 contacted).
- **Approval:** changes apply only after user approval, produce a new config version, and can be reverted in one step. Historical scores keep their original version.
