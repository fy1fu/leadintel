"""Fault-tolerant Sunbiz fixed-width parser (PHASE1.md item 1.4).

* Fields are sliced by **byte** position and decoded per field (UTF-8; cp1252 fallback is flagged).
* NUL padding is stripped; dates are MMDDYYYY (blank / all-zero -> None).
* A record with the wrong length, an undecodable or control-character field, an invalid date or an
  invalid document number goes to quarantine with a reason; the rest of the file still parses.
* The parser never raises on bad data - only on programming errors.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator

from app.sources.sunbiz.layouts import Layout

_DOC = re.compile(r"^[A-Z0-9]{6,12}$")
_CTRL = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class Parsed:
    line_no: int
    raw: bytes
    data: dict
    warnings: list[str] = field(default_factory=list)


@dataclass
class Rejected:
    line_no: int
    raw: bytes
    reason: str
    field: str | None = None


def parse_date(v: str) -> date | None:
    v = v.strip()
    if not v or set(v) <= {"0"}:
        return None
    if not re.fullmatch(r"\d{8}", v):
        raise ValueError(f"not MMDDYYYY: {v!r}")
    return date(int(v[4:8]), int(v[0:2]), int(v[2:4]))


def _decode(b: bytes) -> tuple[str, bool]:
    b = b.replace(b"\x00", b" ")
    try:
        return b.decode("utf-8"), False
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace"), True


def parse_line(raw: bytes, layout: Layout, line_no: int) -> Parsed | Rejected:
    line = raw.rstrip(b"\r")
    if len(line) != layout.record_length:
        return Rejected(line_no, raw, f"record length {len(line)} != {layout.record_length}")
    out, warnings = {}, []
    for f in layout.fields:
        if f.name == "filler":
            continue
        text, fallback = _decode(line[f.start:f.start + f.length])
        if fallback:
            warnings.append(f"{f.name}: not valid UTF-8, decoded as cp1252")
        if _CTRL.search(text):
            return Rejected(line_no, raw, "control characters in field", f.name)
        text = text.strip()
        if f.kind == "date":
            try:
                out[f.name] = parse_date(text)
            except ValueError as exc:
                return Rejected(line_no, raw, f"invalid date: {exc}", f.name)
        elif f.kind == "num":
            if text and not text.isdigit():
                return Rejected(line_no, raw, f"non-numeric value {text!r}", f.name)
            out[f.name] = int(text) if text else None
        else:
            out[f.name] = text or None
    if not out.get("doc") or not _DOC.match(out["doc"]):
        return Rejected(line_no, raw, f"invalid document number {out.get('doc')!r}", "doc")
    return Parsed(line_no, raw, out, warnings)


def parse_bytes(blob: bytes, layout: Layout) -> Iterator[Parsed | Rejected]:
    """Parse a whole file. A truncated final line (no newline, short) is rejected, not guessed."""
    lines = blob.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    for i, raw in enumerate(lines, start=1):
        if not raw.strip(b"\r\x00 "):
            continue
        yield parse_line(raw, layout, i)
