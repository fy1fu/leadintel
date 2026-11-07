"""WQS calibration workflow (M3; research/M3_PREP.md §4). Nothing here invents labels.

1. ``export_sheet`` writes a CSV with one row per audited website (latest scored audit), with empty columns for a
   human rating (overall and per dimension, 1-5). Rate 60-100 real websites; label about 20 of them twice
   (a second row with the same audit_id and another ``labeler``) to measure consistency.
2. ``report`` reads the filled sheet and computes the Spearman rank correlation between WQS and the human overall
   rating, the agreement on the decision bands (< 60 / 60-69 / >= 70) and the double-label consistency.

WQS may be described as validated only when at least ``MIN_SITES`` sites are labelled, Spearman ρ >= ``RHO_TARGET``
and band agreement >= ``BAND_TARGET``. Until then it is provisional, and the app says so.
"""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import text

from app.scoring.wqs import DIMENSION_WEIGHTS, WQS_VERSION

MIN_SITES, RHO_TARGET, BAND_TARGET = 60, 0.7, 0.8
HUMAN = ["human_overall_1_5"] + [f"human_{d}_1_5" for d in DIMENSION_WEIGHTS]
COLUMNS = ["audit_id", "location_id", "business", "website", "audited_at", "wqs", "wqs_version", "confidence",
           *HUMAN, "labeler", "notes"]


def export_sheet(conn, path: Path, limit: int = 200) -> int:
    rows = conn.execute(text(
        "SELECT DISTINCT ON (a.location_id) a.id, a.location_id, l.display_name, a.start_url, a.started_at, a.wqs, "
        "a.wqs_version, a.wqs_confidence FROM website_audit a JOIN location l ON l.id = a.location_id "
        "WHERE a.wqs IS NOT NULL ORDER BY a.location_id, a.started_at DESC LIMIT :n"), {"n": limit}).all()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r[0], r[1], r[2], r[3], r[4].date().isoformat(), r[5], r[6], r[7]] + [""] * len(HUMAN) + ["", ""])
    return len(rows)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1          # average rank for ties
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx, vy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    return None if vx == 0 or vy == 0 else cov / (vx * vy) ** 0.5


def wqs_band(v: float) -> int:
    return 0 if v < 60 else (1 if v < 70 else 2)


def human_band(r: int) -> int:
    return 0 if r <= 2 else (1 if r == 3 else 2)


def report(labels_path: Path) -> dict:
    with open(labels_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    by_audit: dict[str, list[dict]] = {}
    for r in rows:
        try:
            h = int(r.get("human_overall_1_5", "").strip())
            wqs = float(r.get("wqs", "").strip())
        except ValueError:
            continue
        if 1 <= h <= 5:
            by_audit.setdefault(r["audit_id"], []).append({"h": h, "wqs": wqs, "labeler": r.get("labeler", "")})
    sites = [(v[0]["wqs"], sum(x["h"] for x in v) / len(v)) for v in by_audit.values()]
    rho = spearman([s[0] for s in sites], [s[1] for s in sites])
    band = (sum(1 for w, h in sites if wqs_band(w) == human_band(round(h))) / len(sites)) if sites else None
    doubles = [v for v in by_audit.values() if len(v) >= 2]
    agree = (sum(1 for v in doubles if v[0]["h"] == v[1]["h"]) / len(doubles)) if doubles else None
    reasons = []
    if len(sites) < MIN_SITES:
        reasons.append(f"only {len(sites)} labelled sites (at least {MIN_SITES} needed)")
    if rho is None or rho < RHO_TARGET:
        reasons.append(f"Spearman rho {rho:.2f} is below {RHO_TARGET}" if rho is not None else "rank correlation not computable")
    if band is None or band < BAND_TARGET:
        reasons.append(f"band agreement {band:.0%} is below {BAND_TARGET:.0%}" if band is not None else "no band agreement")
    return {"wqs_version": WQS_VERSION, "labelled_sites": len(sites), "spearman_rho": None if rho is None else round(rho, 3),
            "band_agreement": None if band is None else round(band, 3), "double_labelled": len(doubles),
            "double_label_exact_agreement": None if agree is None else round(agree, 3),
            "status": "validated" if not reasons else "provisional", "reasons": reasons}
