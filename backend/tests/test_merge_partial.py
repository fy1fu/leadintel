"""Fix 4: merge / unmerge when two locations share SOME but not all relationships.

Survivor A and merged B share a phone, a profile, a dbpr source link, an alias, a fact and an address;
each also has relationships of its own. A third location C shares an email with B but takes no part.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import (Address, ContactPoint, Fact, Location, LocationAlias, LocationContact, LocationSourceLink,
                           MergeEvent, OnlineProfile)
from app.resolve.merge import MergeError, merge, unmerge

# table -> owner column (and owner_type filter where the table is polymorphic)
TABLES = {"location_source_link": "location_id", "location_alias": "location_id", "online_profile": "location_id",
          "location_contact": "location_id", "location_organization": "location_id", "fact": "subject_id",
          "address": "owner_id"}
POLY = {"fact": "subject_type = 'location'", "address": "owner_type = 'location'"}


def snapshot(s):
    """Every relationship row as (table, id) -> owner location."""
    out = {}
    for t, col in TABLES.items():
        where = f"WHERE {POLY[t]}" if t in POLY else ""
        for rid, owner in s.execute(text(f"SELECT id, {col} FROM {t} {where}")):
            out[(t, rid)] = owner
    return out


def _cp(s, kind, v):
    cp = s.execute(text("SELECT id FROM contact_point WHERE kind=:k AND value_norm=:v"), {"k": kind, "v": v}).scalar()
    if cp is None:
        c = ContactPoint(kind=kind, value_norm=v, display=v)
        s.add(c)
        s.flush()
        cp = c.id
    return cp


def build(s):
    locs = {}
    for k in "ABC":
        l = Location(display_name=f"Acme {k}", name_norm=f"acme {k.lower()}", county_fips="12031", operating_status="open",
                     geom="SRID=4326;POINT(-81.65 30.33)")
        s.add(l)
        s.flush()
        locs[k] = l.id
    A, B, C = locs["A"], locs["B"], locs["C"]

    def contact(l, kind, v):
        s.add(LocationContact(location_id=l, contact_point_id=_cp(s, kind, v), source_key="overture"))

    def profile(l, url):
        s.add(OnlineProfile(location_id=l, url=f"https://{url}", url_norm=url, platform="facebook", platform_kind="social",
                            source_key="overture"))

    def link(l, src, ext):
        s.add(LocationSourceLink(location_id=l, source_key=src, external_id=ext, match_method="t"))

    def alias(l, a):
        s.add(LocationAlias(location_id=l, alias=a, alias_norm=a, kind="listing"))

    def fact(l, field, v):
        s.add(Fact(subject_type="location", subject_id=l, field=field, value={"v": v}, value_norm=v, source_key="overture",
                   provenance="directory"))

    def addr(l, key):
        s.add(Address(owner_type="location", owner_id=l, role="listing", line1=key, zip5="32207", norm_key=key,
                      county_fips="12031", is_operating_location=True))

    # shared by A and B
    for l in (A, B):
        contact(l, "phone", "+19045550001")
        profile(l, "facebook.com/acme")
        link(l, "dbpr", "CBC1")
        alias(l, "acme")
        fact(l, "phone", "+19045550001")
        addr(l, "1 main st 32207")
    # unique to A
    contact(A, "phone", "+19045550003"); profile(A, "instagram.com/acme_a"); link(A, "overture", "ovA")
    alias(A, "acme a"); fact(A, "name", "Acme A"); addr(A, "3 side st 32207")
    # unique to B (relative to A); the email is also on C, which is not part of the merge
    contact(B, "phone", "+19045550002"); contact(B, "email", "info@acme.test"); profile(B, "instagram.com/acme_b")
    link(B, "overture", "ovB"); alias(B, "acme b"); fact(B, "name", "Acme B"); addr(B, "2 back st 32207")
    contact(C, "email", "info@acme.test"); link(C, "overture", "ovC")
    s.flush()
    return A, B, C


SHARED = {  # B's rows that the survivor already has an equivalent of
    "location_contact": "contact_point_id IN (SELECT id FROM contact_point WHERE value_norm = '+19045550001')",
    "online_profile": "url_norm = 'facebook.com/acme'",
    "location_source_link": "source_key = 'dbpr'",
    "location_alias": "alias_norm = 'acme'",
    "fact": "field = 'phone'",
    "address": "norm_key = '1 main st 32207'",
}


def ids(s, table, owner, cond="true"):
    col = TABLES[table]
    poly = f" AND {POLY[table]}" if table in POLY else ""
    return set(s.execute(text(f"SELECT id FROM {table} WHERE {col} = :o{poly} AND {cond}"), {"o": owner}).scalars())


def assert_no_duplicates(s):
    for sql in ("SELECT location_id, source_key, external_id FROM location_source_link",
                "SELECT location_id, contact_point_id, source_key FROM location_contact",
                "SELECT location_id, url_norm FROM online_profile",
                "SELECT location_id, alias_norm, kind FROM location_alias",
                "SELECT subject_id, field, value_norm, source_record_id FROM fact WHERE subject_type = 'location'",
                "SELECT owner_id, role, norm_key FROM address WHERE owner_type = 'location'"):
        dup = s.execute(text(f"SELECT count(*) FROM ({sql}) x GROUP BY x.* HAVING count(*) > 1")).first()
        assert dup is None, sql


def test_partial_sharing_merge_moves_only_unique_rows_records_everything_and_unmerge_restores(db):
    with Session(db) as s:
        A, B, C = build(s)
        before = snapshot(s)
        b_rows = {t: ids(s, t, B) for t in TABLES}
        b_shared = {t: ids(s, t, B, SHARED[t]) for t in SHARED}
        c_before = {k: v for k, v in before.items() if v == C}

        ev = merge(s, A, B, method="manual", evidence={"test": True})
        after = snapshot(s)
        assert len(after) == len(before)                                    # nothing created or deleted
        assert_no_duplicates(s)
        # shared rows stay on B; every other B row moved to A; A's own and C's rows untouched
        for t, rows in b_rows.items():
            shared = b_shared.get(t, set())
            assert ids(s, t, B) == shared, t
            assert all(after[(t, r)] == A for r in rows - shared), t
        assert all(after[k] == A for k, v in before.items() if v == A)
        assert {k: v for k, v in after.items() if v == C} == c_before
        # the email shared with C (not with A) moved to A; C keeps its own copy
        assert s.execute(text("SELECT count(*) FROM location_contact lc JOIN contact_point cp ON cp.id = lc.contact_point_id "
                              "WHERE cp.value_norm = 'info@acme.test' AND lc.location_id = ANY(:l)"),
                         {"l": [A, C]}).scalar() == 2
        # history records EVERY affected B relationship: moved, or kept with the survivor row it duplicates
        kept = ev.moved["_kept"]
        for t, rows in b_rows.items():
            moved = set(ev.moved.get(t, []))
            kept_ids = {m for m, _ in kept.get(t, [])}
            assert moved | kept_ids == rows and not (moved & kept_ids), t
            for m, a_eq in kept.get(t, []):
                assert after[(t, a_eq)] == A
        assert sum(len(v) for v in kept.values()) == len(SHARED)

        back = unmerge(s, ev.id)
        assert snapshot(s) == before                                        # exact restoration
        assert s.get(Location, B).merged_into_id is None
        assert back.moved["restored"] == {t: v for t, v in ev.moved.items() if not t.startswith("_")}
        assert_no_duplicates(s)
        s.commit()


def test_repeated_merge_and_unmerge_neither_duplicate_nor_lose_relationships(db):
    with Session(db) as s:
        A, B, C = build(s)
        before = snapshot(s)
        ev1 = merge(s, A, B, method="manual", evidence={})
        merged_state = snapshot(s)
        with pytest.raises(MergeError):
            merge(s, A, B, method="manual", evidence={})                    # already merged
        assert snapshot(s) == merged_state
        unmerge(s, ev1.id)
        with pytest.raises(MergeError):
            unmerge(s, ev1.id)                                              # already reverted
        assert snapshot(s) == before
        ev2 = merge(s, A, B, method="manual", evidence={})                  # merge again after unmerge
        assert snapshot(s) == merged_state
        assert {t: sorted(v) for t, v in ev2.moved.items() if not t.startswith("_")} == \
               {t: sorted(v) for t, v in ev1.moved.items() if not t.startswith("_")}
        assert_no_duplicates(s)
        unmerge(s, ev2.id)
        assert snapshot(s) == before
        assert s.execute(text("SELECT count(*) FROM merge_event")).scalar() == 4
        s.commit()


def test_older_merge_events_without_kept_record_still_unmerge(db):
    # events written before this fix have no "_kept" key; unmerge must still restore them
    with Session(db) as s:
        A, B, C = build(s)
        before = snapshot(s)
        ev = merge(s, A, B, method="manual", evidence={})
        ev.moved = {k: v for k, v in ev.moved.items() if k != "_kept"}
        s.flush()
        unmerge(s, ev.id)
        assert snapshot(s) == before
        s.commit()
