from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.models import (
    EVIDENCE_STATUSES,
    EvidenceControlMapping,
    EvidenceRequirementMapping,
    EvidenceSnapshot,
    FrameworkRequirement,
    InternalControl,
)

router = APIRouter(prefix="/evidence", tags=["evidence"], dependencies=[Depends(require_login)])


@router.get("")
def list_evidence(request: Request, source_type: str = "", status: str = "", db: Session = Depends(get_db)):
    query = select(EvidenceSnapshot)
    if source_type:
        query = query.where(EvidenceSnapshot.source_type == source_type)
    if status in EVIDENCE_STATUSES:
        query = query.where(EvidenceSnapshot.status == status)
    snapshots = db.scalars(query.order_by(EvidenceSnapshot.collected_at.desc())).all()

    source_types = sorted({s.source_type for s in db.scalars(select(EvidenceSnapshot)).all()})
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "evidence/list.html",
        {
            "snapshots": snapshots,
            "source_types": source_types,
            "statuses": EVIDENCE_STATUSES,
            "selected_source_type": source_type,
            "selected_status": status,
        },
    )


@router.get("/{evidence_id}")
def view_evidence(evidence_id: str, request: Request, db: Session = Depends(get_db)):
    snapshot = db.get(EvidenceSnapshot, evidence_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Evidence snapshot not found")

    mapped_requirement_ids = {m.requirement_id for m in snapshot.requirement_mappings}
    mapped_control_ids = {m.control_id for m in snapshot.control_mappings}
    available_requirements = db.scalars(
        select(FrameworkRequirement).where(FrameworkRequirement.id.not_in(mapped_requirement_ids or [""]))
    ).all()
    available_controls = db.scalars(
        select(InternalControl).where(InternalControl.id.not_in(mapped_control_ids or [""]))
    ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "evidence/detail.html",
        {
            "snapshot": snapshot,
            "available_requirements": available_requirements,
            "available_controls": available_controls,
        },
    )


@router.post("/{evidence_id}/map-requirement")
def map_requirement(
    evidence_id: str,
    request: Request,
    requirement_id: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    snapshot = db.get(EvidenceSnapshot, evidence_id)
    requirement = db.get(FrameworkRequirement, requirement_id)
    if snapshot is None or requirement is None:
        raise HTTPException(status_code=404, detail="Evidence snapshot or requirement not found")

    db.add(EvidenceRequirementMapping(evidence_snapshot_id=snapshot.id, requirement_id=requirement.id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect_with_flash(
            f"/evidence/{evidence_id}", "That requirement is already mapped to this evidence.", kind="error"
        )

    record_audit_event(
        db,
        entity_type="evidence_snapshot",
        entity_id=snapshot.id,
        action="map_requirement",
        detail=f"Mapped evidence '{snapshot.title}' to requirement '{requirement.reference_code}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/evidence/{evidence_id}", "Requirement mapped.")


@router.post("/{evidence_id}/map-control")
def map_control(
    evidence_id: str,
    request: Request,
    control_id: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    snapshot = db.get(EvidenceSnapshot, evidence_id)
    control = db.get(InternalControl, control_id)
    if snapshot is None or control is None:
        raise HTTPException(status_code=404, detail="Evidence snapshot or control not found")

    db.add(EvidenceControlMapping(evidence_snapshot_id=snapshot.id, control_id=control.id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect_with_flash(
            f"/evidence/{evidence_id}", "That control is already mapped to this evidence.", kind="error"
        )

    record_audit_event(
        db,
        entity_type="evidence_snapshot",
        entity_id=snapshot.id,
        action="map_control",
        detail=f"Mapped evidence '{snapshot.title}' to control '{control.name}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/evidence/{evidence_id}", "Control mapped.")
