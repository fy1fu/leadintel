"""Sunbiz fixed-width layouts (docs/SUNBIZ.md §4).

Provenance of each layout:
* FIC (2,098 B) and both event files (762 B / 662 B): COBOL copybooks published on the Sunbiz
  server (`Ficreadme.txt`, `COREVENTREADME.txt`); field lengths sum exactly to the record length.
* COR (1,440 B): positions 1-221 and 473-480 confirmed by the official definition excerpts; the
  rest reconstructed and validated on 6,352 real records (officer block starts at field 37 as
  officially stated). Open verification item V1 - every record is re-validated at parse time.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    start: int      # 0-based byte offset
    length: int
    kind: str = "text"   # text | date | num | flag


@dataclass(frozen=True)
class Layout:
    name: str
    record_length: int
    fields: tuple[Field, ...]


def _build(name: str, record_length: int, spec: list[tuple]) -> Layout:
    out, pos = [], 0
    for item in spec:
        n, ln, *k = item
        out.append(Field(n, pos, ln, k[0] if k else "text"))
        pos += ln
    if pos != record_length:
        raise ValueError(f"layout {name}: fields sum to {pos}, expected {record_length}")
    return Layout(name, record_length, tuple(out))


_cor = [("doc", 12), ("name", 192), ("status", 1, "flag"), ("filing_type", 15), ("addr1", 42), ("addr2", 42),
        ("city", 28), ("state", 2), ("zip", 10), ("country", 2), ("mail_addr1", 42), ("mail_addr2", 42),
        ("mail_city", 28), ("mail_state", 2), ("mail_zip", 10), ("mail_country", 2), ("file_date", 8, "date"),
        ("fei", 14), ("more_than_6_officers", 1, "flag"), ("last_txn_date", 8, "date"), ("state_country", 2),
        ("rpt_year1", 4), ("house_flag1", 1, "flag"), ("rpt_date1", 8, "date"), ("rpt_year2", 4),
        ("house_flag2", 1, "flag"), ("rpt_date2", 8, "date"), ("rpt_year3", 4), ("house_flag3", 1, "flag"),
        ("rpt_date3", 8, "date"), ("ra_name", 42), ("ra_name_type", 1), ("ra_addr", 42), ("ra_city", 28),
        ("ra_state", 2), ("ra_zip", 9)]
for i in range(1, 7):
    _cor += [(f"off{i}_title", 4), (f"off{i}_type", 1), (f"off{i}_name", 42), (f"off{i}_addr", 42),
             (f"off{i}_city", 28), (f"off{i}_state", 2), (f"off{i}_zip", 9)]
_cor += [("filler", 4)]
COR = _build("cor", 1440, _cor)

_fic = [("doc", 12), ("name", 192), ("county", 12), ("addr1", 40), ("addr2", 40), ("city", 28), ("state", 2),
        ("zip", 10), ("country", 2), ("file_date", 8, "date"), ("pages", 5, "num"), ("status", 1, "flag"),
        ("cancel_date", 8, "date"), ("expire_date", 8, "date"), ("owner_count", 5, "num"), ("fei", 14),
        ("gt10_owners", 1, "flag")]
for i in range(1, 11):
    _fic += [(f"own{i}_doc", 12), (f"own{i}_name", 55), (f"own{i}_fmt", 1), (f"own{i}_addr", 40),
             (f"own{i}_city", 28), (f"own{i}_state", 2), (f"own{i}_zip", 10), (f"own{i}_country", 2),
             (f"own{i}_fei", 9), (f"own{i}_charter", 12)]
FIC = _build("fic", 2098, _fic)

COR_EVENT = _build("cor_event", 662, [
    ("doc", 12), ("seq", 5, "num"), ("code", 20), ("desc", 40), ("effective_date", 8, "date"),
    ("filed_date", 8, "date"), ("note1", 35), ("note2", 35), ("note3", 35), ("cons_mer_number", 12),
    ("name", 192), ("name_seq", 5, "num"), ("x_name_seq", 5, "num"), ("name_chg", 1), ("x_name_chg", 1),
    ("addr1", 42), ("addr2", 42), ("city", 28), ("state", 2), ("zip", 10), ("mail_addr1", 42), ("mail_addr2", 42),
    ("mail_city", 28), ("mail_state", 2), ("mail_zip", 10)])

FIC_EVENT = _build("fic_event", 762, [
    ("doc", 12), ("orig_doc", 12), ("name", 192), ("action_ctr", 5, "num"), ("seq", 5, "num"), ("pages", 5, "num"),
    ("event_date", 8, "date"), ("action_seq", 5, "num"), ("action_code", 3), ("action_verbage", 70),
    ("old_fei", 9), ("old_county", 12), ("old_addr1", 40), ("old_addr2", 40), ("old_city", 28), ("old_state", 2),
    ("old_zip5", 5), ("old_zip4", 4), ("old_country", 2), ("new_fei", 9), ("new_county", 12), ("new_addr1", 40),
    ("new_addr2", 40), ("new_city", 28), ("new_state", 2), ("new_zip5", 5), ("new_zip4", 4), ("new_country", 2),
    ("own_name", 55), ("own_address", 40), ("own_city", 28), ("own_state", 2), ("own_zip5", 5), ("own_fei", 9),
    ("own_charter", 12), ("old_name_seq", 5, "num"), ("new_name_seq", 5, "num")])

LAYOUTS = {"cor": COR, "fic": FIC, "cor_event": COR_EVENT, "fic_event": FIC_EVENT}

# Filing-type code -> filing kind (raw code is always kept as well)
FILING_KIND = {
    "FLAL": "llc", "FORL": "foreign_llc", "DOMP": "corporation", "FORP": "foreign_corporation",
    "DOMNP": "nonprofit", "FORNP": "foreign_nonprofit", "DOMLP": "lp", "FORLP": "foreign_lp",
    "DOMLLP": "llp", "FORLLP": "foreign_llp", "TRUST": "trust", "AGENT": "agent",
}
