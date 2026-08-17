from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.control_occurrences import generate_occurrences, record_occurrence_manually
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.models import (
    CADENCE_TYPES,
    CONTROL_STATUSES,
    REVIEW_FREQUENCIES,
    ControlOccurrence,
    ControlPeriod,
    ControlRequirementMapping,
    FrameworkRequirement,
    InternalControl,
    Person,
)
from app.registers.config import FieldSpec, RegisterConfig
from app.registers.router import build_register_router

router = APIRouter(prefix="/controls", tags=["controls"], dependencies=[Depends(require_login)])

CONTROLS_REGISTER_CONFIG = RegisterConfig(
    name="controls",
    model=InternalControl,
    entity_type="control",
    order_by=InternalControl.name,
    fields=(
        FieldSpec(name="name", type="text", required=True, max_length=255),
        FieldSpec(name="owner", type="text", max_length=255),
        FieldSpec(name="status", type="enum", choices=CONTROL_STATUSES),
        FieldSpec(name="review_frequency", type="enum", choices=REVIEW_FREQUENCIES),
        FieldSpec(name="description", type="text"),
        FieldSpec(name="cadence_type", type="enum", choices=CADENCE_TYPES),
        FieldSpec(name="cadence_interval_months", type="number"),
        FieldSpec(
            name="mapped_requirements", type="number", read_only=True, compute=lambda c: len(c.mappings)
        ),
    ),
)

controls_register_router = build_register_router(CONTROLS_REGISTER_CONFIG)


@router.get("")
def list_controls(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "controls/list.html",
        {
            "control_statuses": CONTROL_STATUSES,
            "review_frequencies": REVIEW_FREQUENCIES,
            "cadence_types": CADENCE_TYPES,
        },
    )


@router.get("/{control_id}")
def view_control(control_id: str, request: Request, db: Session = Depends(get_db)):
    control = db.get(InternalControl, control_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Control not found")

    mapped_requirement_ids = {m.requirement_id for m in control.mappings}
    available_requirements = db.scalars(
        select(FrameworkRequirement).where(FrameworkRequirement.id.not_in(mapped_requirement_ids or [""]))
    ).all()

    occurrences = db.scalars(
        select(ControlOccurrence)
        .where(ControlOccurrence.control_id == control.id)
        .order_by(ControlOccurrence.due_at.desc())
    ).all()
    # Naive UTC "now" for comparison — due_at may come back tz-naive after
    # a real DB round-trip (SQLite DateTime without timezone=True; see
    # app/vendor_flags.py's identical confirmed_at.tzinfo handling).
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)

    def _is_overdue(o: ControlOccurrence) -> bool:
        if o.performed_at is not None or o.due_at is None:
            return False
        due_at = o.due_at.replace(tzinfo=None) if o.due_at.tzinfo is not None else o.due_at
        return due_at < now

    # Missing/overdue is computed here, not stored — same "compute and
    # expose, never persist a derived flag" precedent as app/vendor_flags.py.
    overdue = [o for o in occurrences if _is_overdue(o)]
    upcoming = [o for o in occurrences if o.performed_at is None and not _is_overdue(o)]
    completed = [o for o in occurrences if o.performed_at is not None]

    active_periods = db.scalars(
        select(ControlPeriod).where(ControlPeriod.status == "active").order_by(ControlPeriod.starts_on)
    ).all()
    people = db.scalars(select(Person).order_by(Person.display_name, Person.email)).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "controls/detail.html",
        {
            "control": control,
            "available_requirements": available_requirements,
            "overdue_occurrences": overdue,
            "upcoming_occurrences": upcoming,
            "completed_occurrences": completed,
            "active_periods": active_periods,
            "people": people,
        },
    )


