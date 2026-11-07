"""Website Quality Score (WQS), SCORING.md §5 - versioned, explainable, provisional until calibrated.

    criterion c ∈ {0, 1} (passed)           measured weight 1.0, heuristic weight 0.5
    dimension D_k = Σ w·c / Σ w             over the ASSESSED criteria of dimension k
    WQS = 100 · Σ W_k·D_k / Σ W_k            over dimensions with at least one assessed criterion
    confidence = Σ_k W_k · (assessed weight_k / total weight_k) / Σ_k W_k     (all seven dimensions)

Unknown / not-assessed criteria are excluded and lower the confidence; they are never scored as 0. The dimension
weights are starting points (ROADMAP F11), not validated: WQS stays "provisional" until the calibration workflow
(app/scoring/calibration.py) shows Spearman ρ ≥ 0.7 against 60-100 human-labelled websites.
"""
from __future__ import annotations

WQS_VERSION = "wqs-1.0"
DIMENSION_WEIGHTS = {"visual": 0.15, "ux": 0.20, "effectiveness": 0.20, "technical": 0.15, "content": 0.10,
                     "seo": 0.10, "modernity": 0.10}
DIMENSION_LABELS = {"visual": "Visual design", "ux": "Usability and accessibility", "effectiveness": "Getting customers to call or write",
                    "technical": "Technical health", "content": "Content", "seo": "Findability in search", "modernity": "Up to date"}
CALIBRATION_STATUS = "provisional"
CALIBRATION_NOTE = ("Provisional: the weights are starting values that have not yet been checked against human ratings "
                    "of real websites (target: rank correlation of at least 0.7 on 60-100 rated sites).")
LOW_CONFIDENCE = 0.5
BANDS = [(90, "excellent"), (80, "very good"), (70, "good"), (60, "average"), (40, "below average"), (20, "poor"), (0, "severely deficient")]


def band(value: float | None) -> str | None:
    if value is None:
        return None
    return next(label for limit, label in BANDS if value >= limit)


def compute(criteria) -> dict:
    """criteria: objects with key, dimension, passed (True/False/None), weight, evidence."""
    dims = {}
    for d, W in DIMENSION_WEIGHTS.items():
        cs = [c for c in criteria if c.dimension == d]
        assessed = [c for c in cs if c.passed is not None]
        aw = sum(c.weight for c in assessed)
        tw = sum(c.weight for c in cs)
        score = (sum(c.weight for c in assessed if c.passed) / aw) if aw else None
        good = [c.key for c in assessed if c.passed]
        bad = [c.key for c in assessed if not c.passed]
        na = [c.key for c in cs if c.passed is None]
        label = DIMENSION_LABELS[d]
        if score is None:
            expl = f"{label}: not assessed" + (f" ({len(na)} check{'s' if len(na) != 1 else ''} could not be made)" if na else "")
        else:
            expl = f"{label}: {len(good)} of {len(assessed)} checks passed" + (f"; {len(na)} not assessed" if na else "")
        dims[d] = {"label": label, "weight": W, "score": None if score is None else round(100 * score, 1),
                   "coverage": round(aw / tw, 3) if tw else 0.0, "passed": good, "failed": bad, "not_assessed": na,
                   "explanation": expl}
    scored = {d: v for d, v in dims.items() if v["score"] is not None}
    total_w = sum(DIMENSION_WEIGHTS.values())
    confidence = round(sum(DIMENSION_WEIGHTS[d] * v["coverage"] for d, v in dims.items()) / total_w, 3)
    if not scored:
        return {"version": WQS_VERSION, "value": None, "band": None, "confidence": confidence, "status": "not assessed",
                "calibration": CALIBRATION_STATUS, "dimensions": dims, "contributions": {},
                "explanation": ["Not enough could be checked on this website to give a quality score."]}
    sw = sum(DIMENSION_WEIGHTS[d] for d in scored)
    contributions = {d: round(DIMENSION_WEIGHTS[d] * v["score"] / sw, 2) for d, v in scored.items()}
    value = round(sum(contributions.values()), 1)
    strongest = max(scored, key=lambda d: scored[d]["score"])
    weakest = min(scored, key=lambda d: scored[d]["score"])
    explanation = [f"Website quality {value:.0f}/100 ({band(value)}), based on {len(scored)} of 7 areas."]
    if scored[weakest]["score"] < scored[strongest]["score"]:
        explanation.append(f"Strongest: {DIMENSION_LABELS[strongest].lower()} ({scored[strongest]['score']:.0f}); "
                           f"weakest: {DIMENSION_LABELS[weakest].lower()} ({scored[weakest]['score']:.0f}).")
    not_scored = [DIMENSION_LABELS[d].lower() for d in dims if d not in scored]
    if not_scored:
        explanation.append("Not assessed (does not lower the score): " + ", ".join(not_scored) + ".")
    status = "low confidence" if confidence < LOW_CONFIDENCE else CALIBRATION_STATUS
    if confidence < LOW_CONFIDENCE:
        explanation.append(f"Low confidence ({confidence:.0%} of the checks could be made): treat the score as a rough indication.")
    explanation.append(CALIBRATION_NOTE)
    return {"version": WQS_VERSION, "value": value, "band": band(value), "confidence": confidence, "status": status,
            "calibration": CALIBRATION_STATUS, "dimensions": dims, "contributions": contributions, "explanation": explanation}
