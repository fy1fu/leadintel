import pytest
import yaml

from app.config import REPO_ROOT
from app.targeting.keywords import hard_excluded, match

CFG = yaml.safe_load(open(REPO_ROOT / "config" / "defaults" / "keywords.yaml"))


def run(name, category=None):
    hits, total = match({"name": name, "category": category}, CFG)
    return {(h.group, h.neutralized) for h in hits}, total


@pytest.mark.parametrize("name,group", [
    ("Jax Pressure Washing LLC", "pressure_washing"), ("First Coast Power Wash", "pressure_washing"),
    ("Green Lawn Care", "lawn_landscaping"), ("Sparkle Cleaning Co", "cleaning"), ("Mobile Detailing by Ray", "mobile_auto"),
    ("Coastal Boat Maintenance", "marine"), ("A1 HVAC & Plumbing", "trades"), ("Bob's Exterminators", "pest"),
    ("Ace Window Tinting", "mobile_auto"), ("Mayport Fishing Charters", "marine"),
])
def test_positive_matches(name, group):
    groups, total = run(name)
    assert (group, False) in groups and total > 0


@pytest.mark.parametrize("name", ["Prewash Supply", "Detailed Analytics LLC", "Lawnmower Museum Gift Shop Antiques",
                                  "Capitalize Marketing", "Trusted Lawn Care", "Capital City Cleaning"])
def test_no_false_negative_penalties_or_substring_hits(name):
    groups, total = run(name)
    if name == "Prewash Supply":
        assert not groups
    if name == "Detailed Analytics LLC":
        assert ("mobile_auto", False) in groups  # "detailed" is an inflection of "detail" (positive groups only)
    if name in ("Capitalize Marketing",):
        assert not any(g == "investment" for g, _ in groups)
    if name in ("Trusted Lawn Care", "Capital City Cleaning"):
        assert not any(g in ("trust", "investment") and not n for g, n in groups)  # invariant 14


def test_negative_penalties_apply_and_never_exclude():
    groups, total = run("Coastal Capital Holdings LLC")
    assert ("holding", False) in groups and ("investment", False) in groups and total == -27
    assert hard_excluded({"name": "Coastal Capital Holdings LLC"}, CFG) is None  # penalties only


def test_properties_neutralized_by_trade_word():
    groups, total = run("Coastal Properties Lawn Maintenance")
    assert ("properties", True) in groups and total > 0


def test_generic_mobile_needs_another_group_and_groups_do_not_stack():
    groups, total = run("Mobile Notary Mobile")
    assert ("generic_mobile", True) in groups and total == 0
    _, t1 = run("Cleaning")
    _, t2 = run("Cleaning Cleaning Cleaning Services")
    assert t1 == t2


def test_category_field_matches_taxonomy_codes():
    groups, _ = run("Smith Services", category="pressure_washing")
    assert ("pressure_washing", False) in groups


def test_hard_exclude_records_reason():
    cfg = dict(CFG, hard_exclude=[{"phrase": "notary", "reason": "user: not a target"}])
    assert hard_excluded({"name": "Mobile Notary Services"}, cfg) == "user: not a target"


def test_industry_classification_needs_whole_category_terms():
    from app.scoring.runner import classify
    ind = yaml.safe_load(open(REPO_ROOT / "config" / "defaults" / "industries.yaml"))
    assert classify("landscaping", set(), ind) == "landscaping"
    assert classify("commercial_cleaning", set(), ind) == "cleaning"
    assert classify("financial_service", set(), ind) is None      # regression: shared word "service"
    assert classify("home_service", set(), ind) is None
    assert classify(None, {"pressure_washing"}, ind) == "pressure_washing"
