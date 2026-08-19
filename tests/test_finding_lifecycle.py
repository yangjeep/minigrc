"""Domain-level tests for issue #13's finding lifecycle
(app/finding_lifecycle.py). Router-level/authorization tests live in
tests/test_findings_routes.py; migration tests in
tests/test_control_test_migration.py.
"""

from __future__ import annotations

import datetime
import json

import pytest
from sqlalchemy import select

from app.control_tests import record_test
from app.events import DomainEvent
from app.finding_lifecycle import (
    close_finding,
    open_finding,
    rebuild_finding_projection,
    record_remediation_update,
    record_retest,
    request_retest,
)
from app.models import Finding, FindingRemediationUpdate, FindingRetest, InternalControl, Person


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


def test_open_finding_rejects_invalid_severity(app):
    with app.state.session_factory() as session:
        with pytest.raises(ValueError):
            open_finding(session, title="x", severity="not-a-real-severity")


def test_open_finding_rejects_blank_title(app):
    with app.state.session_factory() as session:
        with pytest.raises(ValueError):
            open_finding(session, title="   ", severity="medium")


def test_open_finding_creates_a_finding_with_open_status(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="Access review skipped", severity="high")
        session.commit()

        assert finding.status == "open"
        assert finding.severity == "high"


def test_open_finding_with_a_due_date_stores_a_real_date_object(app):
    """Regression: _project_opened originally assigned payload["due_date"]
    (an ISO string, since the event payload is JSON) directly to
    Finding.due_date without parsing it back into a date object — never
    caught by #13's own tests because none of them passed a real
    due_date. Found while adding issue #14's readiness-queue tests,
    which are the first to open a finding with a due date and then read
    it back for a date comparison."""
    with app.state.session_factory() as session:
        due = datetime.date(2026, 3, 1)
        finding = open_finding(session, title="x", severity="low", due_date=due)
        session.commit()

        assert finding.due_date == due
        assert isinstance(finding.due_date, datetime.date)

        rebuild_finding_projection(session)
        session.commit()
        rebuilt = session.get(Finding, finding.id)
        assert rebuilt.due_date == due


