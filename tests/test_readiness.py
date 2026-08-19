"""Domain-level tests for issue #14's readiness queue
(app/readiness.py). Router-level tests live in tests/test_dashboard_readiness.py.
"""

from __future__ import annotations

import datetime

from app.control_occurrences import (
    link_evidence_artifact_version,
    perform_occurrence,
    record_occurrence_manually,
)
from app.control_tests import record_test
from app.finding_lifecycle import close_finding, open_finding, record_remediation_update, request_retest
from app.models import (
    ControlRequirementMapping,
    EvidenceArtifact,
    EvidenceArtifactVersion,
    EvidenceSnapshot,
    Framework,
    FrameworkRequirement,
    InternalControl,
    Person,
    Policy,
)
from app.readiness import compute_readiness_queue


def _make_control(session, **kwargs):
    defaults = {"name": "Test control", "owner": "owner@example.com"}
    defaults.update(kwargs)
    control = InternalControl(**defaults)
    session.add(control)
    session.flush()
    return control


def _make_person(session, email="tester@example.com"):
    person = Person(email=email)
    session.add(person)
    session.flush()
    return person


def _categories(items):
    return [item.category for item in items]


def test_empty_state_has_no_items(app):
    with app.state.session_factory() as session:
        assert compute_readiness_queue(session) == []


