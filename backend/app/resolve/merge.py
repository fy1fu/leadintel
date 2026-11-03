"""Reversible location merges (PHASE1.md item 1.7).

A merge re-points child rows of the merged location to the survivor and records the exact row ids
moved in `merge_event.moved`, so `unmerge` restores the previous state precisely. Source records
are never modified; the merged location row is kept with `merged_into_id` set.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import Location, MergeEvent

# table -> (location column, unique-key columns other than the location column)
_CHILDREN = {
    "location_source_link": ("location_id", None),
    "location_alias": ("location_id", ("alias_norm", "kind")),
    "online_profile": ("location_id", ("url_norm",)),
    "location_contact": ("location_id", ("contact_point_id", "source_key")),
    "location_organization": ("location_id", ("organization_id", "role")),
}


class MergeError(RuntimeError):
    pass


def merge(s: Session, survivor_id: int, merged_id: int, *, method: str, evidence: dict, actor: str = "system") -> MergeEvent:
    if survivor_id == merged_id:
        raise MergeError("cannot merge a location into itself")
    surv, mer = s.get(Location, survivor_id), s.get(Location, merged_id)
    if surv is None or mer is None or surv.merged_into_id or mer.merged_into_id:
        raise MergeError("both locations must exist and be unmerged")
    moved: dict[str, list[int]] = {}
    for table, (col, uniq) in _CHILDREN.items():
        cond = ""
        if uniq:
            same = " AND ".join(f"o.{u} = c.{u}" for u in uniq)
            cond = f" AND NOT EXISTS (SELECT 1 FROM {table} o WHERE o.{col} = :surv AND {same})"
        ids = s.execute(text(f"UPDATE {table} c SET {col} = :surv WHERE c.{col} = :mer{cond} RETURNING c.id"),
                        {"surv": survivor_id, "mer": merged_id}).scalars().all()
        moved[table] = list(ids)
    moved["fact"] = list(s.execute(text(
        "UPDATE fact c SET subject_id = :surv WHERE c.subject_type='location' AND c.subject_id = :mer AND NOT EXISTS "
        "(SELECT 1 FROM fact o WHERE o.subject_type='location' AND o.subject_id=:surv AND o.field=c.field "
        "AND o.value_norm IS NOT DISTINCT FROM c.value_norm AND o.source_record_id IS NOT DISTINCT FROM c.source_record_id) "
        "RETURNING c.id"), {"surv": survivor_id, "mer": merged_id}).scalars())
    moved["address"] = list(s.execute(text(
        "UPDATE address c SET owner_id = :surv WHERE c.owner_type='location' AND c.owner_id = :mer AND NOT EXISTS "
        "(SELECT 1 FROM address o WHERE o.owner_type='location' AND o.owner_id=:surv AND o.role=c.role AND o.norm_key=c.norm_key) "
        "RETURNING c.id"), {"surv": survivor_id, "mer": merged_id}).scalars())
    mer.merged_into_id = survivor_id
    ev = MergeEvent(action="merge", survivor_id=survivor_id, merged_id=merged_id, method=method, evidence=evidence,
                    moved=moved, actor=actor)
    s.add(ev)
    s.flush()
    return ev


def unmerge(s: Session, event_id: int, *, actor: str = "system") -> MergeEvent:
    ev = s.get(MergeEvent, event_id)
    if ev is None or ev.action != "merge":
        raise MergeError("not a merge event")
    if s.execute(text("SELECT 1 FROM merge_event WHERE reverts_event_id = :e"), {"e": event_id}).first():
        raise MergeError("merge already reverted")
    for table, ids in ev.moved.items():
        if not ids:
            continue
        col = {"fact": "subject_id", "address": "owner_id"}.get(table) or _CHILDREN[table][0]
        s.execute(text(f"UPDATE {table} SET {col} = :mer WHERE id = ANY(:ids)"), {"mer": ev.merged_id, "ids": ids})
    s.get(Location, ev.merged_id).merged_into_id = None
    back = MergeEvent(action="unmerge", survivor_id=ev.survivor_id, merged_id=ev.merged_id, method="manual_unmerge",
                      evidence={}, moved={}, actor=actor, reverts_event_id=ev.id)
    s.add(back)
    s.flush()
    return back
