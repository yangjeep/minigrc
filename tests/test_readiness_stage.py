"""Domain-level tests for issue #33's readiness-stage derivation
(app/readiness.py::compute_readiness_stage). Route-level tests live in
tests/test_dashboard_readiness.py.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select

from app.compliance_scope import define_or_revise_scope
from app.control_occurrences import record_occurrence_manually
from app.finding_lifecycle import open_finding
from app.models import (
    ControlPeriod,
    ControlRequirementMapping,
    Framework,
    FrameworkRequirement,
    InternalControl,
)
from app.readiness import compute_readiness_stage
from app.starter_controls import generate_starter_controls_for_framework


def _make_control(session, **kwargs):
    defaults = {"name": "Test control", "owner": "owner@example.com"}
    defaults.update(kwargs)
    control = InternalControl(**defaults)
    session.add(control)
    session.flush()
    return control


def _make_active_period(session):
    session.add(
        ControlPeriod(
            name="Period",
            starts_on=datetime.date(2026, 1, 1),
            ends_on=datetime.date(2026, 12, 31),
            status="active",
            created_by="tester@example.com",
        )
    )


def test_fresh_deployment_is_scope_incomplete(app):
    with app.state.session_factory() as session:
        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "scope_incomplete"


def test_scope_defined_but_no_primary_framework_is_foundation_incomplete(app):
    with app.state.session_factory() as session:
        # Every fresh deployment already has a primary framework via
        # app/framework_catalog.py's startup reconciliation — flip it off
        # to exercise the genuinely-no-framework path directly.
        session.execute(Framework.__table__.update().values(is_primary=False))
        define_or_revise_scope(session, service_description="x")
        session.commit()

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "foundation_incomplete"


def test_scope_defined_with_uncovered_requirements_is_foundation_incomplete(app):
    with app.state.session_factory() as session:
        define_or_revise_scope(session, service_description="x")
        session.commit()

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "foundation_incomplete"


def test_starter_controls_generated_but_missing_owner_is_still_foundation_incomplete(app):
    with app.state.session_factory() as session:
        define_or_revise_scope(session, service_description="x")
        framework = session.scalar(select(Framework).where(Framework.is_primary.is_(True)))
        generate_starter_controls_for_framework(session, framework)
        session.commit()

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "foundation_incomplete"


def _fully_scoped_session(session):
    """Advance a fresh session through scope + starter controls + owners
    (everything short of an active operating period). Returns the
    primary framework."""
    define_or_revise_scope(session, service_description="x")
    framework = session.scalar(select(Framework).where(Framework.is_primary.is_(True)))
    generate_starter_controls_for_framework(session, framework)
    session.commit()
    # Demo-seed controls (app/seed.py) already have owners; only newly
    # generated starter controls need one assigned here.
    for control in session.scalars(select(InternalControl)).all():
        if not control.owner and control.owner_person_id is None:
            control.owner = "owner@example.com"
    session.commit()
    return framework


def test_foundation_complete_with_no_active_period_is_operating_controls(app):
    with app.state.session_factory() as session:
        _fully_scoped_session(session)

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "operating_controls"


def test_active_period_with_an_overdue_occurrence_is_evidence_gaps(app):
    with app.state.session_factory() as session:
        framework = _fully_scoped_session(session)
        _make_active_period(session)
        session.commit()

        control = _make_control(session)
        requirement = session.scalar(
            select(FrameworkRequirement).where(FrameworkRequirement.framework_id == framework.id).limit(1)
        )
        session.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
        session.commit()
        past = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(days=1)
        record_occurrence_manually(session, control, due_at=past)
        session.commit()

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "evidence_gaps"


def test_active_period_with_only_a_missing_owner_finding_is_testing_remediation_incomplete(app):
    with app.state.session_factory() as session:
        _fully_scoped_session(session)
        _make_active_period(session)
        session.commit()
        open_finding(session, title="x", severity="low")
        session.commit()

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "testing_remediation_incomplete"


def test_fully_healthy_state_is_audit_package_ready(app):
    with app.state.session_factory() as session:
        _fully_scoped_session(session)
        _make_active_period(session)
        session.commit()

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "audit_package_ready"


def test_an_upcoming_occurrence_alone_never_blocks_audit_package_ready(app):
    with app.state.session_factory() as session:
        framework = _fully_scoped_session(session)
        _make_active_period(session)
        session.commit()

        control = _make_control(session)
        requirement = session.scalar(
            select(FrameworkRequirement).where(FrameworkRequirement.framework_id == framework.id).limit(1)
        )
        session.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
        session.commit()
        soon = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(days=3)
        record_occurrence_manually(session, control, due_at=soon)
        session.commit()

        result = compute_readiness_stage(session, app.state.settings)
        assert result.stage == "audit_package_ready"
