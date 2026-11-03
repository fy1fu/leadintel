from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (ContactPoint, Fact, Location, LocationContact, LocationOrganization, LocationSourceLink,
                           MergeEvent, Organization, OrganizationLink, ReviewQueue, Source)
from app.resolve.matching import dedupe_locations
from app.resolve.merge import MergeError, merge, unmerge
import pytest

JAX = (-81.6588, 30.3297)
M = 1 / 111_000  # ~1 m in degrees latitude


def loc(s, name, dy_m=0.0, phone=None, county="12031"):
    from app.normalize.core import name_norm
    l = Location(display_name=name, name_norm=name_norm(name), county_fips=county, operating_status="open",
                 geom=f"SRID=4326;POINT({JAX[0]} {JAX[1] + dy_m * M})")
    s.add(l)
    s.flush()
    s.add(LocationSourceLink(location_id=l.id, source_key="overture", external_id=f"x{l.id}", match_method="source_identity"))
    s.add(Fact(subject_type="location", subject_id=l.id, field="name", value={"v": name}, value_norm=name,
               source_key="overture", provenance="directory"))
    if phone:
        cp = s.execute(select(ContactPoint).where(ContactPoint.value_norm == phone)).scalar_one_or_none()
        if cp is None:
            cp = ContactPoint(kind="phone", value_norm=phone, display=phone)
            s.add(cp)
            s.flush()
        s.add(LocationContact(location_id=l.id, contact_point_id=cp.id, source_key="overture"))
    s.flush()
    return l


def test_same_phone_and_name_nearby_merges_and_unmerge_restores(db):
    with Session(db) as s:
        a, b = loc(s, "Jax Shine Mobile Detailing", 0, "+19045550101"), loc(s, "Jax Shine Mobile Detailing LLC", 40, "+19045550101")
        st = dedupe_locations(s)
        assert st.auto_merged == 1
        assert s.get(Location, b.id).merged_into_id == a.id
        assert s.execute(select(Fact).where(Fact.subject_id == b.id)).first() is None  # facts moved to survivor
        ev = s.execute(select(MergeEvent)).scalar_one()
        unmerge(s, ev.id)
        assert s.get(Location, b.id).merged_into_id is None
        assert s.execute(select(Fact.subject_id).where(Fact.value_norm == "Jax Shine Mobile Detailing LLC")).scalar() == b.id
        assert s.execute(select(LocationSourceLink.location_id).where(LocationSourceLink.external_id == f"x{b.id}")).scalar() == b.id
        with pytest.raises(MergeError):
            unmerge(s, ev.id)  # cannot revert twice


def test_shared_phone_with_different_names_is_colocation_not_duplicate(db):
    with Session(db) as s:
        loc(s, "Walgreens Pharmacy", 0, "+19045550202"), loc(s, "Western Union", 30, "+19045550202")
        st = dedupe_locations(s)
        assert (st.auto_merged, st.queued, st.colocated) == (0, 0, 1)


def test_similar_agents_sharing_office_phone_go_to_review_not_merge(db):
    with Session(db) as s:
        loc(s, "William Ruday: Primerica - Financial Services", 0, "+19045550303")
        loc(s, "Cyndi Birch: Primerica - Financial Services", 5, "+19045550303")
        st = dedupe_locations(s)
        assert st.auto_merged == 0
        assert st.queued + st.colocated == 1
        assert s.execute(select(Location).where(Location.merged_into_id.is_not(None))).first() is None


def test_identical_names_without_phone_are_queued_never_merged(db):
    with Session(db) as s:
        loc(s, "Domino's Pizza", 0), loc(s, "Domino's Pizza", 50)
        st = dedupe_locations(s)
        assert (st.auto_merged, st.queued) == (0, 1)
        assert s.execute(select(ReviewQueue.kind)).scalar() == "location_merge"


def test_same_brand_far_apart_stays_separate_locations(db):
    with Session(db) as s:
        loc(s, "Valley Bank", 0, "+19045550404"), loc(s, "Valley Bank", 2000, "+19045550404")
        st = dedupe_locations(s)
        assert (st.auto_merged, st.queued) == (0, 0)


def test_cannot_merge_into_self_or_twice(db):
    with Session(db) as s:
        a, b, c = loc(s, "A"), loc(s, "B"), loc(s, "C")
        with pytest.raises(MergeError):
            merge(s, a.id, a.id, method="t", evidence={})
        merge(s, a.id, b.id, method="t", evidence={})
        with pytest.raises(MergeError):
            merge(s, c.id, b.id, method="t", evidence={})
