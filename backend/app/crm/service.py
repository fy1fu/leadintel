"""Local CRM (M2): identities, claims, pipeline, activity history, lists and the dashboard.

Write path and the B0 maintenance gate: see app.crm.access. By default every CRM write takes the maintenance
lock like any other writer; only a separate PostgreSQL login that the database itself restricts to the CRM
tables (verified before use) may write while another operation holds the lock.
* Concurrency: one ``crm_prospect`` row per (organization, business) (unique constraint); a claim is a
  conditional UPDATE (``owner_user_id IS NULL``), so two simultaneous claims cannot both succeed. Field edits
  use optimistic concurrency (``version``). Every change also appends a ``crm_activity`` row.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.crm.access import CrmUnavailable, CrmWriter, writer_for
from app.db.models import CRM_STATUSES

USER_ACTIVITY_KINDS = ("note", "call", "email", "meeting", "visit")
CLAIM_FILTERS = ("mine", "others", "unclaimed", "claimed")
FOLLOW_UP_FILTERS = ("overdue", "today", "week", "any", "none")


class CrmError(Exception):
    status = 400


class NotFound(CrmError):
    status = 404


class Conflict(CrmError):
    status = 409


class Forbidden(CrmError):
    status = 403


class Unavailable(CrmError):
    """The change cannot be saved right now (upgrade running, or the maintenance lock is held)."""
    status = 503


UpgradeInProgress = Unavailable   # earlier name


class NotConfigured(CrmError):
    status = 409


@dataclass(frozen=True)
class Identity:
    organization_id: str
    user_id: str
    display_name: str
    initials: str
    role: str


# ---------------------------------------------------------------- writers
def crm_writer(business_engine: Engine, crm_url: str | None = None) -> CrmWriter:
    """How CRM writes reach the database (see app.crm.access): the maintenance lock, or a verified restricted login."""
    if crm_url is None:
        from app.config import get_settings
        crm_url = get_settings().crm_database_url
    return writer_for(business_engine, crm_url)


@contextmanager
def _tx(w: CrmWriter) -> Iterator[Connection]:
    try:
        with w.begin() as c:
            yield c
    except CrmUnavailable as exc:
        raise Unavailable(str(exc)) from exc


# ---------------------------------------------------------------- identity
def crm_available(c: Connection) -> bool:
    return bool(c.execute(text("SELECT to_regclass('crm_prospect') IS NOT NULL")).scalar())


def identity(c: Connection) -> Identity | None:
    """The local organization's first active user, or None before setup (reads only)."""
    if not crm_available(c):
        return None
    row = c.execute(text(
        "SELECT o.id, u.id, u.display_name, u.initials, u.role FROM crm_organization o "
        "JOIN crm_user u ON u.organization_id = o.id AND u.active WHERE o.kind = 'local' "
        "ORDER BY u.created_at, u.id LIMIT 1")).first()
    return Identity(*[str(v) if i < 2 else v for i, v in enumerate(row)]) if row else None


