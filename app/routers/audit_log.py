from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models import AuditEvent

router = APIRouter()


@router.get("/audit-log")
def list_audit_events(request: Request, db: Session = Depends(get_db)):
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(200)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "audit_log.html", {"events": events})
