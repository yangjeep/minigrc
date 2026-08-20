"""Domain-level tests for issue #42's event-backed control-definition
version lifecycle (app/control_definition_lifecycle.py). Migration/
backfill tests live in tests/test_control_definition_lifecycle_migration.py;
occurrence-snapshot integration tests in
tests/test_control_occurrences.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.control_definition_lifecycle import (
    bootstrap_initial_effective_version,
    draft_control_definition_version,
    make_version_effective,
    rebuild_internal_control_version_projection,
    review_version,
    submit_for_review,
    withdraw_version,
)
from app.events import DomainEvent
from app.models import (
    ControlRequirementMapping,
    Framework,
    FrameworkRequirement,
    InternalControl,
    InternalControlVersion,
)


def _make_control(session, **kwargs) -> InternalControl:
    defaults = {"name": "Test Control", "status": "implemented", "review_frequency": "annual"}
    defaults.update(kwargs)
    control = InternalControl(**defaults)
    session.add(control)
    session.flush()
    return control


def _make_version(session, control=None, **kwargs) -> InternalControlVersion:
    if control is None:
        control = _make_control(session)
    return draft_control_definition_version(session, control, **kwargs)


def test_draft_snapshots_current_control_values_by_default(app):
    with app.state.session_factory() as session:
        control = _make_control(session, name="Access Review", status="in_progress")
        version = draft_control_definition_version(session, control, actor_id="user-1")
        session.commit()

        assert version.name == "Access Review"
        assert version.status_snapshot == "in_progress"
        assert version.review_frequency_snapshot == "annual"
        assert version.lifecycle_status == "draft"
        assert version.drafted_by == "user-1"
        assert version.version_number == 1


def test_draft_accepts_explicit_overrides(app):
    with app.state.session_factory() as session:
        control = _make_control(session, name="Access Review")
        version = draft_control_definition_version(session, control, name="Access Review v2")
        session.commit()

        assert version.name == "Access Review v2"
        assert control.name == "Access Review"  # InternalControl's own column is never synced


def test_draft_snapshots_current_mapping_set(app):
    with app.state.session_factory() as session:
        framework = Framework(name="Test Framework", version="2026")
        session.add(framework)
        session.flush()
        requirement = FrameworkRequirement(
            framework_id=framework.id, reference_code="R1", title="Requirement 1"
        )
        session.add(requirement)
        session.flush()
        control = _make_control(session)
        session.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
        session.flush()

        version = draft_control_definition_version(session, control)
        session.commit()

        import json

        assert json.loads(version.mapped_requirement_ids_json) == [requirement.id]


def test_version_numbers_increment_per_control(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        v1 = draft_control_definition_version(session, control)
        session.commit()
        v2 = draft_control_definition_version(session, control)
        session.commit()

        assert v1.version_number == 1
        assert v2.version_number == 2


def test_submit_for_review_transitions_from_draft(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "in_review"
        assert version.submitted_by == "user-1"
        assert version.submitted_at is not None


def test_submit_for_review_rejects_non_draft(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        session.commit()

        with pytest.raises(ValueError, match="draft"):
            submit_for_review(session, version, actor_id="user-1")


def test_review_approve_transitions_to_approved(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", actor_id="user-2")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "approved"
        assert version.reviewed_by == "user-2"
        assert version.review_decision == "approved"


def test_review_reject_returns_to_draft(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="rejected", comment="needs work", actor_id="user-2")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "draft"
        assert version.review_comment == "needs work"


def test_review_rejects_without_comment(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        with pytest.raises(ValueError, match="comment"):
            review_version(session, version, decision="rejected", actor_id="user-2")


def test_review_rejects_invalid_decision(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        with pytest.raises(ValueError, match="Invalid review decision"):
            review_version(session, version, decision="maybe", actor_id="user-2")


def test_review_rejects_from_non_in_review_state(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        with pytest.raises(ValueError, match="in_review"):
            review_version(session, version, decision="approved", actor_id="user-2")


def test_make_effective_requires_approved(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        with pytest.raises(ValueError, match="approved"):
            make_version_effective(session, version, actor_id="user-2")


def test_make_effective_sets_effective_state(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", actor_id="user-2")
        make_version_effective(session, version, actor_id="user-2")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "effective"
        assert version.effective_at is not None


def test_make_effective_supersedes_previous_effective_version(app):
    with app.state.session_factory() as session:
        control = _make_control(session, name="Multi-version Control")

        v1 = draft_control_definition_version(session, control)
        submit_for_review(session, v1, actor_id="user-1")
        review_version(session, v1, decision="approved", actor_id="user-2")
        make_version_effective(session, v1, actor_id="user-2")
        session.flush()

        v2 = draft_control_definition_version(session, control)
        submit_for_review(session, v2, actor_id="user-1")
        review_version(session, v2, decision="approved", actor_id="user-2")
        make_version_effective(session, v2, actor_id="user-2")
        session.commit()

        session.refresh(v1)
        session.refresh(v2)
        assert v2.lifecycle_status == "effective"
        assert v1.lifecycle_status == "superseded"
        assert v1.superseded_by_version_id == v2.id
        assert v1.superseded_at is not None

        events = session.scalars(
            select(DomainEvent).where(
                DomainEvent.aggregate_type == "internal_control_version",
                DomainEvent.aggregate_id.in_([v1.id, v2.id]),
            )
        ).all()
        event_types_by_aggregate: dict[str, list[str]] = {}
        for e in events:
            event_types_by_aggregate.setdefault(e.aggregate_id, []).append(e.event_type)
        assert "InternalControlVersionSuperseded" in event_types_by_aggregate[v1.id]
        assert "InternalControlVersionMadeEffective" in event_types_by_aggregate[v2.id]


def test_making_first_version_effective_has_no_supersede_side_effect(app):
    with app.state.session_factory() as session:
        version = _make_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", actor_id="user-2")
        make_version_effective(session, version, actor_id="user-2")
        session.commit()

        events = session.scalars(
            select(DomainEvent).where(DomainEvent.event_type == "InternalControlVersionSuperseded")
        ).all()
        assert events == []


@pytest.mark.parametrize("lifecycle_status", ["draft", "in_review", "approved", "effective"])
def test_withdraw_valid_from_non_terminal_states(app, lifecycle_status):
    with app.state.session_factory() as session:
        version = _make_version(session)
        if lifecycle_status in ("in_review", "approved", "effective"):
            submit_for_review(session, version, actor_id="user-1")
        if lifecycle_status in ("approved", "effective"):
            review_version(session, version, decision="approved", actor_id="user-2")
        if lifecycle_status == "effective":
            make_version_effective(session, version, actor_id="user-2")

        withdraw_version(session, version, reason="no longer needed", actor_id="user-3")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "withdrawn"
        assert version.withdrawn_reason == "no longer needed"
        assert version.withdrawn_at is not None


def test_withdraw_rejects_already_terminal_states(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        version = draft_control_definition_version(session, control)
        other = draft_control_definition_version(session, control)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", actor_id="user-2")
        make_version_effective(session, version, actor_id="user-2")
        submit_for_review(session, other, actor_id="user-1")
        review_version(session, other, decision="approved", actor_id="user-2")
        make_version_effective(session, other, actor_id="user-2")  # supersedes `version`
        session.flush()

        with pytest.raises(ValueError, match="already"):
            withdraw_version(session, version, actor_id="user-3")


def test_control_effective_version_property(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        assert control.effective_version is None

        version = draft_control_definition_version(session, control)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", actor_id="user-2")
        make_version_effective(session, version, actor_id="user-2")
        session.commit()

        session.refresh(control)
        assert control.effective_version.id == version.id


def test_bootstrap_initial_effective_version_one_call(app):
    with app.state.session_factory() as session:
        control = _make_control(session, name="Bootstrapped Control")
        version = bootstrap_initial_effective_version(session, control)
        session.commit()

        assert version.lifecycle_status == "effective"
        session.refresh(control)
        assert control.effective_version.id == version.id
        # Tagged as system, not a fabricated human action.
        drafted_event = session.scalar(
            select(DomainEvent).where(
                DomainEvent.aggregate_type == "internal_control_version",
                DomainEvent.aggregate_id == version.id,
                DomainEvent.event_type == "InternalControlVersionDrafted",
            )
        )
        assert drafted_event.actor_type == "system"


def test_rebuild_internal_control_version_projection_reproduces_state(app):
    with app.state.session_factory() as session:
        control = _make_control(session, name="Rebuild Control")

        v1 = draft_control_definition_version(session, control)
        submit_for_review(session, v1, actor_id="user-1")
        review_version(session, v1, decision="approved", actor_id="user-2")
        make_version_effective(session, v1, actor_id="user-2")

        v2 = draft_control_definition_version(session, control)
        submit_for_review(session, v2, actor_id="user-1")
        review_version(session, v2, decision="approved", actor_id="user-2")
        make_version_effective(session, v2, actor_id="user-2")  # supersedes v1

        v3 = draft_control_definition_version(session, control)
        withdraw_version(session, v3, reason="abandoned", actor_id="user-1")
        session.commit()
        for version in (v1, v2, v3):
            session.refresh(version)

        before = {
            v.id: (v.lifecycle_status, v.effective_at, v.superseded_at, v.superseded_by_version_id)
            for v in (v1, v2, v3)
        }

        rebuild_internal_control_version_projection(session)
        session.commit()

        after = {
            v.id: (v.lifecycle_status, v.effective_at, v.superseded_at, v.superseded_by_version_id)
            for v in (
                session.get(InternalControlVersion, v1.id),
                session.get(InternalControlVersion, v2.id),
                session.get(InternalControlVersion, v3.id),
            )
        }
        assert before == after


def test_db_constraint_rejects_two_effective_versions_for_same_control(app):
    """Proves uq_internal_control_version_one_effective_per_control
    itself, at the schema level — independent of
    make_version_effective's own ordering (see its docstring for why
    that ordering alone is not the real guard, only a best-effort
    convenience)."""
    import datetime

    with app.state.session_factory() as session:
        control = _make_control(session)
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        session.add(
            InternalControlVersion(
                id="a" * 32,
                control_id=control.id,
                version_number=1,
                name="v1",
                status_snapshot="implemented",
                review_frequency_snapshot="annual",
                lifecycle_status="effective",
                drafted_at=now,
            )
        )
        session.commit()

        with pytest.raises(IntegrityError):
            session.add(
                InternalControlVersion(
                    id="b" * 32,
                    control_id=control.id,
                    version_number=2,
                    name="v2",
                    status_snapshot="implemented",
                    review_frequency_snapshot="annual",
                    lifecycle_status="effective",
                    drafted_at=now,
                )
            )
            session.commit()
