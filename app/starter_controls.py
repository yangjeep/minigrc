"""Starter-control generation for guided onboarding (issue #30).

Ensures every `FrameworkRequirement` under a given framework has at least
one mapped `InternalControl`, creating clearly-labeled placeholder controls
for any requirement left uncovered. Idempotent and additive by
construction — a requirement already mapped (via the demo seed, a CSV
import, a prior onboarding run, or manual creation) is left untouched, so
re-running only fills the remaining gap. See
docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-design.md
§5. Reuses the plain-row + `record_audit_event` shape
`app/framework_catalog.py::reconcile_system_catalogs` and
`app/seed.py::seed_if_empty` already use for the same kind of bootstrap
action — `InternalControl` design is not event-sourced (only
`ControlOccurrence` performance facts are, issue #11), so this introduces no
new precedent.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import ControlRequirementMapping, Framework, FrameworkRequirement, InternalControl


def generate_starter_controls_for_framework(
    session: Session, framework: Framework, *, actor: str = "system"
) -> list[InternalControl]:
    """Create one placeholder InternalControl for every requirement under
    `framework` that has no InternalControl mapped to it yet.

    Returns the newly created controls — an empty list is a valid, non-error
    outcome meaning the framework is already fully covered. Never sets
    `status` to anything implying effectiveness (RULES.md §1.10): every
    created control defaults to "not_started" with no owner, exactly like an
    ordinary manually-created control the register would otherwise start
    with.
    """
    mapped_requirement_ids = set(
        session.scalars(
            select(ControlRequirementMapping.requirement_id)
            .join(FrameworkRequirement)
            .where(FrameworkRequirement.framework_id == framework.id)
        )
    )
    created: list[InternalControl] = []
    for requirement in framework.requirements:
        if requirement.id in mapped_requirement_ids:
            continue
        control = InternalControl(
            name=f"Starter control for {requirement.reference_code}: {requirement.title}",
            description=(
                "Placeholder starter control generated during onboarding. Review, rename, "
                "assign an owner, and replace this description with what your organization "
                "actually does to satisfy this requirement."
            ),
            status="not_started",
        )
        session.add(control)
        session.flush()
        session.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
        detail = (
            f"Generated starter control for requirement '{requirement.reference_code}' ({framework.name})"
        )
        record_audit_event(
            session,
            entity_type="control",
            entity_id=control.id,
            action="onboarding_generate_starter_control",
            detail=detail,
            actor=actor,
        )
        created.append(control)
    return created
