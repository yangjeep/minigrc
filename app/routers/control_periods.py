"""Control-period routes (issue #11).

Structural, cross-control, period-wide actions (create/close/bulk
generate) are `require_admin` — the same category as connection config
and "run checks now" (app/routers/aws_connector.py). Viewing a period
stays `require_login`, matching every other read route in this app.
`ControlPeriod` itself is a plain mutable row + `record_audit_event`, not
event-sourced — see
docs/superpowers/specs/2026-08-15-issue11-control-operations-lifecycle-design.md
§12.1 for why.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.control_occurrences import generate_occurrences
from app.deps import get_db, require_admin, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.models import ControlOccurrence, ControlPeriod, InternalControl, utcnow

router = APIRouter(prefix="/control-periods", tags=["control-periods"], dependencies=[Depends(require_login)])


@router.get("")
def list_control_periods(request: Request, db: Session = Depends(get_db)):
    periods = db.scalars(select(ControlPeriod).order_by(ControlPeriod.starts_on.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "control_periods/list.html", {"periods": periods})


@router.post("")
def create_control_period(
    request: Request,
    name: str = Form(...),
    starts_on: str = Form(...),
    ends_on: str = Form(...),
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    name = name.strip()
    if not name:
        return redirect_with_flash("/control-periods", "Name is required.", kind="error")

    try:
        starts_on_date = datetime.date.fromisoformat(starts_on)
        ends_on_date = datetime.date.fromisoformat(ends_on)
    except ValueError:
        return redirect_with_flash("/control-periods", "Dates must be valid calendar dates.", kind="error")

    if ends_on_date <= starts_on_date:
        return redirect_with_flash("/control-periods", "End date must be after the start date.", kind="error")

    period = ControlPeriod(name=name, starts_on=starts_on_date, ends_on=ends_on_date, created_by=admin.email)
    db.add(period)
    db.flush()

    record_audit_event(
        db,
        entity_type="control_period",
        entity_id=period.id,
        action="create",
        detail=f"Created control period '{period.name}' ({starts_on_date} to {ends_on_date})",
        actor=admin.email,
    )
    return redirect_with_flash(f"/control-periods/{period.id}", "Control period created.")


@router.get("/{period_id}")
def view_control_period(period_id: str, request: Request, db: Session = Depends(get_db)):
    period = db.get(ControlPeriod, period_id)
    if period is None:
        raise HTTPException(status_code=404, detail="Control period not found")

    occurrences = db.scalars(
        select(ControlOccurrence)
        .where(ControlOccurrence.control_period_id == period.id)
        .order_by(ControlOccurrence.due_at)
    ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "control_periods/detail.html", {"period": period, "occurrences": occurrences}
    )


@router.post("/{period_id}/activate")
def activate_control_period(
    period_id: str,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    """Promote a "planned" period to "active" so it accepts occurrence
    generation/association. Not in the original route table (§6) — added
    during implementation: without it, a period created in the default
    "planned" status (§3.1) has no route ever making it usable, since
    generation explicitly requires status="active"."""
    period = db.get(ControlPeriod, period_id)
    if period is None:
        raise HTTPException(status_code=404, detail="Control period not found")
    if period.status != "planned":
        return redirect_with_flash(
            f"/control-periods/{period.id}", "Only a planned period can be activated.", kind="error"
        )

    period.status = "active"
    record_audit_event(
        db,
        entity_type="control_period",
        entity_id=period.id,
        action="activate",
        detail=f"Activated control period '{period.name}'",
        actor=admin.email,
    )
    return redirect_with_flash(f"/control-periods/{period.id}", "Control period activated.")


@router.post("/{period_id}/close")
def close_control_period(
    period_id: str,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    period = db.get(ControlPeriod, period_id)
    if period is None:
        raise HTTPException(status_code=404, detail="Control period not found")

    if period.status == "closed":
        return redirect_with_flash(f"/control-periods/{period.id}", "That period is already closed.")

    period.status = "closed"
    period.closed_at = utcnow()
    record_audit_event(
        db,
        entity_type="control_period",
        entity_id=period.id,
        action="close",
        detail=f"Closed control period '{period.name}'",
        actor=admin.email,
    )
    return redirect_with_flash(f"/control-periods/{period.id}", "Control period closed.")


@router.post("/{period_id}/generate-occurrences")
def generate_period_occurrences(
    period_id: str,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    period = db.get(ControlPeriod, period_id)
    if period is None:
        raise HTTPException(status_code=404, detail="Control period not found")
    if period.status != "active":
        return redirect_with_flash(
            f"/control-periods/{period.id}",
            "Occurrences can only be generated for an active period.",
            kind="error",
        )

    controls = db.scalars(select(InternalControl).where(InternalControl.cadence_type == "calendar")).all()
    total = 0
    for control in controls:
        occurrences = generate_occurrences(db, control, period=period, actor_type="user", actor_id=admin.id)
        total += len(occurrences)

    return redirect_with_flash(
        f"/control-periods/{period.id}",
        f"Generated/confirmed {total} occurrence(s) across {len(controls)} control(s).",
    )
