from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.models import Risk

router = APIRouter(prefix="/risks", tags=["risks"], dependencies=[Depends(require_login)])


@router.get("")
def list_risks(request: Request, db: Session = Depends(get_db)):
    risks = db.scalars(select(Risk).order_by(Risk.created_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "risks/list.html", {"risks": risks})


@router.post("")
def create_risk(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form("general"),
    likelihood: int = Form(1),
    impact: int = Form(1),
    owner: str = Form(""),
    treatment_plan: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    title = title.strip()
    if not title:
        return redirect_with_flash("/risks", "Title is required.", kind="error")
    if not (1 <= likelihood <= 5) or not (1 <= impact <= 5):
        return redirect_with_flash("/risks", "Likelihood and impact must be between 1 and 5.", kind="error")

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
        actor=request.state.user.email,
    )
    return redirect_with_flash("/risks", f"Risk '{risk.title}' created.")
