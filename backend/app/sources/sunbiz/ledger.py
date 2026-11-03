"""Sunbiz date ledger (docs/SUNBIZ.md §5): one state per (file kind, day).

States:
  verified            file downloaded, size-checked and imported
  not_yet_published   no file yet and no later file exists
  absent_holiday      no file; Florida state holiday
  absent_nonworkday   no file; weekend (files are produced on work days, sometimes also on weekends)
  absent_unexplained  no file although later files exist -> discovery coverage is PARTIAL
  failed              download or validation failed -> coverage PARTIAL
"""
from __future__ import annotations

from datetime import date, timedelta

PARTIAL_STATES = {"absent_unexplained", "failed"}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def florida_state_holidays(year: int) -> set[date]:
    """Paid holidays for Florida state employees (s. 110.117, F.S.), with weekend observance."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return {
        _observed(date(year, 1, 1)), _nth_weekday(year, 1, 0, 3), _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)), _nth_weekday(year, 9, 0, 1), _observed(date(year, 11, 11)),
        thanksgiving, thanksgiving + timedelta(days=1), _observed(date(year, 12, 25)),
    }


def classify_days(start: date, end: date, present: set[date], failed: set[date] | None = None) -> dict[date, str]:
    failed = failed or set()
    latest = max(present) if present else None
    holidays = set().union(*(florida_state_holidays(y) for y in range(start.year, end.year + 1)))
    out = {}
    d = start
    while d <= end:
        if d in failed:
            out[d] = "failed"
        elif d in present:
            out[d] = "verified"
        elif latest is None or d > latest:
            out[d] = "not_yet_published"
        elif d in holidays:
            out[d] = "absent_holiday"
        elif d.weekday() >= 5:
            out[d] = "absent_nonworkday"
        else:
            out[d] = "absent_unexplained"
        d += timedelta(days=1)
    return out