@router.post("/{control_id}/owner")
def set_control_owner(
    control_id: str,
    request: Request,
    owner_person_id: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    """Set the control's owner_person_id (issue #11) — a real FK, distinct
    from the pre-existing freeform `owner` text field, snapshotted onto
    each generated occurrence's responsible_person_id. Its own small form
    rather than a CONTROLS_REGISTER_CONFIG field: the generic register
    grid's FieldSpec has no person-picker type (see
    app/routers/vendor_systems.py::link_roster_row for the same pattern
    used for VendorSystem's person-FK fields)."""
    control = db.get(InternalControl, control_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Control not found")

    owner_person_id = owner_person_id.strip() or None
    person = db.get(Person, owner_person_id) if owner_person_id else None
    if owner_person_id and person is None:
        return redirect_with_flash(f"/controls/{control.id}", "Selected person not found.", kind="error")

    control.owner_person_id = person.id if person is not None else None
    record_audit_event(
        db,
        entity_type="control",
        entity_id=control.id,
        action="set_owner_person",
        detail=f"Set control '{control.name}' owner_person_id to '{person.email if person else 'none'}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/controls/{control.id}", "Control owner updated.")


def _parse_due_on(value: str) -> datetime.datetime | None:
    value = value.strip()
    if not value:
        return None
    parsed = datetime.date.fromisoformat(value)
    # UTC end-of-day convention — matches app/control_occurrences.py's
    # _due_at_for_date, used for the same generated-occurrence due dates.
    return datetime.datetime.combine(parsed, datetime.time.max, tzinfo=datetime.UTC)


@router.post("/{control_id}/occurrences/generate")
def generate_control_occurrences(
    control_id: str,
    db: Session = Depends(get_db),
    user=Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    """Self-serve rolling generation for one control (period-less path) —
    require_login, not require_admin: unlike bulk cross-control generation
    (app/routers/control_periods.py::generate_period_occurrences), this
    only affects the one control the caller is already looking at."""
    control = db.get(InternalControl, control_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Control not found")

    occurrences = generate_occurrences(db, control, actor_type="user", actor_id=user.id)
    return redirect_with_flash(
        f"/controls/{control.id}", f"Generated/confirmed {len(occurrences)} occurrence(s)."
    )


@router.post("/{control_id}/occurrences")
def create_control_occurrence(
    control_id: str,
    control_period_id: str = Form(""),
    due_on: str = Form(""),
    scope_note: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    control = db.get(InternalControl, control_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Control not found")

    control_period_id = control_period_id.strip() or None
    period = None
    if control_period_id is not None:
        period = db.get(ControlPeriod, control_period_id)
        if period is None:
            raise HTTPException(status_code=404, detail="Control period not found")
        if period.status != "active":
            return redirect_with_flash(
                f"/controls/{control.id}",
                "Occurrences can only be associated with an active control period.",
                kind="error",
            )

    try:
        due_at = _parse_due_on(due_on)
    except ValueError:
        return redirect_with_flash(f"/controls/{control.id}", "Due date must be a valid date.", kind="error")

    try:
        record_occurrence_manually(
            db,
            control,
            control_period_id=control_period_id,
            due_at=due_at,
            scope_note=scope_note.strip(),
            actor_type="user",
            actor_id=user.id,
        )
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect_with_flash(
            f"/controls/{control.id}",
            "An occurrence with that due date already exists for this control/period.",
            kind="error",
        )

    return redirect_with_flash(f"/controls/{control.id}", "Occurrence recorded.")


@router.post("/{control_id}/mappings")
def add_mapping(
    request: Request,
    control_id: str,
    requirement_id: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    control = db.get(InternalControl, control_id)
    requirement = db.get(FrameworkRequirement, requirement_id)
    if control is None or requirement is None:
        raise HTTPException(status_code=404, detail="Control or requirement not found")

    db.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect_with_flash(
            f"/controls/{control_id}", "That requirement is already mapped to this control.", kind="error"
        )

    record_audit_event(
        db,
        entity_type="control",
        entity_id=control.id,
        action="map_requirement",
        detail=f"Mapped control '{control.name}' to requirement '{requirement.reference_code}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/controls/{control_id}", "Requirement mapped.")
