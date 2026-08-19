"""Compliance program scope: event-sourced declaration of what is currently
in-audit-scope (issue #30).

`ComplianceScope` (app/models.py) is a canonical projection over the
"compliance_scope" DomainEvent aggregate, never written to directly outside
the projector functions here — same discipline as
app/control_occurrences.py/app/evidence_repository.py. Singleton per
deployment (RULES.md §1.11): the first `define_or_revise_scope` call creates
it (`ComplianceScopeDefined`); every later call revises the same aggregate in
place (`ComplianceScopeRevised`), matching `ControlOccurrence`'s "one
long-lived aggregate, projection updated in place" shape rather than
`EvidenceArtifactVersion`'s "every change is a new row" shape — see
docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-design.md
§2.5 for why.
"""

from __future__ import annotations

import datetime
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import TRUST_SERVICE_CATEGORIES, ComplianceScope, new_id


class InvalidComplianceScopeError(ValueError):
    """Raised when caller-supplied scope fields fail validation."""


def _normalize_categories(categories: list[str] | tuple[str, ...] | None) -> str:
    selected = set(categories or ())
    unknown = selected - set(TRUST_SERVICE_CATEGORIES)
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise InvalidComplianceScopeError(f"Unknown Trust Services Category value(s): {joined}")
    # "security" is mandatory in every SOC 2 report — never optional, and
    # never trusted from caller input alone (server-side enforced here, not
    # merely defaulted in a form).
    selected.add("security")
    # Deterministic ordering (TRUST_SERVICE_CATEGORIES' own order), not
    # set/insertion order, so stored payloads/exports are reproducible.
    return ",".join(category for category in TRUST_SERVICE_CATEGORIES if category in selected)


def _validate_period(starts_on: datetime.date | None, ends_on: datetime.date | None) -> None:
    if starts_on is not None and ends_on is not None and ends_on < starts_on:
        raise InvalidComplianceScopeError("audit_period_ends_on must not be before audit_period_starts_on")


def _parse_date(value: str | None) -> datetime.date | None:
    return datetime.date.fromisoformat(value) if value else None


def _project_defined(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        ComplianceScope(
            id=event.aggregate_id,
            service_description=payload["service_description"],
            trust_service_categories=payload["trust_service_categories"],
            audit_period_starts_on=_parse_date(payload.get("audit_period_starts_on")),
            audit_period_ends_on=_parse_date(payload.get("audit_period_ends_on")),
            data_categories=payload["data_categories"],
            locations=payload["locations"],
            exclusions=payload["exclusions"],
            exclusions_rationale=payload["exclusions_rationale"],
            revision=1,
            created_at=event.recorded_at,
            updated_at=event.recorded_at,
        )
    )
    # Explicit flush (session-wide autoflush=False, app/db.py) — matches
    # app/control_occurrences.py::_project_materialized's comment: a
    # ComplianceScopeRevised projector in the same request (there is none
    # possible in the same call, but a later rebuild_projection pass over
    # the same aggregate needs session.get() to see this row).
    session.flush()


def _project_revised(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    scope = session.get(ComplianceScope, event.aggregate_id)
    scope.service_description = payload["service_description"]
    scope.trust_service_categories = payload["trust_service_categories"]
    scope.audit_period_starts_on = _parse_date(payload.get("audit_period_starts_on"))
    scope.audit_period_ends_on = _parse_date(payload.get("audit_period_ends_on"))
    scope.data_categories = payload["data_categories"]
    scope.locations = payload["locations"]
    scope.exclusions = payload["exclusions"]
    scope.exclusions_rationale = payload["exclusions_rationale"]
    scope.revision += 1
    scope.updated_at = event.recorded_at


COMPLIANCE_SCOPE_PROJECTORS: dict[str, ProjectorFn] = {
    "ComplianceScopeDefined": _project_defined,
    "ComplianceScopeRevised": _project_revised,
}


def _reset_compliance_scopes(session: Session) -> None:
    session.execute(delete(ComplianceScope))


def rebuild_compliance_scope_projection(session: Session) -> None:
    """Wipe and deterministically rebuild the ComplianceScope projection
    from DomainEvent history — proves app/events.py's rebuild contract holds
    for this aggregate, same shape as
    app/control_occurrences.py::rebuild_control_occurrence_projection."""
    rebuild_projection(
        session,
        COMPLIANCE_SCOPE_PROJECTORS,
        aggregate_type="compliance_scope",
        reset=_reset_compliance_scopes,
    )


def get_compliance_scope(session: Session) -> ComplianceScope | None:
    return session.scalar(select(ComplianceScope).limit(1))


def define_or_revise_scope(
    session: Session,
    *,
    service_description: str = "",
    trust_service_categories: list[str] | tuple[str, ...] | None = None,
    audit_period_starts_on: datetime.date | None = None,
    audit_period_ends_on: datetime.date | None = None,
    data_categories: str = "",
    locations: str = "",
    exclusions: str = "",
    exclusions_rationale: str = "",
    actor_type: str = "user",
    actor_id: str | None = None,
) -> ComplianceScope:
    """Define the scope if none exists yet, or revise the existing one.

    No idempotency_key: unlike control-occurrence generation, there is no
    natural deterministic key for a user-directed scope edit — every call
    is a genuine new fact ("the org declared this scope as of now"), same
    reasoning as app/control_occurrences.py::record_occurrence_manually.
    """
    _validate_period(audit_period_starts_on, audit_period_ends_on)
    payload = {
        "service_description": service_description,
        "trust_service_categories": _normalize_categories(trust_service_categories),
        "audit_period_starts_on": audit_period_starts_on.isoformat() if audit_period_starts_on else None,
        "audit_period_ends_on": audit_period_ends_on.isoformat() if audit_period_ends_on else None,
        "data_categories": data_categories,
        "locations": locations,
        "exclusions": exclusions,
        "exclusions_rationale": exclusions_rationale,
    }
    existing = get_compliance_scope(session)
    aggregate_id = existing.id if existing is not None else new_id()
    event_type = "ComplianceScopeRevised" if existing is not None else "ComplianceScopeDefined"
    stored_event = append_and_project(
        session,
        COMPLIANCE_SCOPE_PROJECTORS,
        aggregate_type="compliance_scope",
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return session.get(ComplianceScope, stored_event.aggregate_id)
