"""Prospect filtering, paging and counts (M1 S2a) - offline.

The SQL builder is checked as text and bound parameters; the endpoints run against stubbed query helpers, so
no database is contacted. Database-backed correctness and performance checks come in S2b.
"""
import itertools
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import business
from app.api.filters import (MAX_POINTS, OPERATING_SQL, WEBSITE_STATUS_SQL, FilterError, ProspectFilter, _ANY_SOURCE,
                             _INDEPENDENT, _SOCIAL, count_query, like_escape, list_query, points_query, summary_query)
from app.api.main import create_app
from app.normalize.core import name_norm
from app.scoring.model import _presence


# ---------------------------------------------------------------- builder

def test_default_filter_only_excludes_merged_locations():
    where, params = ProspectFilter().where()
    assert where == "l.merged_into_id IS NULL" and params == {}


def test_every_filter_is_bound_not_interpolated():
    f = ProspectFilter(counties=["duval", "clay"], niches=["cleaning"], qualifications=["needs_research"],
                       q="Bob's Pressure Washing", website=["listed"], operating=["open"], phone=True, email=False,
                       bos_min=40, bos_max=90, lps_min=10, lps_max=80)
    where, params = f.where()
    assert params["f_counties"] == ["duval", "clay"] and params["f_niches"] == ["cleaning"]
    assert params["f_quals"] == ["needs_research"]
    assert params["f_q"] == "%" + name_norm("Bob's Pressure Washing") + "%"
    assert params["f_bos_min"] == 40.0 and params["f_lps_max"] == 80.0
    for v in ("duval", "clay", "cleaning", "Bob", "needs_research"):
        assert v not in where, f"{v} must be a bound parameter, not SQL text"
    assert "NOT EXISTS (SELECT 1 FROM location_contact" in where      # email=False
    assert "b.value >= :f_bos_min" in where and "p.value <= :f_lps_max" in where


def test_injection_attempts_stay_in_parameters():
    evil = "x'; DROP TABLE location; --"
    f = ProspectFilter(counties=[evil], niches=[evil], q=evil)
    where, params = f.where()
    assert "DROP" not in where and evil in params["f_counties"]
    assert "DROP" not in list_query(f, 50, 0)[0]


def test_name_search_uses_normalized_name_and_escapes_wildcards():
    where, params = ProspectFilter(q="50%_off\\deals").where()
    assert "l.name_norm ILIKE :f_q ESCAPE" in where
    assert params["f_q"] == "%" + like_escape(name_norm("50%_off\\deals")) + "%"
    assert like_escape("a%b_c\\d") == "a\\%b\\_c\\\\d"


def test_name_search_falls_back_to_display_name_when_normalization_is_empty():
    assert name_norm("LLC") == ""
    where, params = ProspectFilter(q="LLC").where()
    assert "l.display_name ILIKE :f_q" in where and params["f_q"] == "%LLC%"


def test_blank_search_is_ignored():
    assert ProspectFilter(q="   ").where() == ("l.merged_into_id IS NULL", {})


def test_unscored_qualification_matches_missing_rows():
    where, params = ProspectFilter(qualifications=["unscored"]).where()
    assert "(q.status IS NULL)" in where and "f_quals" not in params
    where, params = ProspectFilter(qualifications=["needs_research", "unscored"]).where()
    assert "(q.status = ANY(CAST(:f_quals AS text[])) OR q.status IS NULL)" in where


@pytest.mark.parametrize("kwargs, message", [
    ({"website": ["none"]}, "website"),                  # 'none' is deliberately not a value: use none_recorded
    ({"operating": ["open_or_unknown"]}, "operating"),
    ({"qualifications": ["qualified"]}, "qualification"),
    ({"sort": "random()"}, "sort"),
    ({"bos_min": -1}, "bos_min"),
    ({"lps_max": 101}, "lps_max"),
    ({"bos_min": 80, "bos_max": 20}, "bos_min is greater"),
    ({"q": "x" * 201}, "q is longer"),
])
def test_invalid_values_are_rejected(kwargs, message):
    with pytest.raises(FilterError, match=message):
        ProspectFilter(**kwargs)


@pytest.mark.parametrize("sort, order", [("lps", "p.value DESC NULLS LAST, l.id"), ("bos", "b.value DESC NULLS LAST, l.id"),
                                         ("name", "lower(l.display_name), l.id")])
def test_sorting_is_whitelisted_and_stable(sort, order):
    sql, params = list_query(ProspectFilter(sort=sort), 50, 100)
    assert f"ORDER BY {order}\nLIMIT :limit OFFSET :offset" in sql
    assert params["limit"] == 50 and params["offset"] == 100


def test_all_views_share_the_same_where_clause():
    f = ProspectFilter(counties=["duval"], website=["none_recorded"], operating=["unknown"], phone=True)
    where, params = f.where()
    for sql, p in (list_query(f, 50, 0), count_query(f), summary_query(f), points_query(f)):
        assert f"WHERE {where}" in sql
        assert {k: v for k, v in p.items() if k.startswith("f_")} == params


