"""Reversible location merges (PHASE1.md item 1.7).

A merge re-points child rows of the merged location to the survivor and records the exact row ids
moved in `merge_event.moved`, so `unmerge` restores the previous state precisely. Source records
are never modified; the merged location row is kept with `merged_into_id` set.

A row the survivor already has an equivalent of (same identity key, e.g. the same phone from the same
source) is NOT moved - moving it would duplicate the relationship. It stays on the merged location and
is recorded in `moved["_kept"][table]` as [merged_row_id, survivor_row_id], so the event accounts for
every relationship of the merged location. Unmerge only moves back what was moved.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import Location, MergeEvent

# table -> (location column, unique-key columns other than the location column)
_CHILDREN = {
    "location_source_link": ("location_id", ("source_key", "external_id")),
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
    p = {"surv": survivor_id, "mer": merged_id}
    moved: dict = {}
    kept: dict[str, list[list[int]]] = {}
    for table, col, poly, same in _relations():
        pairs = s.execute(text(f"SELECT c.id, min(o.id) FROM {table} c JOIN {table} o ON o.{col} = :surv{poly('o')} AND {same} "
                               f"WHERE c.{col} = :mer{poly('c')} GROUP BY c.id ORDER BY c.id"), p).all()
        if pairs:
            kept[table] = [[m, a] for m, a in pairs]
        moved[table] = list(s.execute(text(
            f"UPDATE {table} c SET {col} = :surv WHERE c.{col} = :mer{poly('c')} "
            f"AND NOT EXISTS (SELECT 1 FROM {table} o WHERE o.{col} = :surv{poly('o')} AND {same}) RETURNING c.id"), p).scalars())
    moved["_kept"] = kept
    mer.merged_into_id = survivor_id
    ev = MergeEvent(action="merge", survivor_id=survivor_id, merged_id=merged_id, method=method, evidence=evidence,
                    moved=moved, actor=actor)
    s.add(ev)
    s.flush()
    return ev


def _relations():
    """(table, owner column, owner-type filter, identity predicate between rows c and o) for every relationship."""
    def none(_):
        return ""
    for table, (col, uniq) in _CHILDREN.items():
        yield table, col, none, " AND ".join(f"o.{u} = c.{u}" for u in uniq)
    yield ("fact", "subject_id", lambda a: f" AND {a}.subject_type = 'location'",
           "o.field = c.field AND o.value_norm IS NOT DISTINCT FROM c.value_norm "
           "AND o.source_record_id IS NOT DISTINCT FROM c.source_record_id")
    yield ("address", "owner_id", lambda a: f" AND {a}.owner_type = 'location'", "o.role = c.role AND o.norm_key = c.norm_key")


def unmerge(s: Session, event_id: int, *, actor: str = "system") -> MergeEvent:
    ev = s.get(MergeEvent, event_id)
    if ev is None or ev.action != "merge":
        raise MergeError("not a merge event")
    if s.execute(text("SELECT 1 FROM merge_event WHERE reverts_event_id = :e"), {"e": event_id}).first():
        raise MergeError("merge already reverted")
    restored = {}
    for table, ids in ev.moved.items():
        if table.startswith("_"):   # bookkeeping (e.g. "_kept": rows that never moved)
            continue
        restored[table] = ids
        if not ids:
            continue
        col = {"fact": "subject_id", "address": "owner_id"}.get(table) or _CHILDREN[table][0]
        s.execute(text(f"UPDATE {table} SET {col} = :mer WHERE id = ANY(:ids)"), {"mer": ev.merged_id, "ids": ids})
    s.get(Location, ev.merged_id).merged_into_id = None
    back = MergeEvent(action="unmerge", survivor_id=ev.survivor_id, merged_id=ev.merged_id, method="manual_unmerge",
                      evidence={}, moved={"restored": restored}, actor=actor, reverts_event_id=ev.id)
    s.add(back)
    s.flush()
    return back