def test_overdue_occurrence_is_flagged(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        session.commit()
        past = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(days=5)
        record_occurrence_manually(session, control, due_at=past)
        session.commit()

        items = compute_readiness_queue(session)
        assert "occurrence_overdue" in _categories(items)


def test_upcoming_occurrence_within_window_is_flagged(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        session.commit()
        soon = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(days=5)
        record_occurrence_manually(session, control, due_at=soon)
        session.commit()

        items = compute_readiness_queue(session)
        assert "occurrence_upcoming" in _categories(items)


def test_occurrence_far_in_the_future_is_not_flagged(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        session.commit()
        far = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(days=90)
        record_occurrence_manually(session, control, due_at=far)
        session.commit()

        items = compute_readiness_queue(session)
        assert "occurrence_upcoming" not in _categories(items)
        assert "occurrence_overdue" not in _categories(items)


def test_performed_occurrence_with_no_evidence_is_flagged(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        session.commit()
        occurrence = record_occurrence_manually(session, control)
        session.commit()
        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 1, 1),
            performed_by_person_id=None,
            scope_note="done",
            evidence_note="",
        )
        session.commit()

        items = compute_readiness_queue(session)
        assert "occurrence_missing_evidence" in _categories(items)


def test_performed_occurrence_with_evidence_artifact_is_not_flagged(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        session.commit()
        occurrence = record_occurrence_manually(session, control)
        session.commit()
        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 1, 1),
            performed_by_person_id=None,
            scope_note="done",
            evidence_note="",
        )
        session.commit()

        artifact = EvidenceArtifact(title="Screenshot")
        session.add(artifact)
        session.flush()
        version = EvidenceArtifactVersion(
            id="ev-" + artifact.id[:10],
            artifact_id=artifact.id,
            version_number=1,
            object_key="k",
            original_filename="f.png",
            media_type="image/png",
            byte_size=1,
            sha256="a" * 64,
            source_type="manual",
            captured_at=datetime.datetime(2026, 1, 1),
            actor_type="user",
        )
        session.add(version)
        session.commit()
        link_evidence_artifact_version(session, occurrence, version.id)
        session.commit()

        items = compute_readiness_queue(session)
        assert "occurrence_missing_evidence" not in _categories(items)


def test_stale_evidence_snapshot_is_flagged(app):
    with app.state.session_factory() as session:
        past = datetime.datetime(2020, 1, 1)
        session.add(
            EvidenceSnapshot(
                source_type="aws_cloudtrail",
                check_key="x",
                title="x",
                expires_at=past,
                raw_payload_sha256="a" * 64,
            )
        )
        session.commit()

        items = compute_readiness_queue(session)
        assert "evidence_stale" in _categories(items)


def test_fresh_evidence_snapshot_is_not_flagged(app):
    with app.state.session_factory() as session:
        future = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(days=30)
        session.add(
            EvidenceSnapshot(
                source_type="aws_cloudtrail",
                check_key="x",
                title="x",
                expires_at=future,
                raw_payload_sha256="a" * 64,
            )
        )
        session.commit()

        items = compute_readiness_queue(session)
        assert "evidence_stale" not in _categories(items)


def test_failed_test_with_no_finding_is_flagged(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        session.commit()
        record_test(
            session,
            control,
            procedure_description="x",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
        )
        session.commit()

        items = compute_readiness_queue(session)
        assert "control_test_failed_no_finding" in _categories(items)


def test_failed_test_with_a_finding_already_opened_is_not_flagged(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        session.commit()
        test = record_test(
            session,
            control,
            procedure_description="x",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
        )
        session.commit()
        open_finding(session, title="x", severity="low", source_test_id=test.id)
        session.commit()

        items = compute_readiness_queue(session)
        assert "control_test_failed_no_finding" not in _categories(items)


def test_open_finding_reasons_reflect_due_date_urgency(app):
    with app.state.session_factory() as session:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        finding = open_finding(session, title="x", severity="low", due_date=yesterday)
        session.commit()

        items = compute_readiness_queue(session)
        matching = [
            i for i in items if i.category == "finding_needs_attention" and i.link.endswith(finding.id)
        ]
        assert len(matching) == 1
        assert "Overdue" in matching[0].reason


def test_finding_needs_attention_not_duplicated_for_overdue(app):
    """One finding produces exactly one finding_needs_attention item even
    when it is also overdue — never two rows for one fact."""
    with app.state.session_factory() as session:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        finding = open_finding(session, title="x", severity="low", due_date=yesterday)
        session.commit()

        items = compute_readiness_queue(session)
        matching = [
            i for i in items if i.link.endswith(finding.id) and i.category == "finding_needs_attention"
        ]
        assert len(matching) == 1


def test_finding_awaiting_retest_is_flagged(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        record_remediation_update(session, finding, note="fixed")
        session.commit()
        request_retest(session, finding)
        session.commit()

        items = compute_readiness_queue(session)
        assert "finding_needs_retest" in _categories(items)
        assert "finding_needs_attention" not in [i.category for i in items if i.link.endswith(finding.id)]


def test_closed_finding_is_not_flagged_at_all(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        close_finding(session, finding, decision="false_positive")
        session.commit()

        items = compute_readiness_queue(session)
        assert not [i for i in items if i.link.endswith(finding.id)]


def test_finding_missing_owner_is_flagged_while_open(app):
    with app.state.session_factory() as session:
        open_finding(session, title="x", severity="low")
        session.commit()

        items = compute_readiness_queue(session)
        assert "finding_missing_owner" in _categories(items)


def test_finding_with_owner_is_not_flagged_for_missing_owner(app):
    with app.state.session_factory() as session:
        person = _make_person(session)
        session.commit()
        open_finding(session, title="x", severity="low", owner_person_id=person.id)
        session.commit()

        items = compute_readiness_queue(session)
        assert "finding_missing_owner" not in _categories(items)


def test_control_without_owner_is_flagged(app):
    with app.state.session_factory() as session:
        _make_control(session, owner="")
        session.commit()

        items = compute_readiness_queue(session)
        assert "control_missing_owner" in _categories(items)


def test_control_with_owner_text_is_not_flagged(app):
    with app.state.session_factory() as session:
        _make_control(session, owner="security@example.com")
        session.commit()

        items = compute_readiness_queue(session)
        assert "control_missing_owner" not in _categories(items)


def test_overdue_policy_review_is_flagged(app):
    with app.state.session_factory() as session:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        session.add(Policy(title="Security Policy", next_review_date=yesterday))
        session.commit()

        items = compute_readiness_queue(session)
        assert "policy_review_overdue" in _categories(items)


def test_archived_policy_is_not_flagged_even_if_overdue(app):
    with app.state.session_factory() as session:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        session.add(Policy(title="Old Policy", next_review_date=yesterday, archived=True))
        session.commit()

        items = compute_readiness_queue(session)
        assert "policy_review_overdue" not in _categories(items)


def test_framework_filter_hides_items_from_a_different_frameworks_control(app):
    with app.state.session_factory() as session:
        framework_a = Framework(name="Framework A", version="1")
        framework_b = Framework(name="Framework B", version="1")
        session.add_all([framework_a, framework_b])
        session.flush()
        requirement_a = FrameworkRequirement(framework_id=framework_a.id, reference_code="A.1", title="A1")
        session.add(requirement_a)
        session.flush()

        control = _make_control(session, owner="")
        session.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement_a.id))
        session.commit()

        items_for_a = compute_readiness_queue(session, framework_id=framework_a.id)
        items_for_b = compute_readiness_queue(session, framework_id=framework_b.id)

        assert "control_missing_owner" in [i.category for i in items_for_a]
        assert "control_missing_owner" not in [i.category for i in items_for_b]


def test_framework_filter_still_shows_framework_agnostic_items(app):
    """A finding with no control_id has nothing to filter against — it
    must always be visible regardless of the selected framework."""
    with app.state.session_factory() as session:
        framework = Framework(name="Framework A", version="1")
        session.add(framework)
        session.commit()
        open_finding(session, title="x", severity="low")
        session.commit()

        items = compute_readiness_queue(session, framework_id=framework.id)
        assert "finding_missing_owner" in [i.category for i in items]