def test_remediation_update_transitions_open_to_remediating(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()

        record_remediation_update(session, finding, note="Started fixing")
        session.commit()

        assert finding.status == "remediating"
        updates = session.scalars(
            select(FindingRemediationUpdate).where(FindingRemediationUpdate.finding_id == finding.id)
        ).all()
        assert len(updates) == 1
        assert updates[0].note == "Started fixing"


def test_remediation_update_rejects_blank_note(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()

        with pytest.raises(ValueError):
            record_remediation_update(session, finding, note="   ")


def test_remediation_update_rejects_on_closed_finding(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        close_finding(session, finding, decision="risk_accepted")
        session.commit()

        with pytest.raises(ValueError):
            record_remediation_update(session, finding, note="too late")


def test_request_retest_requires_remediating_status(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()

        with pytest.raises(ValueError):
            request_retest(session, finding)


def test_request_retest_transitions_to_retesting(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        record_remediation_update(session, finding, note="fixed")
        session.commit()

        request_retest(session, finding)
        session.commit()

        assert finding.status == "retesting"


def test_record_retest_requires_retesting_status(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        finding = open_finding(session, title="x", severity="low", control_id=control.id)
        session.commit()

        test = record_test(
            session,
            control,
            procedure_description="retest",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="no_exceptions",
        )
        session.commit()

        with pytest.raises(ValueError):
            record_retest(session, finding, test=test)


def test_record_retest_enforces_lineage_against_source_test_id(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)

        original = record_test(
            session,
            control,
            procedure_description="original",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
        )
        session.commit()

        finding = open_finding(
            session, title="x", severity="low", control_id=control.id, source_test_id=original.id
        )
        session.commit()
        record_remediation_update(session, finding, note="fixed")
        session.commit()
        request_retest(session, finding)
        session.commit()

        # A retest that does NOT point retest_of back at the original test
        # must be rejected — the docstring's lineage claim must be enforced,
        # not just documented.
        unrelated_test = record_test(
            session,
            control,
            procedure_description="unrelated",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 2, 1),
            result="no_exceptions",
        )
        session.commit()

        with pytest.raises(ValueError):
            record_retest(session, finding, test=unrelated_test)

        # The correctly-linked retest succeeds.
        real_retest = record_test(
            session,
            control,
            procedure_description="retest",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 2, 2),
            result="no_exceptions",
            retest_of=original,
        )
        session.commit()

        record_retest(session, finding, test=real_retest)
        session.commit()
        assert finding.status == "retesting"


def test_record_retest_enforces_control_match_for_control_associated_findings_without_a_source_test(app):
    """A finding opened manually (no source_test_id) but tied to a
    specific control_id must still reject a retest from an unrelated
    control — only a finding with neither control_id nor source_test_id
    has nothing to validate lineage against."""
    with app.state.session_factory() as session:
        control_a = _make_control(session, name="Control A")
        control_b = _make_control(session, name="Control B")
        person = _make_person(session)

        finding = open_finding(session, title="x", severity="low", control_id=control_a.id)
        session.commit()
        record_remediation_update(session, finding, note="fixed")
        session.commit()
        request_retest(session, finding)
        session.commit()

        unrelated_test = record_test(
            session,
            control_b,
            procedure_description="unrelated control's test",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 2, 1),
            result="no_exceptions",
        )
        session.commit()

        with pytest.raises(ValueError):
            record_retest(session, finding, test=unrelated_test)

        matching_test = record_test(
            session,
            control_a,
            procedure_description="same control's test",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 2, 2),
            result="no_exceptions",
        )
        session.commit()
        record_retest(session, finding, test=matching_test)
        session.commit()
        assert finding.status == "retesting"


def test_record_retest_with_no_exceptions_stays_retesting_awaiting_closure(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        finding = open_finding(session, title="x", severity="low", control_id=control.id)
        session.commit()
        record_remediation_update(session, finding, note="fixed")
        session.commit()
        request_retest(session, finding)
        session.commit()

        test = record_test(
            session,
            control,
            procedure_description="retest",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="no_exceptions",
        )
        session.commit()

        record_retest(session, finding, test=test)
        session.commit()

        assert finding.status == "retesting"
        retests = session.scalars(select(FindingRetest).where(FindingRetest.finding_id == finding.id)).all()
        assert len(retests) == 1
        assert retests[0].test_id == test.id


def test_record_retest_with_exceptions_bounces_back_to_remediating(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        finding = open_finding(session, title="x", severity="low", control_id=control.id)
        session.commit()
        record_remediation_update(session, finding, note="fixed")
        session.commit()
        request_retest(session, finding)
        session.commit()

        test = record_test(
            session,
            control,
            procedure_description="retest",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
        )
        session.commit()

        record_retest(session, finding, test=test)
        session.commit()

        assert finding.status == "remediating"


def test_record_retest_never_auto_closes(app):
    """A passing retest must never close the finding on its own — closure
    is always a distinct, explicit human decision (issue #13)."""
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        finding = open_finding(session, title="x", severity="low", control_id=control.id)
        session.commit()
        record_remediation_update(session, finding, note="fixed")
        session.commit()
        request_retest(session, finding)
        session.commit()
        test = record_test(
            session,
            control,
            procedure_description="retest",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="no_exceptions",
        )
        session.commit()
        record_retest(session, finding, test=test)
        session.commit()

        assert finding.status != "closed"


def test_close_finding_rejects_invalid_decision(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()

        with pytest.raises(ValueError):
            close_finding(session, finding, decision="not-a-real-decision")


def test_close_finding_rejects_already_closed(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        close_finding(session, finding, decision="false_positive")
        session.commit()

        with pytest.raises(ValueError):
            close_finding(session, finding, decision="risk_accepted")


def test_close_finding_from_open_status_directly(app):
    """A false positive can be closed without ever going through
    remediation/retest."""
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()

        close_finding(session, finding, decision="false_positive", note="Not actually a deviation")
        session.commit()

        assert finding.status == "closed"
        assert finding.closure_decision == "false_positive"


def test_closing_a_finding_does_not_erase_the_original_event(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="Original issue", severity="critical")
        session.commit()
        close_finding(session, finding, decision="risk_accepted")
        session.commit()

        events = session.scalars(
            select(DomainEvent)
            .where(DomainEvent.aggregate_type == "finding", DomainEvent.aggregate_id == finding.id)
            .order_by(DomainEvent.aggregate_sequence)
        ).all()
        assert events[0].event_type == "FindingOpened"
        assert json.loads(events[0].payload_json)["title"] == "Original issue"
        assert events[-1].event_type == "FindingClosed"


def test_rebuild_finding_projection_reproduces_state(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="medium")
        session.commit()
        record_remediation_update(session, finding, note="working on it")
        session.commit()
        finding_id = finding.id

        rebuild_finding_projection(session)
        session.commit()

        rebuilt = session.get(Finding, finding_id)
        assert rebuilt.status == "remediating"
        updates = session.scalars(
            select(FindingRemediationUpdate).where(FindingRemediationUpdate.finding_id == finding_id)
        ).all()
        assert len(updates) == 1
