"""Seed a small, clearly-labelled example dataset.

Everything under `summary`/`description` here is placeholder content
authored for this repository, NOT reproduced text from the ISO/IEC 27001
standard. See docs/domain/domain-model.md for the research notes and the
copyright boundary this observes. Seeding is idempotent: it only runs when
the frameworks table is empty, so restarts don't duplicate data.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import (
    ControlRequirementMapping,
    Framework,
    InternalControl,
    Risk,
)
from app.requirements import add_requirement


def seed_if_empty(session: Session) -> bool:
    if session.query(Framework).first() is not None:
        return False

    framework = Framework(
        name="ISO/IEC 27001:2022 Annex A (sample catalogue)",
        version="2022",
        description=(
            "Placeholder catalogue for local development and demos. Reference codes "
            "follow the public Annex A theme numbering, but titles and summaries below "
            "are paraphrased placeholders written for this repository — not the "
            "licensed normative text of the standard. Replace with your "
            "organization's own licensed copy before relying on this for a real audit."
        ),
        is_placeholder_content=True,
    )
    session.add(framework)
    session.flush()
    record_audit_event(
        session,
        entity_type="framework",
        entity_id=framework.id,
        action="seed",
        detail=f"Seeded sample framework '{framework.name}'",
    )

    requirement_specs = [
        (
            "A.5.1",
            "Policies for information security",
            "Top-level direction and topic-specific policies exist, are approved, and communicated.",
        ),
        (
            "A.5.9",
            "Inventory of information and assets",
            "Assets associated with information and information processing are identified and inventoried.",
        ),
        (
            "A.8.1",
            "User endpoint devices",
            "Information stored on, processed by, or accessible via endpoint devices is protected.",
        ),
        (
            "A.8.8",
            "Management of technical vulnerabilities",
            "Information about technical vulnerabilities is obtained and exposure is evaluated.",
        ),
        (
            "A.5.30",
            "ICT readiness for business continuity",
            "ICT readiness is planned, implemented, maintained, and tested against continuity objectives.",
        ),
    ]
    requirements = []
    for order, (reference_code, title, summary) in enumerate(requirement_specs):
        requirement = add_requirement(
            session,
            framework,
            reference_code=reference_code,
            title=title,
            summary=summary,
            display_order=order,
        )
        requirements.append(requirement)
    session.flush()
    record_audit_event(
        session,
        entity_type="framework",
        entity_id=framework.id,
        action="seed",
        detail=f"Seeded {len(requirements)} sample requirements",
    )

    by_code = {r.reference_code: r for r in requirements}

    control_specs = [
        (
            "Information security policy set",
            "Annual publication and acknowledgement of the org-wide security policy set.",
            "security-lead@example.com",
            "implemented",
            "annual",
            ["A.5.1"],
        ),
        (
            "Asset inventory in Google Workspace + connector scans",
            "Endpoint and SaaS asset inventory reconciled monthly via the Google Workspace connector.",
            "it-lead@example.com",
            "in_progress",
            "monthly",
            ["A.5.9", "A.8.1"],
        ),
        (
            "Vulnerability management (owned by Aikido)",
            "Aikido is the system of record for vulnerability scanning and remediation SLAs.",
            "security-lead@example.com",
            "implemented",
            "quarterly",
            ["A.8.8"],
        ),
        (
            "Business continuity test",
            "Annual tabletop exercise validating ICT recovery objectives.",
            "ops-lead@example.com",
            "not_started",
            "annual",
            ["A.5.30"],
        ),
    ]
    for name, description, owner, status, review_frequency, codes in control_specs:
        control = InternalControl(
            name=name,
            description=description,
            owner=owner,
            status=status,
            review_frequency=review_frequency,
        )
        session.add(control)
        session.flush()
        for code in codes:
            session.add(ControlRequirementMapping(control_id=control.id, requirement_id=by_code[code].id))
        record_audit_event(
            session,
            entity_type="control",
            entity_id=control.id,
            action="seed",
            detail=f"Seeded sample control '{control.name}' mapped to {', '.join(codes)}",
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
