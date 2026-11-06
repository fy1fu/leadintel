"""Website audit API (M3). Starting an audit is a POST, so the S1 token and same-origin rules apply."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from app.api.business import engine
from app.audit import service as audits
from app.config import get_settings

router = APIRouter(prefix="/api")


def _crm_user(conn) -> str | None:
    from app.crm import service as crm
    me = crm.identity(conn) if crm.crm_available(conn) else None
    return me.user_id if me else None


@router.get("/audits/status")
def audit_status() -> dict:
    return {**audits.JOBS.status(), "crawler_available": audits.crawler_available()}


@router.get("/locations/{location_id}/audits")
def location_audits(location_id: int) -> dict:
    with engine().connect() as c:
        url = audits.start_url_for(c, location_id)
        return {"website": url, "can_audit": bool(url) and audits.crawler_available(),
                "crawler_available": audits.crawler_available(), "audits": audits.history(c, location_id)}


@router.post("/locations/{location_id}/audits", status_code=202)
def start_audit(location_id: int) -> dict:
    if not audits.crawler_available():
        raise HTTPException(503, detail="website audits need the crawler component, which is not installed in this version")
    e = engine()
    with e.connect() as c:
        if audits.start_url_for(c, location_id) is None:
            raise HTTPException(409, detail="no website is recorded for this business")
        who = _crm_user(c)
    settings = get_settings()
    try:
        audits.JOBS.start(e, settings.data_dir, location_id, audits.default_deps(settings), requested_by=who)
    except audits.AuditUnavailable as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return audits.JOBS.status()


@router.get("/audits/{audit_id}")
def audit_detail(audit_id: int) -> dict:
    with engine().connect() as c:
        d = audits.detail(c, audit_id)
    if d is None:
        raise HTTPException(404, detail="unknown audit")
    return d


@router.get("/audits/artifacts/{artifact_id}")
def audit_artifact(artifact_id: int) -> Response:
    with engine().connect() as c:
        data = audits.artifact_bytes(c, get_settings().data_dir, artifact_id)
    if data is None:
        raise HTTPException(404, detail="screenshot not available")
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": "inline"})
