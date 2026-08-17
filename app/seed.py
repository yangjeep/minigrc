"""Seed a small, clearly-labelled example dataset.

Everything under `summary`/`description` here is placeholder content
authored for this repository, NOT reproduced text from the ISO/IEC 27001
standard or the AICPA Trust Services Criteria. See
docs/domain/domain-model.md for the research notes and copyright boundary,
and docs/superpowers/specs/2026-08-17-issue12-soc2-primary-framework-design.md
for the SOC 2 placeholder's sourcing.

The system framework catalogs (ISO 27001, SOC 2) are reconciled separately
and unconditionally by app/framework_catalog.py::reconcile_system_catalogs,
called before this function on every startup — this function only adds the
*demo* InternalControl/Risk rows on top of whichever catalogs already
exist, and is gated on an AuditEvent marker (immune to later deletion of
the demo controls themselves, and to Framework no longer ever being empty
now that catalog reconciliation always runs) rather than any business
table's emptiness. See that design doc's §5.2/§5.3 for why.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import (
    AuditEvent,
    ControlRequirementMapping,
    Framework,
    InternalControl,
    Risk,
)

_DEMO_SEED_MARKER = {"entity_type": "control", "action": "seed"}


def seed_if_empty(session: Session) -> bool:
    already_seeded = (
        session.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_type == _DEMO_SEED_MARKER["entity_type"],
                AuditEvent.action == _DEMO_SEED_MARKER["action"],
            )
        )
        is not None
    )
    if already_seeded:
        return False

    # reconcile_system_catalogs (called before this function in
    # app/main.py) is guaranteed to have already created both catalogs and
    # their requirements — this only reads them, never creates a
    # Framework/FrameworkRequirement row itself, avoiding the duplicate-ISO-
    # framework bug an earlier draft of this design had.
    iso_framework = session.scalar(select(Framework).where(Framework.catalog_key == "iso27001-2022-sample"))
    soc2_framework = session.scalar(select(Framework).where(Framework.catalog_key == "soc2-2017-sample"))
    if iso_framework is None or soc2_framework is None:
        raise RuntimeError(
            "seed_if_empty requires reconcile_system_catalogs to have run first in the same "
            "session — call app/framework_catalog.py::reconcile_system_catalogs before this function"
        )
    iso_by_code = {r.reference_code: r for r in iso_framework.requirements}
    soc2_by_code = {r.reference_code: r for r in soc2_framework.requirements}

    control_specs = [
        (
            "Information security policy set",
            "Annual publication and acknowledgement of the org-wide security policy set.",
            "security-lead@example.com",
            "implemented",
            "annual",
            [("iso", "A.5.1"), ("soc2", "CC1.1")],
        ),
        (
            "Asset inventory in Google Workspace + connector scans",
            "Endpoint and SaaS asset inventory reconciled monthly via the Google Workspace connector.",
            "it-lead@example.com",
            "in_progress",
            "monthly",
            [("iso", "A.5.9"), ("iso", "A.8.1")],
        ),
        (
            "Vulnerability management (owned by Aikido)",
            "Aikido is the system of record for vulnerability scanning and remediation SLAs.",
            "security-lead@example.com",
            "implemented",
            "quarterly",
            [("iso", "A.8.8")],
        ),
        (
            "Business continuity test",
            "Annual tabletop exercise validating ICT recovery objectives.",
            "ops-lead@example.com",
            "not_started",
            "annual",
            [("iso", "A.5.30")],
        ),
    ]
    for name, description, owner, status, review_frequency, mappings in control_specs:
        control = InternalControl(
            name=name,
            description=description,
            owner=owner,
            status=status,
            review_frequency=review_frequency,
        )
        session.add(control)
        session.flush()
        codes_by_id = {}
        for framework_key, code in mappings:
            requirement = (iso_by_code if framework_key == "iso" else soc2_by_code)[code]
            session.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
            codes_by_id[code] = requirement
        record_audit_event(
            session,
            entity_type="control",
            entity_id=control.id,
            action="seed",
            detail=f"Seeded sample control '{control.name}' mapped to {', '.join(codes_by_id)}",
        )

    risk_specs = [
        (
            "Unpatched endpoint software",
            "Sample risk: laptops falling out of patch compliance window.",
            "technology",
            3,
            4,
            "it-lead@example.com",
            "mitigating",
            "Enforce automatic updates; track via endpoint connector once built.",
        ),
        (
            "Vendor SaaS data exposure",
            "Sample risk: a connected SaaS vendor mishandles customer data.",
            "third-party",
            2,
            5,
            "security-lead@example.com",
            "open",
            "Vendor risk review during onboarding; revisit at renewal.",
        ),
    ]
    for title, description, category, likelihood, impact, owner, status, treatment_plan in risk_specs:
        risk = Risk(
            title=title,
            description=description,
            category=category,
            likelihood=likelihood,
            impact=impact,
            owner=owner,
            status=status,
            treatment_plan=treatment_plan,
        )
        session.add(risk)
        session.flush()
        record_audit_event(
            session,
            entity_type="risk",
            entity_id=risk.id,
            action="seed",
            detail=f"Seeded sample risk '{risk.title}'",
        )

    return True
