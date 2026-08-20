"""Domain-level tests for issue #30's starter-control generation
(app/starter_controls.py)."""

from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, ControlRequirementMapping, Framework, FrameworkRequirement, InternalControl
from app.starter_controls import generate_starter_controls_for_framework


def _make_framework(session, *, requirement_codes):
    framework = Framework(name="Test Framework", version="1.0", is_placeholder_content=True)
    session.add(framework)
    session.flush()
    for i, code in enumerate(requirement_codes):
        session.add(
            FrameworkRequirement(
                framework_id=framework.id, reference_code=code, title=f"Requirement {code}", display_order=i
            )
        )
    session.flush()
    return framework


def test_generates_one_control_per_uncovered_requirement(app):
    with app.state.session_factory() as session:
        framework = _make_framework(session, requirement_codes=["A.1", "A.2", "A.3"])

        created = generate_starter_controls_for_framework(session, framework, actor="tester@example.com")
        session.commit()

        assert len(created) == 3
        for control in created:
            assert control.status == "not_started"
            assert control.owner == ""
        mapped_count = session.scalar(
            select(FrameworkRequirement.id)
            .join(ControlRequirementMapping)
            .where(FrameworkRequirement.framework_id == framework.id)
        )
        assert mapped_count is not None


def test_is_idempotent_on_second_call(app):
    with app.state.session_factory() as session:
        framework = _make_framework(session, requirement_codes=["A.1", "A.2"])
        first = generate_starter_controls_for_framework(session, framework)
        session.commit()
        assert len(first) == 2

        second = generate_starter_controls_for_framework(session, framework)
        session.commit()
        assert second == []


def test_only_fills_the_gap_left_by_pre_existing_mappings(app):
    """Mirrors app/seed.py's demo seed already covering some requirements
    before onboarding ever runs — generation must not duplicate those."""
    with app.state.session_factory() as session:
        framework = _make_framework(session, requirement_codes=["A.1", "A.2", "A.3"])
        already_covered = session.scalars(
            select(FrameworkRequirement).where(FrameworkRequirement.reference_code == "A.1")
        ).one()
        manual_control = InternalControl(name="Pre-existing control", status="implemented")
        session.add(manual_control)
        session.flush()
        session.add(
            ControlRequirementMapping(control_id=manual_control.id, requirement_id=already_covered.id)
        )
        session.flush()

        created = generate_starter_controls_for_framework(session, framework)
        session.commit()

        assert len(created) == 2
        assert {c.name for c in created} == {
            "Starter control for A.2: Requirement A.2",
            "Starter control for A.3: Requirement A.3",
        }


def test_records_an_audit_event_per_created_control(app):
    with app.state.session_factory() as session:
        framework = _make_framework(session, requirement_codes=["A.1"])
        created = generate_starter_controls_for_framework(session, framework, actor="tester@example.com")
        session.commit()

        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_type == "control",
                AuditEvent.entity_id == created[0].id,
                AuditEvent.action == "onboarding_generate_starter_control",
            )
        )
        assert event is not None
        assert event.actor == "tester@example.com"


def test_starter_controls_get_a_real_effective_definition_version(app):
    """Issue #42: every starter control begins with a genuine
    event-sourced InternalControlVersion, not just controls that
    already existed when #42's migration backfill ran."""
    with app.state.session_factory() as session:
        framework = _make_framework(session, requirement_codes=["A.1", "A.2"])
        created = generate_starter_controls_for_framework(session, framework, actor="tester@example.com")
        session.commit()

        for control in created:
            session.refresh(control)
            version = control.effective_version
            assert version is not None
            assert version.lifecycle_status == "effective"
            assert version.name == control.name
            assert version.status_snapshot == "not_started"
            mapped_ids = {m.requirement_id for m in control.mappings}
            import json

            assert set(json.loads(version.mapped_requirement_ids_json)) == mapped_ids
