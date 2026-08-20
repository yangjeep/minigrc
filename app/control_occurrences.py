"""Control-occurrence generation and event projectors (issue #11).

A plain module — matching the shape of app/csv_import.py/app/vendor_flags.py/
app/requirements.py, not a framework. Builds on app/events.py (issue #22):
`ControlOccurrence`/`ControlOccurrenceEvidence` (app/models.py) are
canonical projections over the "control_occurrence" DomainEvent aggregate,
never written to directly outside the projector functions here.

See docs/superpowers/specs/2026-08-15-issue11-control-operations-lifecycle-design.md
§12 for the full design and reconciliation against the event-store contract.
"""

from __future__ import annotations

import calendar
import datetime
import json

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import (
    ControlOccurrence,
    ControlOccurrenceEvidence,
    ControlOccurrenceEvidenceArtifact,
    ControlPeriod,
    InternalControl,
    new_id,
)


def _parse_iso_datetime(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def _project_materialized(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        ControlOccurrence(
            id=event.aggregate_id,
            control_id=payload["control_id"],
            control_period_id=payload.get("control_period_id"),
            origin="generated",
            due_at=_parse_iso_datetime(payload.get("due_at")),
            responsible_person_id=payload.get("responsible_person_id"),
            scope_note="",
            evidence_note="",
            control_definition_version_id=payload.get("control_definition_version_id"),
            created_at=event.recorded_at,
            updated_at=event.recorded_at,
        )
    )
    # Explicit flush (session-wide autoflush=False, app/db.py): a later
    # event for this same aggregate — ControlOccurrencePerformed, in the
    # same append_and_project call or a later rebuild_projection pass —
    # calls session.get() on this row before anything else would trigger
    # a flush, and session.get() does not autoflush pending inserts first.
    session.flush()


def _project_recorded_manually(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        ControlOccurrence(
            id=event.aggregate_id,
            control_id=payload["control_id"],
            control_period_id=payload.get("control_period_id"),
            origin="manual",
            due_at=_parse_iso_datetime(payload.get("due_at")),
            scope_note=payload.get("scope_note", ""),
            evidence_note="",
            control_definition_version_id=payload.get("control_definition_version_id"),
            created_at=event.recorded_at,
            updated_at=event.recorded_at,
        )
    )
    session.flush()  # see _project_materialized's comment above


def _project_performed(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    occurrence = session.get(ControlOccurrence, event.aggregate_id)
    # occurred_at is the claimed performance time (business time — may be
    # backdated, see §12.3); recorded_at is when this claim was submitted.
    # Every submission (including corrections) overwrites these fields —
    # full history of every claim remains queryable via DomainEvent.
    occurrence.performed_at = event.occurred_at
    occurrence.performed_by_person_id = payload.get("performed_by_person_id")
    occurrence.scope_note = payload["scope_note"]
    occurrence.evidence_note = payload.get("evidence_note", "")
    occurrence.updated_at = event.recorded_at


def _project_evidence_linked(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        ControlOccurrenceEvidence(
            occurrence_id=event.aggregate_id, evidence_snapshot_id=payload["evidence_snapshot_id"]
        )
    )


def _project_evidence_artifact_linked(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        ControlOccurrenceEvidenceArtifact(
            occurrence_id=event.aggregate_id,
            evidence_artifact_version_id=payload["evidence_artifact_version_id"],
        )
    )


CONTROL_OCCURRENCE_PROJECTORS: dict[str, ProjectorFn] = {
    "ControlOccurrenceMaterialized": _project_materialized,
    "ControlOccurrenceRecordedManually": _project_recorded_manually,
    "ControlOccurrencePerformed": _project_performed,
    "ControlOccurrenceEvidenceLinked": _project_evidence_linked,
    "ControlOccurrenceEvidenceArtifactLinked": _project_evidence_artifact_linked,
}


def _reset_control_occurrences(session: Session) -> None:
    session.execute(delete(ControlOccurrenceEvidence))
    session.execute(delete(ControlOccurrenceEvidenceArtifact))
    session.execute(delete(ControlOccurrence))


def rebuild_control_occurrence_projection(session: Session) -> None:
    """Wipe and deterministically rebuild the ControlOccurrence/
    ControlOccurrenceEvidence/ControlOccurrenceEvidenceArtifact
    projections from DomainEvent history. Admin/ops recovery path —
    proves app/events.py's rebuild contract holds for this real domain,
    not just the generic infrastructure test. Call this after
    app/evidence_repository.py::rebuild_evidence_artifact_version_projection
    if both were reset, since that function's own reset also clears
    ControlOccurrenceEvidenceArtifact (a foreign-key ordering
    requirement, not this aggregate's own concern) — see its docstring.
    """
    rebuild_projection(
        session,
        CONTROL_OCCURRENCE_PROJECTORS,
        aggregate_type="control_occurrence",
        reset=_reset_control_occurrences,
    )


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp to the target month's actual last day (e.g. Jan 31 + 1 -> Feb 28).
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _due_at_for_date(d: datetime.date) -> datetime.datetime:
    # UTC, end of the computed calendar day — stated convention, matching
    # this app's all-UTC timestamps and its existing lack of any
    # per-organization timezone setting.
    return datetime.datetime.combine(d, datetime.time.max, tzinfo=datetime.UTC)


def _compute_due_dates(
    session: Session, control: InternalControl, period: ControlPeriod | None, horizon_months: int
) -> list[datetime.date]:
    interval = control.cadence_interval_months
    if period is not None:
        dates = []
        current = period.starts_on
        while current <= period.ends_on:
            dates.append(current)
            current = _add_months(current, interval)
        return dates

    # Period-less rolling path — the primary, always-available path so a
    # ControlPeriod is never a precondition for ordinary cadence tracking.
    # Starts from the later of "the last existing period-less occurrence's
    # due date for this control" or "today," for horizon_months ahead.
    last_due = session.scalar(
        select(func.max(ControlOccurrence.due_at)).where(
            ControlOccurrence.control_id == control.id,
            ControlOccurrence.control_period_id.is_(None),
        )
    )
    today = datetime.date.today()
    horizon_end = _add_months(today, horizon_months)
    current = _add_months(last_due.date(), interval) if last_due is not None else today

    dates = []
    while current <= horizon_end:
        dates.append(current)
        current = _add_months(current, interval)
    return dates


def generate_occurrences(
    session: Session,
    control: InternalControl,
    *,
    period: ControlPeriod | None = None,
    horizon_months: int = 12,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> list[ControlOccurrence]:
    """Materialize expected occurrences for a calendar-cadence control.

    No-op for event_driven/unconfigured controls (nothing to materialize —
    event-driven occurrences are only ever created reactively via
    `record_occurrence_manually`). Idempotent per due date via
    `app/events.py`'s idempotency_key mechanism — safe to call repeatedly
    or concurrently; a due date that already has an occurrence is returned
    unchanged, never duplicated. Returns every occurrence for the computed
    due-date range, whether just-created or already existing.
    """
    # `not control.cadence_interval_months` alone only filters None/0 — a
    # negative value would otherwise sail through into _compute_due_dates
    # and walk backward forever (defense in depth: the DB CHECK constraint
    # ck_control_cadence_interval_positive should make this unreachable in
    # practice, but a domain-level guard shouldn't rely solely on that).
    if control.cadence_type != "calendar" or not control.cadence_interval_months:
        return []
    if control.cadence_interval_months <= 0:
        return []
    if period is not None and period.status != "active":
        raise ValueError("Cannot generate occurrences for a control period that is not active")

    period_id = period.id if period is not None else None
    due_dates = _compute_due_dates(session, control, period, horizon_months)
    # Snapshot once per call, not once per due_date: every occurrence
    # generated by this call is generated "now," against whichever
    # control-definition version is effective right now (issue #42) —
    # NULL if the control has no effective version yet (an honest gap,
    # not a fabricated reference; see design doc §4).
    #
    # Refresh first: this app's session factory uses
    # expire_on_commit=False, so a long-lived `control` object's
    # `.versions` relationship — which effective_version reads — can
    # be stale (e.g. cached empty from an earlier
    # draft_control_definition_version call, before any version
    # existed) unless refreshed here. Same bug class as
    # app/connectors/google_drive_capture.py::run_google_drive_capture
    # (#28) and app/control_definition_lifecycle.py::draft_control_definition_version.
    session.refresh(control)
    effective_version = control.effective_version
    control_definition_version_id = effective_version.id if effective_version is not None else None

    occurrences = []
    for due_date in due_dates:
        due_at = _due_at_for_date(due_date)
        idempotency_key = (
            f"control_occurrence:materialize:{control.id}:{period_id or 'none'}:{due_date.isoformat()}"
        )
        event = append_and_project(
            session,
            CONTROL_OCCURRENCE_PROJECTORS,
            aggregate_type="control_occurrence",
            aggregate_id=new_id(),
            event_type="ControlOccurrenceMaterialized",
            payload={
                "control_id": control.id,
                "control_period_id": period_id,
                "due_at": due_at.isoformat(),
                "responsible_person_id": control.owner_person_id,
                "cadence_interval_months": control.cadence_interval_months,
                "control_definition_version_id": control_definition_version_id,
            },
            idempotency_key=idempotency_key,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        occurrences.append(session.get(ControlOccurrence, event.aggregate_id))
    return occurrences


def record_occurrence_manually(
    session: Session,
    control: InternalControl,
    *,
    control_period_id: str | None = None,
    due_at: datetime.datetime | None = None,
    scope_note: str = "",
    actor_type: str = "user",
    actor_id: str | None = None,
) -> ControlOccurrence:
    """Record a manual/event-driven occurrence. No idempotency_key —
    unlike generation, there is no natural deterministic key for a
    user-directed single creation; if `due_at` collides with an existing
    occurrence's (control_id, control_period_id, due_at), the caller must
    catch the resulting IntegrityError from the projection's
    UniqueConstraint (see app/routers/control_occurrences.py)."""
    session.refresh(control)  # see generate_occurrences' comment on why
    effective_version = control.effective_version
    event = append_and_project(
        session,
        CONTROL_OCCURRENCE_PROJECTORS,
        aggregate_type="control_occurrence",
        aggregate_id=new_id(),
        event_type="ControlOccurrenceRecordedManually",
        payload={
            "control_id": control.id,
            "control_period_id": control_period_id,
            "due_at": due_at.isoformat() if due_at else None,
            "scope_note": scope_note,
            "control_definition_version_id": effective_version.id if effective_version is not None else None,
        },
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return session.get(ControlOccurrence, event.aggregate_id)


def perform_occurrence(
    session: Session,
    occurrence: ControlOccurrence,
    *,
    occurred_at: datetime.datetime,
    performed_by_person_id: str | None,
    scope_note: str,
    evidence_note: str,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> ControlOccurrence:
    """Record a performance claim. Safe to call more than once for the
    same occurrence (a correction) — every call appends a new immutable
    event; the projection reflects the latest, but every prior claim
    remains queryable via DomainEvent. Caller is responsible for
    validating `occurred_at` is not in the future and for pre-filling
    `scope_note` from the occurrence's current value when the caller's
    form didn't intend to change it (see the route for why)."""
    append_and_project(
        session,
        CONTROL_OCCURRENCE_PROJECTORS,
        aggregate_type="control_occurrence",
        aggregate_id=occurrence.id,
        event_type="ControlOccurrencePerformed",
        payload={
            "performed_by_person_id": performed_by_person_id,
            "scope_note": scope_note,
            "evidence_note": evidence_note,
        },
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    # The projector mutates `occurrence` in place (same identity-mapped
    # object `session.get` returned) — no refresh needed.
    return occurrence


def link_evidence(
    session: Session,
    occurrence: ControlOccurrence,
    evidence_snapshot_id: str,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> None:
    """Link an EvidenceSnapshot to an occurrence. The caller must flush
    and catch IntegrityError for a duplicate (occurrence_id,
    evidence_snapshot_id) link — this is a single-item operation, not a
    batch loop, so that pattern is safe here (see
    app/routers/controls.py::add_mapping for the precedent)."""
    append_and_project(
        session,
        CONTROL_OCCURRENCE_PROJECTORS,
        aggregate_type="control_occurrence",
        aggregate_id=occurrence.id,
        event_type="ControlOccurrenceEvidenceLinked",
        payload={"evidence_snapshot_id": evidence_snapshot_id},
        actor_type=actor_type,
        actor_id=actor_id,
    )


def link_evidence_artifact_version(
    session: Session,
    occurrence: ControlOccurrence,
    evidence_artifact_version_id: str,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> None:
    """Link an EvidenceArtifactVersion (issue #32) to an occurrence —
    mirrors link_evidence exactly. The caller must flush and catch
    IntegrityError for a duplicate (occurrence_id,
    evidence_artifact_version_id) link, same as link_evidence."""
    append_and_project(
        session,
        CONTROL_OCCURRENCE_PROJECTORS,
        aggregate_type="control_occurrence",
        aggregate_id=occurrence.id,
        event_type="ControlOccurrenceEvidenceArtifactLinked",
        payload={"evidence_artifact_version_id": evidence_artifact_version_id},
        actor_type=actor_type,
        actor_id=actor_id,
    )
