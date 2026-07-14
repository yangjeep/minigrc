from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db
from app.models import Risk

router = APIRouter(prefix="/risks", tags=["risks"])


@router.get("")
def list_risks(request: Request, db: Session = Depends(get_db)):
    risks = db.scalars(select(Risk).order_by(Risk.created_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "risks/list.html", {"risks": risks})


@router.post("")
def create_risk(
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form("general"),
    likelihood: int = Form(1),
    impact: int = Form(1),
    owner: str = Form(""),
    treatment_plan: str = Form(""),
    db: Session = Depends(get_db),
):
    risk = Risk(
        title=title,
        description=description,
        category=category,
        likelihood=likelihood,
        impact=impact,
        owner=owner,
        treatment_plan=treatment_plan,
    )
    db.add(risk)
    db.flush()
    record_audit_event(
        db,
        entity_type="risk",
        entity_id=risk.id,
        action="create",
        detail=f"Created risk '{risk.title}'",
    )
    return RedirectResponse(url="/risks", status_code=303)
