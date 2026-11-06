"""Local CRM API (M2). Writes are POST/PATCH/DELETE, so the S1 token and same-origin rules apply to all of them."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.business import engine
from app.crm import service as crm

router = APIRouter(prefix="/api/crm")


def _engines():
    e = engine()
    with e.connect() as c:
        if not crm.crm_available(c):
            raise HTTPException(409, detail="the sales features need a database upgrade (run Setup again)")
    return e, crm.crm_engine(e)


def _run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except crm.CrmError as exc:
        raise HTTPException(exc.status, detail=str(exc)) from exc


class MeIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    initials: str = Field(min_length=1, max_length=8)
    email: str | None = Field(None, max_length=254)


class UpdateIn(BaseModel):
    version: int = Field(ge=0)
    status: str | None = None
    next_action: str | None = Field(None, max_length=500)
    follow_up_at: datetime | None = None


class ActivityIn(BaseModel):
    kind: str
    body: str | None = Field(None, max_length=10_000)
    occurred_at: datetime | None = None
    outcome: str | None = Field(None, max_length=60)


class ReassignIn(BaseModel):
    user_id: str = Field(min_length=36, max_length=36)


class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "list"
    description: str | None = Field(None, max_length=1000)


class ListPatch(BaseModel):
    archived: bool


class ListItemIn(BaseModel):
    location_id: int


@router.get("/me")
def me() -> dict:
    e = engine()
    with e.connect() as c:
        if not crm.crm_available(c):
            return {"available": False, "configured": False, "me": None}
        who = crm.identity(c)
    return {"available": True, "configured": who is not None, "me": who.__dict__ if who else None}


@router.post("/me")
def set_me(body: MeIn) -> dict:
    _, ce = _engines()
    who = _run(crm.setup_identity, ce, body.display_name, body.initials, body.email)
    return {"available": True, "configured": True, "me": who.__dict__}


@router.get("/users")
def users() -> list[dict]:
    e = engine()
    with e.connect() as c:
        who = crm.identity(c)
        if who is None:
            return []
        return [dict(r) for r in c.execute(text(
            "SELECT id::text AS id, display_name, initials, role FROM crm_user WHERE organization_id = CAST(:o AS uuid) "
            "AND active ORDER BY display_name"), {"o": who.organization_id}).mappings()]


@router.get("/prospects/{location_id}")
def prospect(location_id: int) -> dict:
    e = engine()
    with e.connect() as c:
        if not crm.crm_available(c):
            return {"available": False}
        return {"available": True, **crm.state(c, location_id)}


@router.post("/prospects/{location_id}/claim")
def claim(location_id: int) -> dict:
    return _run(crm.claim, _engines()[1], location_id)


@router.post("/prospects/{location_id}/release")
def release(location_id: int) -> dict:
    return _run(crm.release, _engines()[1], location_id)


@router.post("/prospects/{location_id}/reassign")
def reassign(location_id: int, body: ReassignIn) -> dict:
    return _run(crm.reassign, _engines()[1], location_id, body.user_id)


@router.patch("/prospects/{location_id}")
def update(location_id: int, body: UpdateIn) -> dict:
    given = body.model_fields_set
    kw = {k: getattr(body, k) for k in ("status", "next_action", "follow_up_at") if k in given}
    if "status" in kw and kw["status"] is None:
        raise HTTPException(422, detail="status cannot be empty")
    return _run(crm.update, _engines()[1], location_id, body.version, **kw)


@router.post("/prospects/{location_id}/activities")
def add_activity(location_id: int, body: ActivityIn) -> dict:
    return _run(crm.add_activity, _engines()[1], location_id, body.kind, body.body, body.occurred_at, body.outcome)


@router.get("/lists")
def lists() -> list[dict]:
    e = engine()
    with e.connect() as c:
        return crm.lists(c) if crm.crm_available(c) else []


@router.post("/lists")
def create_list(body: ListIn) -> dict:
    return _run(crm.create_list, _engines()[1], body.name, body.kind, body.description)


@router.patch("/lists/{list_id}")
def patch_list(list_id: int, body: ListPatch) -> dict:
    _run(crm.set_list_archived, _engines()[1], list_id, body.archived)
    return {"id": list_id, "archived": body.archived}


@router.post("/lists/{list_id}/items")
def add_item(list_id: int, body: ListItemIn) -> dict:
    return _run(crm.add_to_list, _engines()[1], list_id, body.location_id)


@router.delete("/lists/{list_id}/items/{location_id}")
def remove_item(list_id: int, location_id: int) -> dict:
    return _run(crm.remove_from_list, _engines()[1], list_id, location_id)


@router.get("/dashboard")
def dashboard() -> dict:
    e = engine()
    with e.connect() as c:
        if not crm.crm_available(c):
            return {"available": False, "configured": False}
        return {"available": True, **crm.dashboard(c)}
