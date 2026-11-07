"""Build synthetic Sunbiz fixed-width records from the layouts (no real personal data)."""
from datetime import date

from app.sources.sunbiz.layouts import LAYOUTS


def record(kind: str, **values) -> bytes:
    layout = LAYOUTS[kind]
    buf = bytearray(b" " * layout.record_length)
    for f in layout.fields:
        if f.name == "filler":
            buf[f.start:f.start + f.length] = b"\x00" * f.length
            continue
        v = values.get(f.name)
        if v is None:
            continue
        if isinstance(v, date):
            v = v.strftime("%m%d%Y")
        b = str(v).encode("utf-8")[: f.length]
        buf[f.start:f.start + len(b)] = b
    return bytes(buf)


def cor(doc, name, *, addr="100 MAIN ST", zip_="32202", ra_addr="200 AGENT WAY", ra_zip="32202", filed=date(2026, 10, 1),
        ftype="FLAL", status="A", officers=(("MGR", "TEST PERSON A"),)):
    vals = dict(doc=doc, name=name, status=status, filing_type=ftype, addr1=addr, city="JACKSONVILLE", zip=zip_,
                mail_addr1=addr, mail_city="JACKSONVILLE", mail_state="FL", mail_zip=zip_, file_date=filed,
                ra_name="TEST AGENT", ra_addr=ra_addr, ra_city="JACKSONVILLE", ra_state="FL", ra_zip=ra_zip)
    for i, (title, nm) in enumerate(officers, 1):
        vals[f"off{i}_title"], vals[f"off{i}_type"], vals[f"off{i}_name"] = title, "P", nm
    return record("cor", **vals)


def fic(doc, name, *, owner_charter=None, owner_name="TEST OWNER LLC", addr="300 TRADE ST", zip_="32207",
        county="DUVAL", filed=date(2026, 10, 2)):
    vals = dict(doc=doc, name=name, county=county, addr1=addr, city="JACKSONVILLE", state="FL", zip=zip_, country="US",
                file_date=filed, pages="00001", status="A", expire_date=date(2031, 12, 31), owner_count="00001",
                gt10_owners="N", own1_doc=doc, own1_name=owner_name, own1_fmt="C" if owner_charter else "P")
    if owner_charter:
        vals["own1_charter"] = owner_charter
    return record("fic", **vals)
