"""Provisional BOS / LPS, presence and qualification (docs/SCORING.md §4.4, §6; PHASE1.md M2).

Pure functions: features in, scores + factor ledger out. One pipeline for every stage:

    components C_k -> base B = 100*sum(W_k*C_k) -> targeting T (bounded) -> X = clamp(B+T)
    -> multipliers -> caps applied LAST as min()  (evidence gate, ...)

Phase 1 limitations, made explicit in factors: website quality and competitors are unknown (neutral
values, low assessment confidence); website status is `incomplete` because website discovery runs
in Phase 2; licences/BTR evidence arrive in M3; reviews only via manual observation (Phase 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.targeting.keywords import Hit


@dataclass
class Features:
    location_id: int
    name: str
    aliases: list[str]
    category: str | None
    operating_status: str
    chain_flag: bool
    overture_confidence: float | None
    platforms: list[tuple[str, str]]          # (platform, platform_kind) from online_profile
    phones: list[str]                          # verification states of phone contacts
    emails: list[str]
    has_mail_address: bool
    sources: set[str]
    orgs: list[dict]                           # {filing_kind, registration_date, status, owner_registration_date}
    in_target: bool
    last_seen: date | None
    industry: str | None = None


@dataclass
class Factor:
    component: str
    factor: str
    effect: float
    evidence: dict = field(default_factory=dict)
    provenance: str | None = None


@dataclass
class Result:
    bos: float
    lps: float
    pre_cap: float
    caps: list[str]
    evidence_points: int
    presence_type: str
    qualification: str
    reasons: list[str]
    factors: list[Factor]
    lps_factors: list[Factor]
    confidence: float


def _presence(platforms: list[tuple[str, str]], has_registry_or_listing: bool) -> str:
    kinds = {k for _, k in platforms}
    if "independent" in kinds:
        return "independent_website"
    social = {p for p, k in platforms if k in ("social", "link_hub")}
    if social == {"facebook"}:
        return "facebook_only"
    if social == {"instagram"}:
        return "instagram_only"
    if social:
        return "social_only"
    return "directory_only" if has_registry_or_listing else "unknown"


def evidence(f: Features, cfg: dict, today: date) -> tuple[int, list[Factor]]:
    pts = cfg["evidence_points"]
    out: list[Factor] = []
    min_conf = float(cfg.get("overture_min_confidence_for_open", 0.6))
    if f.operating_status == "open" and (f.overture_confidence or 0) >= min_conf:
        out.append(Factor("evidence", "open_listing", pts["open_listing"], {"overture_confidence": f.overture_confidence}, "directory"))
    if len(f.sources) >= 2:
        out.append(Factor("evidence", "multi_source", pts["multi_source"], {"sources": sorted(f.sources)}, "directory"))
    if f.phones:
        out.append(Factor("evidence", "phone_published", pts["phone"], {"count": len(f.phones)}, "directory"))
    if any(k in ("social", "link_hub") for _, k in f.platforms):
        out.append(Factor("evidence", "social_profile", pts["social_profile"],
                          {"platforms": sorted({p for p, k in f.platforms if k in ("social", "link_hub")})}, "directory"))
    old = [o for o in f.orgs if o.get("status") == "active" and o.get("registration_date")
           and o["registration_date"] <= today - timedelta(days=365)]
    if old:
        out.append(Factor("evidence", "registry_age_12m", pts["registry_age_12m"],
                          {"registered": min(o["registration_date"] for o in old).isoformat()}, "registry"))
    # NOTE: a filing's own recency or kind earns no evidence (SCORING.md §4.3). Licences/BTR: M3. Reviews: manual, Phase 2.
    return int(sum(x.effect for x in out)), out


def entity_effect(f: Features, cfg: dict, today: date) -> tuple[float, Factor | None]:
    if not f.orgs:
        return 0.0, None
    eff = cfg["entity_type_effects"]
    best: tuple[float, str, dict] | None = None
    for o in f.orgs:
        k = o.get("filing_kind")
        if k == "fictitious_name":
            own = o.get("owner_registration_date")
            reg = o.get("registration_date")
            if own and reg and own <= reg - timedelta(days=365):
                cand = (float(eff["fictitious_name_of_established"]), "dba_of_established_entity", o)
            else:
                cand = (float(eff["fictitious_name_new"]), "fictitious_name", o)
        elif k in ("llc", "corporation"):
            cand = (float(eff.get(k, 0)), k, o)
        elif k and k.startswith("foreign"):
            cand = (float(eff["foreign"]), k, o)
        elif k and "nonprofit" in k:
            cand = (float(eff["nonprofit"]), k, o)
        else:
            continue
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return 0.0, None
    o = best[2]
    return best[0], Factor("targeting", f"filing_kind:{best[1]}", best[0],
                           {"registered": o.get("registration_date").isoformat() if o.get("registration_date") else None}, "registry")


def score(f: Features, hits: list[Hit], scfg: dict, industries: dict, today: date) -> Result:
    W = scfg["bos_weights"]
    profile = industries["profiles"].get(f.industry or "", industries.get("unclassified", {}))
    if f.industry is None:
        profile = industries.get("unclassified", {"value_tier": 0.4, "online_dependence": 0.5})
    pts, ev_factors = evidence(f, scfg, today)
    presence = _presence(f.platforms, bool(f.sources))
    wn = scfg.get("provisional_website_need", {})
    website_need = {"independent_website": wn.get("independent_listed", 0.4), "facebook_only": wn.get("social_only_listed", 0.75),
                    "instagram_only": wn.get("social_only_listed", 0.75), "social_only": wn.get("social_only_listed", 0.75),
                    "directory_only": wn.get("listing_only", 0.65)}.get(presence, wn.get("nothing_listed", 0.6))
    comps = {"website_need": website_need, "business_value": float(profile.get("value_tier", 0.4)),
             "acquisition_dependence": float(profile.get("online_dependence", 0.5)),
             "competitive_pressure": 0.5, "establishment": min(pts, 6) / 6}
    factors = [Factor("component", k, round(100 * W[k] * v, 2),
                      {"value": round(v, 3), "weight": W[k], **({"note": "neutral until Phase 3 competitor analysis"} if k == "competitive_pressure" else {}),
                       **({"presence_type": presence, "note": "provisional: website discovery runs in Phase 2"} if k == "website_need" else {}),
                       **({"industry": f.industry or "unclassified"} if k in ("business_value", "acquisition_dependence") else {})})
               for k, v in comps.items()]
    factors += ev_factors
    base = sum(x.effect for x in factors if x.component == "component")
    # targeting adjustment T (bounded)
    kw = [h for h in hits if not h.neutralized]
    t_raw = sum(h.effect for h in kw)
    ent, ent_factor = entity_effect(f, scfg, today)
    tb = scfg["targeting_adjustment"]
    T = max(float(tb["min"]), min(float(tb["max"]), t_raw + ent))
    for h in hits:
        factors.append(Factor("targeting", f"keyword:{h.group}", 0.0 if h.neutralized else h.effect,
                              {"term": h.term, "matched": h.matched_text, "field": h.field, "neutralized": h.neutralized}, "inferred"))
    if ent_factor:
        factors.append(ent_factor)
    if T != t_raw + ent:
        factors.append(Factor("targeting", "targeting_bound", round(T - (t_raw + ent), 2), {"bounds": tb}))
    x = max(0.0, min(100.0, base + T))
    mult, mreasons = 1.0, []
    if f.operating_status == "permanently_closed":
        mult = 0.0
        mreasons.append("closed")
    if f.chain_flag:
        mult *= 0.1
        mreasons.append("chain_or_franchise")
    if pts == 0 and any(h.polarity == "negative" and h.group in ("holding", "investment") and not h.neutralized for h in hits):
        mult *= 0.2
        mreasons.append("likely_holding_or_investment_entity")
    if (f.category or "") in set(scfg.get("non_customer_facing_categories", [])):
        mult *= 0.2
        mreasons.append("not_customer_facing_category")
    y = x * mult
    for r in mreasons:
        factors.append(Factor("multiplier", r, 0.0, {"multiplier_total": round(mult, 3)}))
    caps = []
    cap_val = scfg["evidence_caps"].get(str(pts))
    bos = y
    if cap_val is not None and y > float(cap_val):
        caps.append(f"evidence_gate:{cap_val}")
        factors.append(Factor("cap", "evidence_gate", round(float(cap_val) - y, 2), {"evidence_points": pts, "cap": cap_val}))
        bos = float(cap_val)
    bos = round(bos, 1)
    # ---- LPS
    L = scfg["lps"]
    mult_v = L["verification_multipliers"]
    cw = L["channel_weights"]
    contact = 0.0
    if f.phones:
        contact += cw["phone"] * max(mult_v.get(s, 0.6) for s in f.phones)
    if f.emails:
        contact += cw["email"] * max(mult_v.get(s, 0.6) for s in f.emails)
    if contact == 0 and f.has_mail_address:
        contact = cw.get("mail", 0.2)
    contact = min(1.0, contact)
    verification = min(pts / 6, 1.0)
    assessment = 0.3  # provisional: no website assessment yet
    freshness = 1.0 if f.last_seen and (today - f.last_seen).days <= int(scfg.get("freshness_days", 60)) else 0.5
    w = L["weights"]
    A = w["contact"] * contact + w["verification"] * verification + w["assessment"] * assessment + w["freshness"] * freshness
    lps = round(bos * (float(L["floor"]) + (1 - float(L["floor"])) * A), 1)
    lps_factors = [Factor("actionability", "contact_completeness", round(contact, 3),
                          {"phones": f.phones, "emails": f.emails, "mail_route": f.has_mail_address}),
                   Factor("actionability", "business_verification", round(verification, 3), {"evidence_points": pts}),
                   Factor("actionability", "assessment_confidence", assessment, {"note": "provisional until Phase 2-3"}),
                   Factor("actionability", "freshness", freshness, {"last_seen": f.last_seen.isoformat() if f.last_seen else None})]
    # ---- qualification (separate from ranking)
    reasons, status = [], None
    if f.operating_status == "permanently_closed":
        reasons.append("closed")
    if f.chain_flag:
        reasons.append("chain_or_franchise")
    if not f.in_target:
        reasons.append("outside_target_area")
    if pts < int(scfg["qualification"]["min_evidence"]):
        reasons.append(f"insufficient_operating_evidence ({pts} < {scfg['qualification']['min_evidence']})")
    if not (f.phones or f.emails or f.has_mail_address):
        reasons.append("no_usable_contact_route")
    if "not_customer_facing_category" in mreasons:
        reasons.append("not_customer_facing")
    if reasons:
        status = "not_qualified"
    elif presence == "independent_website":
        status, reasons = "needs_research", ["website listed; quality unknown until Phase 2 analysis"]
    else:
        status, reasons = "provisionally_qualified", [f"no independent website in listing data ({presence}); "
                                                      "to be confirmed by Phase 2 website discovery"]
    confidence = round(0.5 * verification + 0.2 * assessment + 0.3 * contact, 3)
    return Result(bos, lps, round(y, 1), caps, pts, presence, status, reasons, factors, lps_factors, confidence)
