"""Guided-onboarding step derivation (issue #30).

Every step below is a pure read over existing authoritative tables — no new
mutable "current onboarding step" state is persisted anywhere. RULES.md §3
warns against event-sourcing (or inventing new persisted state for)
transient UI bookkeeping without a concrete compliance reason; a persisted
progress field would also silently drift from reality whenever a user
completes a prerequisite through its own ordinary page (e.g. assigning a
control owner from /controls) without ever visiting /onboarding. See
docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-design.md
§3 for the full reasoning and the table this module implements.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.compliance_scope import get_compliance_scope
from app.config import Settings
from app.models import (
    AwsConnection,
    ControlPeriod,
    ControlRequirementMapping,
    ExternalConnection,
    Framework,
    FrameworkRequirement,
    GoogleDriveConnection,
    InternalControl,
    Person,
    VendorSystem,
)


@dataclass(frozen=True)
class OnboardingStep:
    key: str
    title: str
    description: str
    is_complete: bool
    action_url: str
    action_label: str


def get_primary_framework(session: Session) -> Framework | None:
    return session.scalar(select(Framework).where(Framework.is_primary.is_(True)).limit(1))


def _framework_fully_covered(session: Session, framework: Framework) -> bool:
    uncovered = session.scalar(
        select(FrameworkRequirement.id)
        .where(
            FrameworkRequirement.framework_id == framework.id,
            ~exists().where(ControlRequirementMapping.requirement_id == FrameworkRequirement.id),
        )
        .limit(1)
    )
    return uncovered is None


def _any_control_missing_owner(session: Session) -> bool:
    missing = session.scalar(
        select(InternalControl.id)
        .where(InternalControl.owner_person_id.is_(None), InternalControl.owner == "")
        .limit(1)
    )
    return missing is not None


def compute_onboarding_steps(session: Session, settings: Settings) -> list[OnboardingStep]:
    scope = get_compliance_scope(session)
    primary_framework = get_primary_framework(session)

    has_scoped_person = (
        session.scalar(select(Person.id).where(Person.in_scope.is_(True)).limit(1)) is not None
    )
    has_scoped_vendor_system = (
        session.scalar(select(VendorSystem.id).where(VendorSystem.in_scope.is_(True)).limit(1)) is not None
    )

    starter_controls_done = primary_framework is not None and _framework_fully_covered(
        session, primary_framework
    )

    any_control_exists = session.scalar(select(InternalControl.id).limit(1)) is not None
    owners_assigned = any_control_exists and not _any_control_missing_owner(session)

    evidence_source_configured = (
        settings.evidence_repository_configured
        or session.scalar(select(GoogleDriveConnection.id).limit(1)) is not None
        or session.scalar(select(AwsConnection.id).limit(1)) is not None
        or session.scalar(select(ExternalConnection.id).limit(1)) is not None
    )

    operating_period_started = (
        session.scalar(select(ControlPeriod.id).where(ControlPeriod.status == "active").limit(1)) is not None
    )

    return [
        OnboardingStep(
            key="define_scope",
            title="Define compliance scope",
            description="Describe the service being audited, select SOC 2 Trust Services Categories, "
            "and set a target audit period.",
            is_complete=scope is not None,
            action_url="/onboarding/scope",
            action_label="Define scope" if scope is None else "Review scope",
        ),
        OnboardingStep(
            key="scope_people_systems",
            title="Identify people and systems in scope",
            description="Flag which people and vendor systems are part of this compliance program.",
            is_complete=has_scoped_person or has_scoped_vendor_system,
            action_url="/people",
            action_label="Review people",
        ),
        OnboardingStep(
            key="starter_controls",
            title="Generate starter controls",
            description="Create a placeholder control for every requirement your primary framework "
            "doesn't have one for yet.",
            is_complete=starter_controls_done,
            action_url="/onboarding",
            action_label="Generate starter controls",
        ),
        OnboardingStep(
            key="assign_owners",
            title="Assign control owners",
            description="Every control should have an owner accountable for operating it.",
            is_complete=owners_assigned,
            action_url="/controls",
            action_label="Review controls",
        ),
        OnboardingStep(
            key="evidence_source",
            title="Configure an evidence source",
            description="Connect at least one evidence source (the S3-compatible evidence repository, "
            "Google Drive, AWS, or an external connection).",
            is_complete=evidence_source_configured,
            action_url="/connections",
            action_label="Configure a connection",
        ),
        OnboardingStep(
            key="operating_period",
            title="Start an operating period",
            description="Open an active control period so occurrence generation and tracking can begin.",
            is_complete=operating_period_started,
            action_url="/control-periods",
            action_label="Manage control periods",
        ),
    ]
