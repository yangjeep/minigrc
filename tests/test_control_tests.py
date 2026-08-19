"""Domain-level tests for issue #13's population/sample/test recording
(app/control_tests.py). Router-level/authorization tests live in
tests/test_control_tests_routes.py; migration tests in
tests/test_control_test_migration.py.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.control_occurrences import record_occurrence_manually
from app.control_tests import (
    InvalidControlTestError,
    draw_sample,
    freeze_population,
    link_test_evidence,
    rebuild_control_test_projection,
    record_test,
)
from app.events import DomainEvent
from app.models import (
    ControlTest,
    ControlTestException,
    EvidenceArtifact,
    EvidenceArtifactVersion,
    InternalControl,
    Person,
)


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


def _make_occurrences(session, control, n=3):
    return [record_occurrence_manually(session, control) for _ in range(n)]


def test_freeze_population_snapshots_current_occurrences(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        _make_occurrences(session, control, n=3)
        session.commit()

        population = freeze_population(session, control, description="Q1 population", actor="tester")
        session.commit()

        assert len(population.items) == 3


def test_freeze_population_is_unaffected_by_later_occurrences(app):
    """A population's membership must not silently change if a new
    occurrence is created afterward — RULES.md's historical-basis
    invariant."""
    with app.state.session_factory() as session:
        control = _make_control(session)
        _make_occurrences(session, control, n=2)
        session.commit()

        population = freeze_population(session, control, actor="tester")
        session.commit()
        population_id = population.id
        item_count = len(population.items)

        record_occurrence_manually(session, control)
        session.commit()

        refreshed = session.get(type(population), population_id)
        assert len(refreshed.items) == item_count


def test_draw_sample_rejects_item_not_in_population(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        _make_occurrences(session, control, n=2)
        session.commit()
        population = freeze_population(session, control, actor="tester")
        session.commit()

        with pytest.raises(InvalidControlTestError):
            draw_sample(
                session,
                population,
                ["not-a-real-item-id"],
                selection_method="haphazard",
                selection_rationale="test",
                actor="tester",
            )


def test_draw_sample_rejects_empty_selection(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        _make_occurrences(session, control, n=1)
        session.commit()
        population = freeze_population(session, control, actor="tester")
        session.commit()

        with pytest.raises(InvalidControlTestError):
            draw_sample(
                session, population, [], selection_method="haphazard", selection_rationale="x", actor="t"
            )


def test_draw_sample_succeeds_with_valid_items(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        _make_occurrences(session, control, n=3)
        session.commit()
        population = freeze_population(session, control, actor="tester")
        session.commit()

        chosen = [population.items[0].id, population.items[1].id]
        sample = draw_sample(
            session,
            population,
            chosen,
            selection_method="haphazard",
            selection_rationale="2 of 3",
            actor="tester",
        )
        session.commit()
        assert len(sample.items) == 2


def test_record_test_rejects_invalid_result(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        session.commit()

        with pytest.raises(InvalidControlTestError):
            record_test(
                session,
                control,
                procedure_description="Inspected config",
                tester_person_id=person.id,
                performed_at=datetime.datetime(2026, 1, 1),
                result="not-a-real-result",
            )


def test_record_test_no_exceptions_creates_a_control_test(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        session.commit()

        test = record_test(
            session,
            control,
            procedure_description="Inspected config",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="no_exceptions",
        )
        session.commit()

        assert test.result == "no_exceptions"
        assert test.control_id == control.id
        assert test.sample_id is None


def test_record_test_with_exceptions_creates_exception_rows(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        _make_occurrences(session, control, n=2)
        session.commit()
        population = freeze_population(session, control, actor="tester")
        session.commit()
        sample = draw_sample(
            session,
            population,
            [population.items[0].id],
            selection_method="haphazard",
            selection_rationale="1 of 2",
            actor="tester",
        )
        session.commit()

        test = record_test(
            session,
            control,
            sample=sample,
            procedure_description="Reviewed sample",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
            exceptions=[{"sample_item_id": sample.items[0].id, "description": "Approval missing"}],
        )
        session.commit()

        exceptions = session.scalars(
            select(ControlTestException).where(ControlTestException.test_id == test.id)
        ).all()
        assert len(exceptions) == 1
        assert exceptions[0].description == "Approval missing"


def test_record_test_rejects_exception_sample_item_from_a_different_sample(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        _make_occurrences(session, control, n=2)
        session.commit()
        population = freeze_population(session, control, actor="tester")
        session.commit()
        sample = draw_sample(
            session,
            population,
            [population.items[0].id],
            selection_method="haphazard",
            selection_rationale="x",
            actor="tester",
        )
        session.commit()

        with pytest.raises(InvalidControlTestError):
            record_test(
                session,
                control,
                sample=sample,
                procedure_description="x",
                tester_person_id=person.id,
                performed_at=datetime.datetime(2026, 1, 1),
                result="exceptions_noted",
                exceptions=[{"sample_item_id": "not-in-this-sample", "description": "x"}],
            )


def test_record_test_rejects_exceptions_without_exceptions_noted_result(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        session.commit()

        with pytest.raises(InvalidControlTestError):
            record_test(
                session,
                control,
                procedure_description="x",
                tester_person_id=person.id,
                performed_at=datetime.datetime(2026, 1, 1),
                result="no_exceptions",
                exceptions=[{"description": "shouldn't be allowed"}],
            )


def test_record_test_rejects_a_sample_from_a_different_controls_population(app):
    with app.state.session_factory() as session:
        control_a = _make_control(session, name="Control A")
        control_b = _make_control(session, name="Control B")
        person = _make_person(session)
        _make_occurrences(session, control_a, n=1)
        session.commit()

        population = freeze_population(session, control_a, actor="tester")
        session.commit()
        sample = draw_sample(
            session,
            population,
            [population.items[0].id],
            selection_method="haphazard",
            selection_rationale="x",
            actor="tester",
        )
        session.commit()

        with pytest.raises(InvalidControlTestError):
            record_test(
                session,
                control_b,
                sample=sample,
                procedure_description="x",
                tester_person_id=person.id,
                performed_at=datetime.datetime(2026, 1, 1),
                result="no_exceptions",
            )


def test_retest_must_reference_the_same_control(app):
    with app.state.session_factory() as session:
        control_a = _make_control(session, name="Control A")
        control_b = _make_control(session, name="Control B")
        person = _make_person(session)
        session.commit()

        original = record_test(
            session,
            control_a,
            procedure_description="x",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
        )
        session.commit()

        with pytest.raises(InvalidControlTestError):
            record_test(
                session,
                control_b,
                procedure_description="retest",
                tester_person_id=person.id,
                performed_at=datetime.datetime(2026, 2, 1),
                result="no_exceptions",
                retest_of=original,
            )


def test_retest_of_a_different_control_test_links_correctly(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        person = _make_person(session)
        session.commit()

        original = record_test(
            session,
            control,
            procedure_description="x",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
        )
        session.commit()

        retest = record_test(
            session,
            control,
            procedure_description="retest",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 2, 1),
            result="no_exceptions",
            retest_of=original,
        )
        session.commit()

        assert retest.retest_of_test_id == original.id


def _make_evidence_artifact_version(session):
    artifact = EvidenceArtifact(title="Test evidence")
    session.add(artifact)
    session.flush()
    version = EvidenceArtifactVersion(
        id="ev-version-" + artifact.id[:8],
        artifact_id=artifact.id,
        version_number=1,
        object_key="evidence/x/content.txt",
        original_filename="x.txt",
        media_type="text/plain",
        byte_size=1,
        sha256="a" * 64,
        source_type="manual",
        captured_at=datetime.datetime(2026, 1, 1),
        actor_type="user",
    )
    session.add(version)
    session.flush()
    return version


def test_link_test_evidence_and_rebuild_reproduces_state(app):
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
            result="no_exceptions",
        )
        session.commit()
        evidence_version = _make_evidence_artifact_version(session)
        session.commit()
        link_test_evidence(session, test, evidence_version.id)
        session.commit()

        events = session.scalars(
            select(DomainEvent).where(
                DomainEvent.aggregate_type == "control_test", DomainEvent.aggregate_id == test.id
            )
        ).all()
        assert [e.event_type for e in events] == ["ControlTestRecorded", "ControlTestEvidenceLinked"]

        rebuild_control_test_projection(session)
        session.commit()

        rebuilt = session.get(ControlTest, test.id)
        assert rebuilt.result == "no_exceptions"
