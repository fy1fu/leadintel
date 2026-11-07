"""WQS (M3): versioned, explainable, unknown is never poor, provisional until calibrated; recommendations from
actual failed checks only; the calibration workflow (Spearman, band agreement, provisional status). Offline."""
import csv
import json

import pytest

from app import cli
from app.audit import recommend
from app.crawl.checks import Criterion
from app.scoring import calibration, wqs


def C(key, dim, passed, kind="measured", evidence="e"):
    return Criterion(key, dim, kind, passed, evidence, "test", 1.0 if kind == "measured" else 0.5)


def test_version_weights_and_dimensions():
    assert wqs.WQS_VERSION == "wqs-1.0"
    assert set(wqs.DIMENSION_WEIGHTS) == {"visual", "ux", "effectiveness", "technical", "content", "seo", "modernity"}
    assert abs(sum(wqs.DIMENSION_WEIGHTS.values()) - 1.0) < 1e-9
    assert set(wqs.DIMENSION_LABELS) == set(wqs.DIMENSION_WEIGHTS)


def test_unknown_is_never_poor():
    known = [C("https", "technical", True), C("click_to_call", "effectiveness", True)]
    a = wqs.compute(known)
    b = wqs.compute(known + [C("visual_design", "visual", None), C("jquery_current", "modernity", None),
                             C("load_time", "technical", None, "heuristic")])
    assert a["value"] == b["value"] == 100.0                 # not-assessed criteria never lower the score
    assert b["confidence"] < 1 and b["dimensions"]["visual"]["score"] is None
    assert "visual_design" in b["dimensions"]["visual"]["not_assessed"]
    assert any("does not lower the score" in line for line in b["explanation"])


def test_nothing_assessed_gives_no_score():
    r = wqs.compute([C("visual_design", "visual", None)])
    assert r["value"] is None and r["status"] == "not assessed" and r["band"] is None


def test_measured_counts_twice_as_much_as_heuristic_and_contributions_add_up():
    r = wqs.compute([C("a", "content", True, "measured"), C("b", "content", False, "heuristic"),
                     C("c", "seo", False), C("d", "ux", True)])
    assert r["dimensions"]["content"]["score"] == pytest.approx(100 * 1.0 / 1.5, abs=0.1)
    assert sum(r["contributions"].values()) == pytest.approx(r["value"], abs=0.1)
    assert r["band"] == wqs.band(r["value"])
    assert r["calibration"] == "provisional" and wqs.CALIBRATION_NOTE in r["explanation"]


def test_low_coverage_is_flagged():
    r = wqs.compute([C("a", "seo", True)])
    assert r["confidence"] < wqs.LOW_CONFIDENCE and r["status"] == "low confidence"


def test_bands():
    assert [wqs.band(v) for v in (95, 85, 75, 65, 45, 25, 5)] == [
        "excellent", "very good", "good", "average", "below average", "poor", "severely deficient"]


# ---------------------------------------------------------------- recommendations
def test_recommendations_come_only_from_failed_checks_in_severity_order():
    crit = [C("https", "technical", False, evidence="home page served over http"),
            C("titles_unique", "seo", False), C("meta_description", "seo", False),
            C("click_to_call", "effectiveness", False, evidence="tel: links on 0 pages"),
            C("viewport_meta", "ux", None), C("page_language", "ux", True), C("unknown_check", "seo", False),
            C("enough_text", "content", False, "heuristic"), C("image_alt_text", "ux", False)]
    recs = recommend.build(crit, limit=5)
    keys = [r["criterion"] for r in recs]
    assert keys[:2] == ["click_to_call", "https"]           # high first, then by dimension weight
    assert "viewport_meta" not in keys and "page_language" not in keys and "unknown_check" not in keys
    assert len(recs) == 5 and [r["priority"] for r in recs] == [1, 2, 3, 4, 5]
    assert {r["severity"] for r in recs} <= {"high", "medium", "low"}
    https = next(r for r in recs if r["criterion"] == "https")
    assert https["evidence"] == "home page served over http" and https["benchmark"] is None
    for r in recs:
        assert all(r[k] for k in ("title", "finding", "why", "suggestion"))
        text = " ".join(str(v) for v in r.values()).lower()
        assert not any(w in text for w in ("revenue", "% more", "guarantee", "increase sales"))


def test_recommendation_benchmark_sentence_uses_peer_pass_counts():
    bench = {"description": "lawn care businesses in Duval County", "criteria": {"https": {"passed": 7, "assessed": 9}}}
    r = recommend.build([C("https", "technical", False)], bench)[0]
    assert r["benchmark"] == "7 of 9 comparable businesses (lawn care businesses in Duval County) pass this check."
    few = {**bench, "criteria": {"https": {"passed": 1, "assessed": 1}}}            # too few peers assessed this check
    assert recommend.build([C("https", "technical", False)], few)[0]["benchmark"] is None


def test_every_template_criterion_exists_in_the_checks():
    import inspect

    from app.crawl import checks
    src = inspect.getsource(checks.evaluate_site)
    for key in recommend.TEMPLATES:
        assert f'"{key}"' in src, key


# ---------------------------------------------------------------- calibration
def test_spearman():
    assert calibration.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert calibration.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert calibration.spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9487, abs=1e-3)   # ties averaged
    assert calibration.spearman([1, 2], [1, 2]) is None
    assert calibration.spearman([1, 1, 1], [1, 2, 3]) is None


def _sheet(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=calibration.COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in calibration.COLUMNS})


def test_report_stays_provisional_with_too_few_sites(tmp_path):
    rows = [{"audit_id": i, "wqs": 20 * i - 10, "human_overall_1_5": i, "labeler": "a"} for i in range(1, 6)]
    rows += [{"audit_id": 1, "wqs": 10, "human_overall_1_5": 1, "labeler": "b"},
             {"audit_id": 2, "wqs": 30, "human_overall_1_5": 3, "labeler": "b"},
             {"audit_id": 3, "wqs": 50, "human_overall_1_5": "", "labeler": "b"},      # unlabelled row ignored
             {"audit_id": 4, "wqs": 70, "human_overall_1_5": "9", "labeler": "b"}]      # out of range ignored
    _sheet(tmp_path / "l.csv", rows)
    r = calibration.report(tmp_path / "l.csv")
    assert r["labelled_sites"] == 5 and r["spearman_rho"] >= 0.9 and r["double_labelled"] == 2
    assert r["double_label_exact_agreement"] == 0.5
    assert r["status"] == "provisional" and any("60" in why for why in r["reasons"])


def test_report_validates_only_when_every_target_is_met(tmp_path):
    rows = []
    for i in range(70):
        h = 1 + i * 5 // 70
        rows.append({"audit_id": i, "wqs": {1: 20, 2: 45, 3: 65, 4: 80, 5: 92}[h] + (i % 3), "human_overall_1_5": h})
    _sheet(tmp_path / "l.csv", rows)
    r = calibration.report(tmp_path / "l.csv")
    assert r["status"] == "validated" and r["reasons"] == [] and r["band_agreement"] >= 0.8


def test_calibration_cli_report(tmp_path, capsys):
    _sheet(tmp_path / "l.csv", [{"audit_id": 1, "wqs": 50, "human_overall_1_5": 3}])
    assert cli.main(["wqs-calibration", "report", "--labels", str(tmp_path / "l.csv")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "provisional" and out["wqs_version"] == wqs.WQS_VERSION
    assert cli.main(["wqs-calibration", "report"]) == 2
