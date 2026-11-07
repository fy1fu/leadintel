"""SCORING.md §7 invariants that apply to provisional scoring (Phase 1)."""
from datetime import date, timedelta

import yaml
from hypothesis import given, settings, strategies as st

from app.config import REPO_ROOT
from app.scoring.model import Features, score
from app.targeting.keywords import match

D = REPO_ROOT / "config" / "defaults"
SC, IND, KW = (yaml.safe_load(open(D / f"{n}.yaml")) for n in ("scoring", "industries", "keywords"))
TODAY = date(2026, 11, 3)


def feats(**kw):
    base = dict(location_id=1, name="X", aliases=[], category=None, operating_status="unknown", chain_flag=False,
                overture_confidence=None, platforms=[], phones=[], emails=[], has_mail_address=True, sources={"sunbiz"},
                orgs=[], in_target=True, last_seen=TODAY, industry=None)
    base.update(kw)
    return Features(**base)


def run(f, name=None):
    hits, _ = match({"name": name or f.name, "category": f.category}, KW)
    return score(f, hits, SC, IND, TODAY)


def established_no_website():
    return feats(name="Bob's Pressure Washing", category="pressure_washing", operating_status="open", overture_confidence=0.95,
                 platforms=[("facebook", "social")], phones=["ingested"], sources={"overture", "sunbiz"},
                 orgs=[{"filing_kind": "llc", "registration_date": date(2016, 5, 1), "status": "active"}], industry="pressure_washing")


def test_established_business_outranks_brand_new_llc_without_evidence():  # invariant 1
    est = run(established_no_website())
    new = run(feats(name="Elite Pressure Washing Mobile Detailing Cleaning LLC", industry="pressure_washing",
                    orgs=[{"filing_kind": "fictitious_name", "registration_date": TODAY - timedelta(days=1), "status": "active"}]))
    assert est.bos > new.bos and est.lps > new.lps
    assert new.bos <= 35 and new.evidence_points == 0 and "evidence_gate:35" in new.caps


filing = st.sampled_from(["fictitious_name", "llc", "corporation", "foreign_llc", "nonprofit"])
groups = st.lists(st.sampled_from(["Pressure Washing", "Lawn Care", "Cleaning", "Mobile Detailing", "Boat Repair",
                                   "HVAC", "Roofing", "Pest Control", "Junk Removal", "Pool Cleaning"]), max_size=6)


@settings(max_examples=300, deadline=None)
@given(kinds=st.lists(filing, min_size=1, max_size=4), words=groups, days=st.integers(0, 3000),
       industry=st.sampled_from([None, "cleaning", "landscaping", "pressure_washing", "home_trades"]))
def test_no_combination_of_positive_signals_breaks_the_cap(kinds, words, days, industry):  # invariant 11
    orgs = [{"filing_kind": k, "registration_date": TODAY - timedelta(days=days), "status": "active",
             "owner_registration_date": TODAY - timedelta(days=days + 4000)} for k in kinds]
    if days >= 365:  # registry age itself is evidence; keep this generator to the no-evidence case
        orgs = [dict(o, registration_date=TODAY - timedelta(days=days % 365)) for o in orgs]
    r = run(feats(name=" ".join(words) or "Acme", orgs=orgs, industry=industry))
    assert r.evidence_points == 0
    assert r.bos <= 35 and r.lps <= 35


def test_closed_and_chain_and_holding():
    assert run(feats(name="Closed Co", operating_status="permanently_closed", overture_confidence=0.9, sources={"overture"})).bos == 0
    chain = run(feats(name="Valley Bank", chain_flag=True, operating_status="open", overture_confidence=0.9, phones=["ingested"],
                      sources={"overture"}))
    indie = run(feats(name="Valley Credit Shop", operating_status="open", overture_confidence=0.9, phones=["ingested"], sources={"overture"}))
    assert chain.bos < indie.bos  # invariant 8
    hold = run(feats(name="Coastal Capital Holdings LLC", orgs=[{"filing_kind": "llc", "registration_date": TODAY, "status": "active"}]))
    assert any(x.factor == "likely_holding_or_investment_entity" for x in hold.factors)


def test_verified_contacts_rank_above_ingested_contacts():  # invariant 13
    a = run(feats(name="Same Co", operating_status="open", overture_confidence=0.9, phones=["ingested"], sources={"overture", "sunbiz"}))
    b = run(feats(name="Same Co", operating_status="open", overture_confidence=0.9, phones=["verified"], sources={"overture", "sunbiz"}))
    assert a.bos == b.bos and b.lps > a.lps


def test_qualification_is_separate_from_ranking():
    q = run(established_no_website())
    assert q.qualification == "provisionally_qualified"
    mail_only = run(feats(name="Quiet Lawn Co", category="landscaping", operating_status="open", overture_confidence=0.9,
                          sources={"overture", "sunbiz"}, platforms=[("instagram", "social")], phones=[],
                          has_mail_address=True, industry="landscaping",
                          orgs=[{"filing_kind": "llc", "registration_date": date(2015, 1, 1), "status": "active"}]))
    assert mail_only.qualification == "provisionally_qualified"  # a legitimate mailing address is a contact route
    assert mail_only.lps < q.lps                                  # ...but ranks lower (LPS decides priority)
    no_route = run(feats(name="Ghost Co", operating_status="open", overture_confidence=0.9, sources={"overture", "sunbiz"},
                         has_mail_address=False, orgs=[{"filing_kind": "llc", "registration_date": date(2015, 1, 1), "status": "active"}]))
    assert no_route.qualification == "not_qualified" and "no_usable_contact_route" in no_route.reasons
    site = run(feats(name="Has Site", operating_status="open", overture_confidence=0.9, phones=["ingested"],
                     platforms=[("website", "independent")], sources={"overture", "sunbiz"},
                     orgs=[{"filing_kind": "llc", "registration_date": date(2015, 1, 1), "status": "active"}]))
    assert site.qualification == "needs_research"


def test_deterministic_and_every_component_explained():  # invariants 6 and 9
    a, b = run(established_no_website()), run(established_no_website())
    assert (a.bos, a.lps, [x.__dict__ for x in a.factors]) == (b.bos, b.lps, [x.__dict__ for x in b.factors])
    comps = {x.factor for x in a.factors if x.component == "component"}
    assert comps == set(SC["bos_weights"])


def test_presence_types():
    assert run(feats(platforms=[("facebook", "social"), ("yelp", "listing")])).presence_type == "facebook_only"
    assert run(feats(platforms=[("facebook", "social"), ("instagram", "social")])).presence_type == "social_only"
    assert run(feats(platforms=[("website", "independent"), ("facebook", "social")])).presence_type == "independent_website"
    assert run(feats(platforms=[("yelp", "listing")])).presence_type == "directory_only"
