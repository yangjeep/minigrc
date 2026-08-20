"""Occurrence-level routes (issue #11): viewing one ControlOccurrence and
recording a performance claim against it.

Kept separate from app/routers/controls.py because these routes are
addressed by the occurrence's own id, not nested under `/controls/{id}` —
same reasoning as app/routers/evidence.py being its own router rather than
folded into controls.py. `require_write_access`, not `require_admin`:
recording your own occurrence is individual record-keeping, the same category as
`update_assessment` (see
docs/superpowers/specs/2026-08-15-issue11-control-operations-lifecycle-design.md
§6).
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.control_occurrences import link_evidence, perform_occurrence
from app.deps import get_db, require_login, require_write_access, verify_csrf
from app.flash import redirect_with_flash
from app.history import get_event_history
from app.models import ControlOccurrence, ControlOccurrenceEvidence, EvidenceSnapshot, Person

router = APIRouter(prefix="/occurrences", tags=["occurrences"], dependencies=[Depends(require_login)])


@router.get("/{occurrence_id}")
def view_occurrence(occurrence_id: str, request: Request, db: Session = Depends(get_db)):
    occurrence = db.get(ControlOccurrence, occurrence_id)
    if occurrence is None:
        raise HTTPException(status_code=404, detail="Occurrence not found")

    people = db.scalars(select(Person).order_by(Person.display_name, Person.email)).all()

    linked_evidence_ids = {
        e.evidence_snapshot_id
        for e in db.scalars(
            select(ControlOccurrenceEvidence).where(ControlOccurrenceEvidence.occurrence_id == occurrence.id)
        ).all()
    }
    available_evidence = db.scalars(
        select(EvidenceSnapshot).where(EvidenceSnapshot.id.not_in(linked_evidence_ids or [""]))
    ).all()

    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    # Pre-fill from the existing claim when correcting a performance
    # record, not from today — otherwise re-opening the form only to fix
    # scope_note would silently shift occurred_at (an immutable event
    # field) to today on submission (adversarial review finding, #11).
    performed_on_default = occurrence.performed_at.date().isoformat() if occurrence.performed_at else today

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "occurrences/detail.html",
        {
            "occurrence": occurrence,
            "people": people,
            "available_evidence": available_evidence,
            "today": today,
            "performed_on_default": performed_on_default,
        },
    )


@router.get("/{occurrence_id}/history")
def view_occurrence_history(occurrence_id: str, request: Request, db: Session = Depends(get_db)):
    """Issue #45: the chronological event timeline for one control
    occurrence, derived from the immutable event store — read-only, a
    second representative domain alongside policies/{id}/history."""
    occurrence = db.get(ControlOccurrence, occurrence_id)
    if occurrence is None:
        raise HTTPException(status_code=404, detail="Occurrence not found")

    events = get_event_history(db, aggregate_type="control_occurrence", aggregate_id=occurrence.id)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "occurrences/history.html", {"occurrence": occurrence, "events": events}
    )


@router.post("/{occurrence_id}/perform")
def perform(
    occurrence_id: str,
    performed_on: str = Form(...),
    performed_by_person_id: str = Form(""),
    scope_note: str = Form(""),
    evidence_note: str = Form(""),
    evidence_snapshot_id: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    occurrence = db.get(ControlOccurrence, occurrence_id)
    if occurrence is None:
        raise HTTPException(status_code=404, detail="Occurrence not found")

    detail_url = f"/occurrences/{occurrence.id}"

    try:
        performed_date = datetime.date.fromisoformat(performed_on)
    except ValueError:
        return redirect_with_flash(detail_url, "Performed date must be a valid date.", kind="error")

    today = datetime.datetime.now(datetime.UTC).date()
    if performed_date > today:
        return redirect_with_flash(detail_url, "Performed date cannot be in the future.", kind="error")

    performed_by_person_id = performed_by_person_id.strip() or None
    performer = db.get(Person, performed_by_person_id) if performed_by_person_id else None
    if performed_by_person_id and performer is None:
        return redirect_with_flash(detail_url, "Selected person not found.", kind="error")

    evidence_snapshot_id = evidence_snapshot_id.strip() or None
    evidence = db.get(EvidenceSnapshot, evidence_snapshot_id) if evidence_snapshot_id else None
    if evidence_snapshot_id and evidence is None:
        return redirect_with_flash(detail_url, "Selected evidence snapshot not found.", kind="error")

    occurred_at = datetime.datetime.combine(performed_date, datetime.time.max, tzinfo=datetime.UTC)
    perform_occurrence(
        db,
        occurrence,
        occurred_at=occurred_at,
        performed_by_person_id=performer.id if performer is not None else None,
        scope_note=scope_note.strip(),
        evidence_note=evidence_note.strip(),
        actor_type="user",
        actor_id=user.id,
    )

    if evidence is not None:
        try:
            # A SAVEPOINT (not a plain db.rollback()): perform_occurrence's
            # event above is already pending in this same request-scoped
            # transaction (app/deps.py::get_db commits once at teardown) —
            # a bare rollback here would discard that already-successful
            # mutation too. Same pattern as app/events.py's own
            # idempotency-slot retry.
            with db.begin_nested():
                link_evidence(db, occurrence, evidence.id, actor_type="user", actor_id=user.id)
                db.flush()
        except IntegrityError:
            return redirect_with_flash(
                detail_url, "That evidence is already linked to this occurrence.", kind="error"
            )

    return redirect_with_flash(detail_url, "Occurrence recorded as performed.")