def test_points_query_only_returns_mappable_rows_and_detects_truncation():
    sql, params = points_query(ProspectFilter(), cap=10)
    assert "AND l.geom IS NOT NULL" in sql and params["cap"] == 11


def test_summary_counts_with_and_without_coordinates():
    sql, _ = summary_query(ProspectFilter())
    assert "count(*) FILTER (WHERE l.geom IS NOT NULL) AS with_coordinates" in sql
    assert "coalesce(q.status, 'unscored')" in sql


# ---------------------------------------------------------------- meanings match the scoring code

def _eval_status(sql: str, independent: bool, social: bool, source: bool) -> bool:
    """Evaluates a website-status condition's boolean structure in Python (no database)."""
    expr = sql.replace(_INDEPENDENT, str(independent)).replace(_SOCIAL, str(social)).replace(_ANY_SOURCE, str(source))
    expr = expr.replace("NOT ", "not ").replace(" AND ", " and ")
    assert re.fullmatch(r"[()\sA-Za-z]+", expr), expr
    return eval(expr)  # noqa: S307 - only True/False/not/and/parentheses remain (asserted above)


PRESENCE_TO_STATUS = {"independent_website": "listed", "facebook_only": "social_only", "instagram_only": "social_only",
                      "social_only": "social_only", "directory_only": "none_recorded", "unknown": "unknown"}


@pytest.mark.parametrize("platforms, source", [
    (p, s) for p, s in itertools.product([
        [], [("website", "independent")], [("facebook", "social")], [("instagram", "social")],
        [("linktree", "link_hub")], [("facebook", "social"), ("x", "social")], [("yelp", "listing")],
        [("website", "independent"), ("facebook", "social")]], [False, True])])
def test_website_status_matches_scoring_presence(platforms, source):
    kinds = {k for _, k in platforms}
    expected = PRESENCE_TO_STATUS[_presence(platforms, source)]
    flags = dict(independent="independent" in kinds, social=bool(kinds & {"social", "link_hub"}), source=source)
    matches = [w for w, sql in WEBSITE_STATUS_SQL.items() if _eval_status(sql, **flags)]
    assert matches == [expected], "exactly one website status must apply, and it must match the scoring presence"


@pytest.mark.parametrize("status, expected", [("open", "open"), ("permanently_closed", "closed"),
                                              ("temporarily_closed", "closed"), ("unknown", "unknown"),
                                              (None, "unknown"), ("something_new", "unknown")])
def test_operating_status_buckets_are_separate(status, expected):
    # Mirror of OPERATING_SQL: open is only 'open'; unknown is never folded into open.
    closed = {"permanently_closed", "temporarily_closed"}
    got = "open" if status == "open" else "closed" if status in closed else "unknown"
    assert got == expected
    assert OPERATING_SQL["open"] == "l.operating_status = 'open'"
    assert "permanently_closed" in OPERATING_SQL["closed"] and "temporarily_closed" in OPERATING_SQL["closed"]
    assert "IS NULL" in OPERATING_SQL["unknown"] and "NOT IN ('open'" in OPERATING_SQL["unknown"]


# ---------------------------------------------------------------- endpoints (stubbed queries, no database)

ROW = {"id": 1, "name": "A", "industry": "cleaning", "county": "duval", "operating_status": "open", "lon": -81.6,
       "lat": 30.3, "bos": 50.0, "lps": 40.0, "stage": "provisional", "evidence_points": 2,
       "qualification": "needs_research", "website_listed": False, "website_status": "none_recorded",
       "has_phone": True, "has_email": False}


@pytest.fixture
def api(monkeypatch):
    calls = []

    def rows(sql, **params):
        calls.append(("rows", sql, params))
        if "GROUP BY 1" in sql:                                   # summary
            return [{"qualification": "needs_research", "n": 7, "with_coordinates": 5},
                    {"qualification": "unscored", "n": 3, "with_coordinates": 3}]
        if "AND l.geom IS NOT NULL" in sql:                       # points
            return [{"id": i, "lon": -81.0, "lat": 30.0, "qualification": "needs_research"} for i in range(3)]
        return [dict(ROW, id=i) for i in range(min(params.get("limit", 2), 2))]

    def scalar(sql, **params):
        calls.append(("scalar", sql, params))
        return 10

    monkeypatch.setattr(business, "_rows", rows)
    monkeypatch.setattr(business, "_scalar", scalar)
    client = TestClient(create_app(SimpleNamespace(diagnostics=None)), base_url="http://127.0.0.1:8765")
    return client, calls


def test_legacy_limit_is_still_honoured(api):
    c, calls = api
    d = c.get("/api/prospects", params={"qualification": "provisionally_qualified,needs_research", "limit": 5000}).json()
    sql, params = calls[0][1], calls[0][2]
    assert params["limit"] == 5000 and params["offset"] == 0
    assert params["f_quals"] == ["provisionally_qualified", "needs_research"]
    assert d["count"] == 2 and d["total"] == 10 and d["limit"] == 5000 and d["truncated"] is True
    assert set(ROW) <= set(d["items"][0]) and "note" in d


