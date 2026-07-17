from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_db, require_login
from app.models import AuditEvent, Framework, Policy, RequirementNote, Risk
from app.progress import compute_progress

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    frameworks = db.scalars(select(Framework).where(Framework.is_active.is_(True))).all()
    progress_list = [compute_progress(f) for f in frameworks]
    incomplete_requirements = sum(p.applicable - p.implemented for p in progress_list)

    open_risk_count = (
        db.scalar(select(func.count()).select_from(Risk).where(Risk.status.in_(["open", "mitigating"]))) or 0
    )

    policies_by_status = dict(
        db.execute(
            select(Policy.status, func.count()).where(Policy.archived.is_(False)).group_by(Policy.status)
        ).all()
    )

    today = datetime.date.today()
    soon = today + datetime.timedelta(days=30)
    policies_due_soon = (
        db.scalar(
            select(func.count())
            .select_from(Policy)
            .where(Policy.next_review_date.is_not(None), Policy.next_review_date.between(today, soon))
        )
        or 0
    )
    policies_overdue = (
        db.scalar(
            select(func.count())
            .select_from(Policy)
            .where(Policy.next_review_date.is_not(None), Policy.next_review_date < today)
        )
        or 0
    )

    recent_notes = db.scalars(
        select(RequirementNote).order_by(RequirementNote.created_at.desc()).limit(5)
    ).all()
    recent_audit_events = db.scalars(
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(10)
    ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "framework_count": len(frameworks),
            "progress_list": list(zip(frameworks, progress_list, strict=True)),
            "incomplete_requirements": incomplete_requirements,
            "open_risk_count": open_risk_count,
            "policies_by_status": policies_by_status,
            "policies_due_soon": policies_due_soon,
            "policies_overdue": policies_overdue,
            "recent_notes": recent_notes,
            "recent_audit_events": recent_audit_events,
        },
    )