def _clean_initials(initials: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", initials or "").upper()[:4]
    if not s:
        raise CrmError("initials must contain at least one letter or digit")
    return s


def setup_identity(w: CrmWriter, display_name: str, initials: str, email: str | None = None,
                   organization_name: str = "My company") -> Identity:
    """Creates (or updates) the local organization and its user. Idempotent."""
    display_name = (display_name or "").strip()
    if not display_name or len(display_name) > 120:
        raise CrmError("display name must be 1-120 characters")
    ini = _clean_initials(initials)
    with _tx(w) as c:
        c.execute(text("INSERT INTO crm_organization (name, kind) VALUES (:n, 'local') "
                       "ON CONFLICT (kind) WHERE kind = 'local' DO NOTHING"), {"n": organization_name})
        org = str(c.execute(text("SELECT id FROM crm_organization WHERE kind = 'local'")).scalar())
        me = identity(c)
        if me:
            c.execute(text("UPDATE crm_user SET display_name = :d, initials = :i, email = :m WHERE id = CAST(:u AS uuid)"),
                      {"d": display_name, "i": ini, "m": email, "u": me.user_id})
        else:
            c.execute(text("INSERT INTO crm_user (organization_id, display_name, initials, email, role) "
                           "VALUES (CAST(:o AS uuid), :d, :i, :m, 'owner')"), {"o": org, "d": display_name, "i": ini, "m": email})
        return identity(c)


def _require(c: Connection) -> Identity:
    me = identity(c)
    if me is None:
        raise NotConfigured("set up your salesperson profile first")
    return me


# ---------------------------------------------------------------- prospects
def _location_ok(c: Connection, location_id: int) -> None:
    row = c.execute(text("SELECT merged_into_id FROM location WHERE id = :l"), {"l": location_id}).first()
    if row is None:
        raise NotFound("unknown business")
    if row[0] is not None:
        raise Conflict(f"this business was merged into business {row[0]}; use that record")


def _ensure_row(c: Connection, me: Identity, location_id: int) -> None:
    c.execute(text("INSERT INTO crm_prospect (organization_id, location_id) VALUES (CAST(:o AS uuid), :l) "
                   "ON CONFLICT (organization_id, location_id) DO NOTHING"), {"o": me.organization_id, "l": location_id})


def _log(c: Connection, me: Identity, location_id: int, kind: str, body: str | None = None, data: dict | None = None,
         occurred_at: datetime | None = None) -> None:
    import json
    c.execute(text("INSERT INTO crm_activity (organization_id, location_id, user_id, kind, body, data, occurred_at) "
                   "VALUES (CAST(:o AS uuid), :l, CAST(:u AS uuid), :k, :b, CAST(:d AS jsonb), coalesce(:t, now()))"),
              {"o": me.organization_id, "l": location_id, "u": me.user_id, "k": kind, "b": body,
               "d": json.dumps(data or {}), "t": occurred_at})


def state(c: Connection, location_id: int) -> dict:
    """CRM state of one business for the local organization (reads only)."""
    me = identity(c)
    base = {"configured": me is not None, "status": "new", "owner": None, "claimed_at": None, "next_action": None,
            "follow_up_at": None, "version": 0, "owned_by_me": False, "activities": [], "lists": []}
    if me is None:
        return base
    row = c.execute(text(
        "SELECT p.status, p.owner_user_id, u.display_name, u.initials, p.claimed_at, p.next_action, p.follow_up_at, p.version "
        "FROM crm_prospect p LEFT JOIN crm_user u ON u.id = p.owner_user_id "
        "WHERE p.organization_id = CAST(:o AS uuid) AND p.location_id = :l"), {"o": me.organization_id, "l": location_id}).first()
    if row:
        base.update(status=row[0], owner=({"user_id": str(row[1]), "display_name": row[2], "initials": row[3]} if row[1] else None),
                    claimed_at=row[4], next_action=row[5], follow_up_at=row[6], version=row[7],
                    owned_by_me=row[1] is not None and str(row[1]) == me.user_id)
    base["activities"] = [dict(r) for r in c.execute(text(
        "SELECT a.id, a.kind, a.body, a.data, a.occurred_at, a.created_at, u.initials, u.display_name FROM crm_activity a "
        "JOIN crm_user u ON u.id = a.user_id WHERE a.organization_id = CAST(:o AS uuid) AND a.location_id = :l "
        "ORDER BY a.occurred_at DESC, a.id DESC LIMIT 200"), {"o": me.organization_id, "l": location_id}).mappings()]
    base["lists"] = [dict(r) for r in c.execute(text(
        "SELECT cl.id, cl.name, cl.kind FROM crm_list_item i JOIN crm_list cl ON cl.id = i.list_id "
        "WHERE cl.organization_id = CAST(:o AS uuid) AND i.location_id = :l ORDER BY cl.name"),
        {"o": me.organization_id, "l": location_id}).mappings()]
    return base


def claim(w: CrmWriter, location_id: int) -> dict:
    with _tx(w) as c:
        me = _require(c)
        _location_ok(c, location_id)
        _ensure_row(c, me, location_id)
        got = c.execute(text(
            "UPDATE crm_prospect SET owner_user_id = CAST(:u AS uuid), claimed_at = now(), version = version + 1, updated_at = now() "
            "WHERE organization_id = CAST(:o AS uuid) AND location_id = :l AND owner_user_id IS NULL RETURNING id"),
            {"u": me.user_id, "o": me.organization_id, "l": location_id}).first()
        if got:
            _log(c, me, location_id, "claim")
        else:
            owner = c.execute(text("SELECT u.id, u.initials FROM crm_prospect p JOIN crm_user u ON u.id = p.owner_user_id "
                                   "WHERE p.organization_id = CAST(:o AS uuid) AND p.location_id = :l"),
                              {"o": me.organization_id, "l": location_id}).first()
            if owner and str(owner[0]) != me.user_id:
                raise Conflict(f"already claimed by {owner[1]}")
    with w.connect() as c:
        return state(c, location_id)


def release(w: CrmWriter, location_id: int) -> dict:
    with _tx(w) as c:
        me = _require(c)
        row = c.execute(text("SELECT owner_user_id FROM crm_prospect WHERE organization_id = CAST(:o AS uuid) AND location_id = :l "
                             "FOR UPDATE"), {"o": me.organization_id, "l": location_id}).first()
        if not row or row[0] is None:
            raise Conflict("this business is not claimed")
        if str(row[0]) != me.user_id and me.role not in ("owner", "admin"):
            raise Forbidden("only the salesperson who claimed it, or an admin, can release it")
        c.execute(text("UPDATE crm_prospect SET owner_user_id = NULL, claimed_at = NULL, version = version + 1, updated_at = now() "
                       "WHERE organization_id = CAST(:o AS uuid) AND location_id = :l"), {"o": me.organization_id, "l": location_id})
        _log(c, me, location_id, "release", data={"previous_owner": str(row[0])})
    with w.connect() as c:
        return state(c, location_id)


def reassign(w: CrmWriter, location_id: int, user_id: str) -> dict:
    with _tx(w) as c:
        me = _require(c)
        if me.role not in ("owner", "admin"):
            raise Forbidden("only an admin can reassign a business")
        _location_ok(c, location_id)
        target = c.execute(text("SELECT id FROM crm_user WHERE id = CAST(:u AS uuid) AND organization_id = CAST(:o AS uuid) AND active"),
                           {"u": user_id, "o": me.organization_id}).first()
        if not target:
            raise NotFound("unknown salesperson")
        _ensure_row(c, me, location_id)
        prev = c.execute(text("SELECT owner_user_id FROM crm_prospect WHERE organization_id = CAST(:o AS uuid) AND location_id = :l "
                              "FOR UPDATE"), {"o": me.organization_id, "l": location_id}).scalar()
        c.execute(text("UPDATE crm_prospect SET owner_user_id = CAST(:u AS uuid), claimed_at = now(), version = version + 1, "
                       "updated_at = now() WHERE organization_id = CAST(:o AS uuid) AND location_id = :l"),
                  {"u": user_id, "o": me.organization_id, "l": location_id})
        _log(c, me, location_id, "reassign", data={"from": str(prev) if prev else None, "to": user_id})
    with w.connect() as c:
        return state(c, location_id)


_UNSET = object()


def update(w: CrmWriter, location_id: int, version: int, status=_UNSET, next_action=_UNSET, follow_up_at=_UNSET) -> dict:
    """Changes status / next action / follow-up date; `version` must match (optimistic concurrency)."""
    if status is not _UNSET and status not in CRM_STATUSES:
        raise CrmError(f"unknown status {status!r}")
    if next_action is not _UNSET and next_action is not None and len(next_action) > 500:
        raise CrmError("next action is longer than 500 characters")
    with _tx(w) as c:
        me = _require(c)
        _location_ok(c, location_id)
        _ensure_row(c, me, location_id)
        cur = c.execute(text("SELECT status, next_action, follow_up_at, version, owner_user_id FROM crm_prospect "
                             "WHERE organization_id = CAST(:o AS uuid) AND location_id = :l FOR UPDATE"),
                        {"o": me.organization_id, "l": location_id}).first()
        if cur[4] is not None and str(cur[4]) != me.user_id and me.role not in ("owner", "admin"):
            raise Forbidden("another salesperson has claimed this business")
        if version != cur[3] and not (version == 0 and cur[3] == 1):
            raise Conflict("this business was changed elsewhere; reload it and try again")
        changes = {}
        if status is not _UNSET and status != cur[0]:
            changes["status"] = status
        if next_action is not _UNSET and (next_action or None) != cur[1]:
            changes["next_action"] = next_action or None
        if follow_up_at is not _UNSET and follow_up_at != cur[2]:
            changes["follow_up_at"] = follow_up_at
        if changes:
            sets = ", ".join(f"{k} = :{k}" for k in changes)
            c.execute(text(f"UPDATE crm_prospect SET {sets}, version = version + 1, updated_at = now() "
                           "WHERE organization_id = CAST(:o AS uuid) AND location_id = :l"),
                      {**changes, "o": me.organization_id, "l": location_id})
            if "status" in changes:
                _log(c, me, location_id, "status", data={"from": cur[0], "to": changes["status"]})
            if "next_action" in changes:
                _log(c, me, location_id, "next_action", body=changes["next_action"])
            if "follow_up_at" in changes:
                fu = changes["follow_up_at"]
                _log(c, me, location_id, "follow_up", data={"at": fu.isoformat() if fu else None})
    with w.connect() as c:
        return state(c, location_id)


def add_activity(w: CrmWriter, location_id: int, kind: str, body: str | None, occurred_at: datetime | None = None,
                 outcome: str | None = None) -> dict:
    """Notes and contact history (call, email, meeting, visit), written by the salesperson."""
    if kind not in USER_ACTIVITY_KINDS:
        raise CrmError(f"kind must be one of {list(USER_ACTIVITY_KINDS)}")
    body = (body or "").strip()
    if kind == "note" and not body:
        raise CrmError("a note needs some text")
    if len(body) > 10_000:
        raise CrmError("text is longer than 10,000 characters")
    with _tx(w) as c:
        me = _require(c)
        _location_ok(c, location_id)
        _ensure_row(c, me, location_id)
        _log(c, me, location_id, kind, body or None, {"outcome": outcome} if outcome else {}, occurred_at)
    with w.connect() as c:
        return state(c, location_id)


# ---------------------------------------------------------------- lists and campaigns
def lists(c: Connection) -> list[dict]:
    me = identity(c)
    if me is None:
        return []
    return [dict(r) for r in c.execute(text(
        "SELECT cl.id, cl.name, cl.kind, cl.description, cl.archived, cl.created_at, count(i.location_id) AS size "
        "FROM crm_list cl LEFT JOIN crm_list_item i ON i.list_id = cl.id WHERE cl.organization_id = CAST(:o AS uuid) "
        "GROUP BY cl.id ORDER BY cl.archived, cl.kind, lower(cl.name)"), {"o": me.organization_id}).mappings()]


def create_list(w: CrmWriter, name: str, kind: str = "list", description: str | None = None) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise CrmError("list name must be 1-120 characters")
    if kind not in ("list", "campaign"):
        raise CrmError("kind must be list or campaign")
    with _tx(w) as c:
        me = _require(c)
        row = c.execute(text("INSERT INTO crm_list (organization_id, name, kind, description, created_by) "
                             "VALUES (CAST(:o AS uuid), :n, :k, :d, CAST(:u AS uuid)) "
                             "ON CONFLICT (organization_id, name) DO NOTHING RETURNING id"),
                        {"o": me.organization_id, "n": name, "k": kind, "d": description, "u": me.user_id}).first()
        if not row:
            raise Conflict(f"a list called {name!r} already exists")
        return {"id": row[0], "name": name, "kind": kind, "description": description}


def _list_of_org(c: Connection, me: Identity, list_id: int) -> tuple:
    row = c.execute(text("SELECT id, name FROM crm_list WHERE id = :i AND organization_id = CAST(:o AS uuid)"),
                    {"i": list_id, "o": me.organization_id}).first()
    if not row:
        raise NotFound("unknown list")
    return row


def set_list_archived(w: CrmWriter, list_id: int, archived: bool) -> None:
    with _tx(w) as c:
        me = _require(c)
        _list_of_org(c, me, list_id)
        c.execute(text("UPDATE crm_list SET archived = :a WHERE id = :i"), {"a": archived, "i": list_id})


def add_to_list(w: CrmWriter, list_id: int, location_id: int) -> dict:
    with _tx(w) as c:
        me = _require(c)
        lst = _list_of_org(c, me, list_id)
        _location_ok(c, location_id)
        added = c.execute(text("INSERT INTO crm_list_item (list_id, location_id, added_by) VALUES (:i, :l, CAST(:u AS uuid)) "
                               "ON CONFLICT DO NOTHING RETURNING list_id"), {"i": list_id, "l": location_id, "u": me.user_id}).first()
        if added:
            _ensure_row(c, me, location_id)
            _log(c, me, location_id, "list_add", data={"list_id": list_id, "list": lst[1]})
    with w.connect() as c:
        return state(c, location_id)


def remove_from_list(w: CrmWriter, list_id: int, location_id: int) -> dict:
    with _tx(w) as c:
        me = _require(c)
        lst = _list_of_org(c, me, list_id)
        gone = c.execute(text("DELETE FROM crm_list_item WHERE list_id = :i AND location_id = :l RETURNING list_id"),
                         {"i": list_id, "l": location_id}).first()
        if gone:
            _log(c, me, location_id, "list_remove", data={"list_id": list_id, "list": lst[1]})
    with w.connect() as c:
        return state(c, location_id)


# ---------------------------------------------------------------- dashboard
def dashboard(c: Connection) -> dict:
    me = identity(c)
    if me is None:
        return {"configured": False}
    o = {"o": me.organization_id, "u": me.user_id}
    by_status = dict(c.execute(text("SELECT status, count(*) FROM crm_prospect WHERE organization_id = CAST(:o AS uuid) "
                                    "AND (owner_user_id = CAST(:u AS uuid)) GROUP BY 1"), o).all())
    follow = [dict(r) for r in c.execute(text(
        "SELECT p.location_id, l.display_name AS name, p.follow_up_at, p.next_action, p.status, "
        "CASE WHEN p.follow_up_at < date_trunc('day', now()) THEN 'overdue' "
        "     WHEN p.follow_up_at < date_trunc('day', now()) + interval '1 day' THEN 'today' ELSE 'upcoming' END AS due "
        "FROM crm_prospect p JOIN location l ON l.id = p.location_id "
        "WHERE p.organization_id = CAST(:o AS uuid) AND p.owner_user_id = CAST(:u AS uuid) AND p.follow_up_at IS NOT NULL "
        "AND p.status NOT IN ('won', 'lost', 'not_a_fit') AND p.follow_up_at < date_trunc('day', now()) + interval '8 days' "
        "ORDER BY p.follow_up_at LIMIT 100"), o).mappings()]
    recent = [dict(r) for r in c.execute(text(
        "SELECT a.location_id, l.display_name AS name, a.kind, a.body, a.data, a.occurred_at, u.initials FROM crm_activity a "
        "JOIN location l ON l.id = a.location_id JOIN crm_user u ON u.id = a.user_id "
        "WHERE a.organization_id = CAST(:o AS uuid) ORDER BY a.occurred_at DESC, a.id DESC LIMIT 25"), o).mappings()]
    claimed = c.execute(text("SELECT count(*) FROM crm_prospect WHERE organization_id = CAST(:o AS uuid) "
                             "AND owner_user_id = CAST(:u AS uuid)"), o).scalar()
    return {"configured": True, "me": me.__dict__, "claimed": int(claimed or 0),
            "pipeline": {s: int(by_status.get(s, 0)) for s in CRM_STATUSES},
            "follow_ups": follow, "overdue": sum(1 for f in follow if f["due"] == "overdue"),
            "due_today": sum(1 for f in follow if f["due"] == "today"), "recent": recent}


# ---------------------------------------------------------------- saved searches (M3, migration 0007)
SAVED_KEYS = {"county", "niche", "qualification", "website", "operating", "sales", "q", "phone", "email", "lps_min",
              "bos_min", "sort", "claim", "follow_up", "list_id", "near", "r", "ru", "nl", "all", "view"}
MAX_SAVED_PARAMS = 4000


def _check_saved_params(params: dict) -> dict:
    """Saved-search parameters are the app's URL state; they must form a valid prospect search (same validation)."""
    import json
    if not isinstance(params, dict) or not params:
        raise CrmError("a saved search needs its search parameters")
    bad = sorted(k for k in params if k not in SAVED_KEYS)
    if bad:
        raise CrmError(f"unknown search parameters: {bad}")
    if any(not isinstance(v, str) or len(v) > 1000 for v in params.values()):
        raise CrmError("search parameters must be short texts")
    if len(json.dumps(params)) > MAX_SAVED_PARAMS:
        raise CrmError("search parameters are too long")
    from app.api.filters import FilterError, ProspectFilter, split_list
    try:
        kw = {}
        if "near" in params:
            lat, sep, lon = params["near"].partition(",")
            if not sep:
                raise ValueError("near must be 'latitude,longitude'")
            kw = {"near_lat": float(lat), "near_lon": float(lon), "radius_m": float(params.get("r", "nan"))}
            if params.get("ru", "mi") not in ("mi", "km"):
                raise ValueError("unknown radius unit")
        elif any(k in params for k in ("r", "ru", "nl")):
            raise ValueError("a radius needs a center (near)")
        if params.get("view", "split") not in ("split", "list", "map"):
            raise ValueError("unknown view")
        yn = {"": None, "true": True, "false": False}
        if params.get("phone", "") not in yn or params.get("email", "") not in yn:
            raise ValueError("phone and email are 'true', 'false' or empty")
        num = {k: float(params[k]) for k in ("lps_min", "bos_min") if params.get(k)}
        ProspectFilter(qualifications=split_list(params.get("qualification")), website=split_list(params.get("website")),
                       operating=split_list(params.get("operating")), sales=split_list(params.get("sales")),
                       claim=params.get("claim") or None, follow_up=params.get("follow_up") or None,
                       list_id=int(params["list_id"]) if params.get("list_id") else None,
                       phone=yn[params.get("phone", "")], email=yn[params.get("email", "")],
                       sort=params.get("sort") or "lps", crm=True, **num, **kw)
    except (FilterError, ValueError) as exc:
        raise CrmError(f"not a valid search: {exc}") from exc
    return params


def saved_searches(c: Connection) -> list[dict]:
    me = identity(c)
    if me is None:
        return []
    return [dict(r) for r in c.execute(text(
        "SELECT id, name, params, version, created_at, updated_at FROM crm_saved_search "
        "WHERE organization_id = CAST(:o AS uuid) ORDER BY lower(name)"), {"o": me.organization_id}).mappings()]


def save_search(w: CrmWriter, name: str, params: dict) -> dict:
    import json
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise CrmError("a saved search needs a name of 1-120 characters")
    params = _check_saved_params(params)
    with _tx(w) as c:
        me = _require(c)
        row = c.execute(text(
            "INSERT INTO crm_saved_search (organization_id, name, params, created_by) "
            "VALUES (CAST(:o AS uuid), :n, CAST(:p AS jsonb), CAST(:u AS uuid)) "
            "ON CONFLICT (organization_id, name) DO UPDATE SET params = EXCLUDED.params, updated_at = now(), "
            "version = crm_saved_search.version + 1 RETURNING id, version"),
            {"o": me.organization_id, "n": name, "p": json.dumps(params), "u": me.user_id}).first()
        return {"id": row[0], "name": name, "params": params, "version": row[1]}


def delete_saved_search(w: CrmWriter, search_id: int) -> None:
    with _tx(w) as c:
        me = _require(c)
        gone = c.execute(text("DELETE FROM crm_saved_search WHERE id = :i AND organization_id = CAST(:o AS uuid) "
                              "RETURNING id"), {"i": search_id, "o": me.organization_id}).first()
        if not gone:
            raise NotFound("unknown saved search")