def test_legacy_default_limit_is_500(api):
    c, calls = api
    c.get("/api/prospects")
    assert calls[0][2]["limit"] == 500


def test_empty_qualification_means_all_as_before(api):
    c, calls = api
    c.get("/api/prospects", params={"qualification": ""})
    where = calls[0][1].split("WHERE", 1)[1].split("ORDER BY", 1)[0]       # the filter conditions only
    assert "f_quals" not in calls[0][2] and "q.status" not in where


def test_paged_mode(api):
    c, calls = api
    d = c.get("/api/prospects", params={"page": 3, "page_size": 4}).json()
    assert calls[0][2]["limit"] == 4 and calls[0][2]["offset"] == 8
    assert d["page"] == 3 and d["page_size"] == 4 and d["pages"] == 3 and d["has_more"] is False and d["total"] == 10


def test_page_without_size_defaults_to_50(api):
    c, calls = api
    d = c.get("/api/prospects", params={"page": 2}).json()
    assert calls[0][2]["limit"] == 50 and calls[0][2]["offset"] == 50 and d["page_size"] == 50


@pytest.mark.parametrize("params", [{"page_size": 201}, {"page": 0}, {"limit": 5001}, {"limit": -1},
                                    {"website": "none"}, {"operating": "maybe"}, {"sort": "lps; DROP"},
                                    {"bos_min": 70, "bos_max": 10}, {"phone": "perhaps"}])
def test_invalid_parameters_give_422_without_querying(api, params):
    c, calls = api
    assert c.get("/api/prospects", params=params).status_code == 422
    assert calls == []


def test_query_parameters_reach_the_filter(api):
    c, calls = api
    c.get("/api/prospects", params={"county": "duval,clay", "niche": "cleaning", "website": "listed,none_recorded",
                                    "operating": "open,unknown", "phone": "true", "email": "false", "bos_min": 50,
                                    "sort": "bos", "q": "Acme"})
    sql, params = calls[0][1], calls[0][2]
    assert params["f_counties"] == ["duval", "clay"] and params["f_niches"] == ["cleaning"] and params["f_bos_min"] == 50.0
    assert "ORDER BY b.value DESC NULLS LAST, l.id" in sql and params["f_q"] == "%acme%"


def test_summary_reports_mappable_and_unmappable_counts(api):
    c, _ = api
    d = c.get("/api/prospects/summary", params={"county": "duval"}).json()
    assert d == {"total": 10, "with_coordinates": 8, "without_coordinates": 2,
                 "by_qualification": {"needs_research": 7, "unscored": 3}}


def test_points_never_claim_to_equal_the_business_count(api):
    c, _ = api
    d = c.get("/api/prospects/points").json()
    assert d["returned"] == 3 and d["total"] == 10 and d["with_coordinates"] == 8 and d["without_coordinates"] == 2
    assert d["points"][0] == [0, -81.0, 30.0, "needs_research"] and d["truncated"] is False and d["limit"] == MAX_POINTS


def test_points_report_truncation(api, monkeypatch):
    monkeypatch.setattr(business, "MAX_POINTS", 2)
    c, calls = api
    d = c.get("/api/prospects/points").json()
    assert d["returned"] == 2 and d["truncated"] is True and d["limit"] == 2
    assert calls[0][2]["cap"] == 3


def test_summary_and_points_are_not_taken_for_a_location_id(api):
    c, _ = api
    assert c.get("/api/prospects/summary").status_code == 200
    assert c.get("/api/prospects/points").status_code == 200


def test_legacy_limit_zero_still_returns_no_rows_but_reports_the_total(api):
    c, calls = api
    d = c.get("/api/prospects", params={"limit": 0}).json()
    assert calls[0][2]["limit"] == 0 and d["count"] == 0 and d["items"] == [] and d["total"] == 10


def test_export_query_shares_the_filter_and_order():
    from app.reports.prospects import export_query
    f = ProspectFilter(counties=["duval"], website=["listed"], sort="name")
    where, params = f.where()
    sql, p = export_query(f, 123)
    assert f"WHERE {where}" in sql and "ORDER BY lower(l.display_name), l.id" in sql and p["limit"] == 123
    assert {k: v for k, v in p.items() if k.startswith("f_")} == params


def test_export_rejects_invalid_filters_and_limits(api):
    c, calls = api
    assert c.get("/api/prospects/export.csv", params={"website": "none"}).status_code == 422
    assert c.get("/api/prospects/export.csv", params={"limit": 100_001}).status_code == 422
    assert calls == []


def test_legacy_fips_restriction_is_kept_for_the_cli():
    where, params = ProspectFilter(fips=["12031"]).where()
    assert "l.county_fips = ANY(CAST(:f_fips AS text[]))" in where and params["f_fips"] == ["12031"]
