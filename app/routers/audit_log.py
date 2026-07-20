"""Audit Log — relocated under Admin (issue #7's IA groups it there since
it's an org-wide mutation history, not per-user data). The legacy
`/audit-log` path permanently redirects rather than keeping a second
implementation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin
from app.models import AuditEvent

router = APIRouter(dependencies=[Depends(require_admin)])
legacy_router = APIRouter()


@router.get("/admin/audit-log")
def list_audit_events(request: Request, db: Session = Depends(get_db)):
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(200)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/audit_log.html", {"events": events})


@legacy_router.get("/audit-log")
def legacy_audit_log_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/audit-log", status_code=308)
