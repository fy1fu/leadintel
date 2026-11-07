"""Phrase-aware keyword matching (docs/SCORING.md §2).

* Token-based: a phrase matches only as a whole-word token sequence (never raw substring).
* Positive groups allow inflections (wash/washing, detail/detailing) and `*` prefix wildcards;
  negative groups match exact tokens only ("Trusted" does not hit "trust", "Capitalize" not "capital").
* Negative penalties never exclude; `neutralized_by` / `neutralized_by_any_positive_group` remove them.
* `requires_other_group` groups (e.g. "mobile") only count together with another positive group.
* Every hit is returned with term, matched text and field so the profile can show what fired.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_SUFFIXES = ("", "s", "es", "ing", "er", "ers", "ed")


def tokens(text: str | None) -> list[str]:
    if not text:
        return []
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = s.replace("_", " ")
    return re.findall(r"[a-z0-9]+", s)


def _token_matches(pattern: str, tok: str, inflect: bool) -> bool:
    if pattern.endswith("*"):
        return tok.startswith(pattern[:-1])
    if not inflect:
        return tok == pattern
    if tok == pattern:
        return True
    stem = pattern[:-1] if pattern.endswith("e") else pattern
    return any(tok == pattern + suf or tok == stem + suf for suf in _SUFFIXES[1:])


def find_phrase(toks: list[str], phrase: str, inflect: bool) -> str | None:
    ptoks = phrase.lower().split()
    n = len(ptoks)
    for i in range(len(toks) - n + 1):
        if all(_token_matches(p, t, inflect) for p, t in zip(ptoks, toks[i:i + n])):
            return " ".join(toks[i:i + n])
    return None


@dataclass
class Hit:
    group: str
    polarity: str
    term: str
    matched_text: str
    field: str
    effect: float
    neutralized: bool = False


def match(fields: dict[str, str | None], cfg: dict) -> tuple[list[Hit], float]:
    """fields: field name -> text. Returns all hits (incl. neutralized) and the total effect.

    Each group counts at most once (its strongest phrase), so repeating a word can't stack points.
    """
    toks = {f: tokens(v) for f, v in fields.items()}
    inflect_pos = cfg.get("match", {}).get("variants", "positive_only") in ("positive_only", True, "all")
    hits: list[Hit] = []
    pos_groups: set[str] = set()
    for g, spec in cfg.get("groups", {}).items():
        for phrase in spec["phrases"]:
            found = next(((f, m) for f, t in toks.items() if (m := find_phrase(t, phrase, inflect_pos))), None)
            if found:
                hits.append(Hit(g, "positive", phrase, found[1], found[0], float(spec["weight"])))
                pos_groups.add(g)
                break
    for h in hits:  # weak groups only count alongside another positive group
        if cfg["groups"][h.group].get("requires_other_group") and not (pos_groups - {h.group}):
            h.neutralized = True
    real_pos = {h.group for h in hits if not h.neutralized}
    all_toks = [t for ts in toks.values() for t in ts]
    for g, spec in cfg.get("negative", {}).items():
        for phrase in spec["phrases"]:
            found = next(((f, m) for f, t in toks.items() if (m := find_phrase(t, phrase, False))), None)
            if not found:
                continue
            neutral = bool(spec.get("neutralized_by_any_positive_group") and real_pos) or any(
                find_phrase(all_toks, w, True) for w in spec.get("neutralized_by", []))
            hits.append(Hit(g, "negative", phrase, found[1], found[0], float(spec["penalty"]), neutral))
            break
    total = sum(h.effect for h in hits if not h.neutralized)
    return hits, total


def hard_excluded(fields: dict[str, str | None], cfg: dict) -> str | None:
    """Returns the recorded reason when the user hard-excluded a matching phrase."""
    toks = [t for v in fields.values() for t in tokens(v)]
    for entry in cfg.get("hard_exclude", []) or []:
        phrase = entry["phrase"] if isinstance(entry, dict) else str(entry)
        if find_phrase(toks, phrase, False):
            return entry.get("reason", f"hard-excluded phrase '{phrase}'") if isinstance(entry, dict) else f"hard-excluded phrase '{phrase}'"
    return None
