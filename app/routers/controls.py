from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db
from app.models import ControlRequirementMapping, FrameworkRequirement, InternalControl

router = APIRouter(prefix="/controls", tags=["controls"])


@router.get("")
def list_controls(request: Request, db: Session = Depends(get_db)):
    controls = db.scalars(select(InternalControl)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "controls/list.html", {"controls": controls})


@router.get("/{control_id}")
def view_control(control_id: str, request: Request, db: Session = Depends(get_db)):
    control = db.get(InternalControl, control_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Control not found")

    mapped_requirement_ids = {m.requirement_id for m in control.mappings}
    available_requirements = db.scalars(
        select(FrameworkRequirement).where(FrameworkRequirement.id.not_in(mapped_requirement_ids or [""]))
    ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "controls/detail.html",
        {"control": control, "available_requirements": available_requirements},
    )


@router.post("/{control_id}/mappings")
def add_mapping(
    control_id: str,
    requirement_id: str = Form(...),
    db: Session = Depends(get_db),
):
    control = db.get(InternalControl, control_id)
    requirement = db.get(FrameworkRequirement, requirement_id)
    if control is None or requirement is None:
        raise HTTPException(status_code=404, detail="Control or requirement not found")

    db.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
    record_audit_event(
        db,
        entity_type="control",
        entity_id=control.id,
        action="map_requirement",
        detail=f"Mapped control '{control.name}' to requirement '{requirement.reference_code}'",
    )
    return RedirectResponse(url=f"/controls/{control_id}", status_code=303)
