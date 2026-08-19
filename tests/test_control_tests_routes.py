"""Route-level tests for issue #13's control-test routes
(app/routers/control_tests.py). Domain-level tests live in
tests/test_control_tests.py; migration tests in
tests/test_control_test_migration.py.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.control_occurrences import record_occurrence_manually
from app.control_tests import freeze_population
from app.models import (
    ControlTest,
    ControlTestException,
    ControlTestPopulation,
    ControlTestSample,
    EvidenceArtifact,
    EvidenceArtifactVersion,
    InternalControl,
    Person,
)
from tests.conftest import extract_csrf_token

NON_WRITE_CLIENTS = ["reader_client", "auditor_client"]


def _make_control(app, **kwargs):
    defaults = {"name": "Test control", "owner": "owner@example.com"}
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        control = InternalControl(**defaults)
        session.add(control)
        session.commit()
        session.refresh(control)
        return control.id


def _make_person(app, email="tester@example.com"):
    with app.state.session_factory() as session:
        person = Person(email=email)
        session.add(person)
        session.commit()
        session.refresh(person)
        return person.id


def _make_occurrences(app, control_id, n=2):
    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        for _ in range(n):
            record_occurrence_manually(session, control)
        session.commit()


def test_create_population_and_view(logged_in_client, app):
    control_id = _make_control(app)
    _make_occurrences(app, control_id, n=2)

    page = logged_in_client.get("/control-tests/populations/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/control-tests/populations",
        data={
            "control_id": control_id,
            "description": "Q1 access reviews",
            "criteria_note": "All occurrences this quarter",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        population = session.scalar(select(ControlTestPopulation))
        assert population is not None
        assert len(population.items) == 2


def test_create_population_404_on_missing_control(logged_in_client):
    page = logged_in_client.get("/control-tests/populations/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/control-tests/populations",
        data={"control_id": "does-not-exist", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_population_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app)

    page = client.get("/control-tests/populations/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/control-tests/populations",
        data={"control_id": control_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert session.scalar(select(ControlTestPopulation)) is None


def _draw_a_population(app, control_id):
    _make_occurrences(app, control_id, n=2)
    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        population = freeze_population(session, control, actor="tester")
        session.commit()
        return population.id


def test_draw_sample_success(logged_in_client, app):
    control_id = _make_control(app)
    population_id = _draw_a_population(app, control_id)

    with app.state.session_factory() as session:
        population = session.get(ControlTestPopulation, population_id)
        item_id = population.items[0].id

    page = logged_in_client.get(f"/control-tests/populations/{population_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/control-tests/populations/{population_id}/samples",
        data={
            "population_item_ids": [item_id],
            "selection_method": "haphazard",
            "selection_rationale": "1 of 2",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        sample = session.scalar(select(ControlTestSample))
        assert sample is not None
        assert len(sample.items) == 1


def test_draw_sample_rejects_unknown_item(logged_in_client, app):
    control_id = _make_control(app)
    population_id = _draw_a_population(app, control_id)

    page = logged_in_client.get(f"/control-tests/populations/{population_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/control-tests/populations/{population_id}/samples",
        data={
            "population_item_ids": ["not-a-real-item"],
            "selection_method": "haphazard",
            "selection_rationale": "x",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_draw_sample_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app)
    population_id = _draw_a_population(app, control_id)
    with app.state.session_factory() as session:
        population = session.get(ControlTestPopulation, population_id)
        item_id = population.items[0].id

    page = client.get(f"/control-tests/populations/{population_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/control-tests/populations/{population_id}/samples",
        data={
            "population_item_ids": [item_id],
            "selection_method": "x",
            "selection_rationale": "x",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_create_control_test_no_exceptions(logged_in_client, app):
    control_id = _make_control(app)
    person_id = _make_person(app)

    page = logged_in_client.get("/control-tests/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "Inspected access list",
            "tester_person_id": person_id,
            "performed_on": "2026-01-15",
            "result": "no_exceptions",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        test = session.scalar(select(ControlTest))
        assert test is not None
        assert test.result == "no_exceptions"


def test_create_control_test_with_exception_lines(logged_in_client, app):
    control_id = _make_control(app)
    person_id = _make_person(app)

    page = logged_in_client.get("/control-tests/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "Inspected access list",
            "tester_person_id": person_id,
            "performed_on": "2026-01-15",
            "result": "exceptions_noted",
            "exception_lines": "One user retained access after termination",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        test = session.scalar(select(ControlTest))
        exceptions = session.scalars(
            select(ControlTestException).where(ControlTestException.test_id == test.id)
        ).all()
        assert len(exceptions) == 1
        assert exceptions[0].description == "One user retained access after termination"


def test_create_control_test_rejects_invalid_result(logged_in_client, app):
    control_id = _make_control(app)
    person_id = _make_person(app)

    page = logged_in_client.get("/control-tests/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "x",
            "tester_person_id": person_id,
            "performed_on": "2026-01-15",
            "result": "not-a-real-result",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_create_control_test_404_on_missing_tester(logged_in_client, app):
    control_id = _make_control(app)

    page = logged_in_client.get("/control-tests/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "x",
            "tester_person_id": "not-a-real-person",
            "performed_on": "2026-01-15",
            "result": "no_exceptions",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_control_test_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app)
    person_id = _make_person(app)

    page = client.get("/control-tests/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "x",
            "tester_person_id": person_id,
            "performed_on": "2026-01-15",
            "result": "no_exceptions",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert session.scalar(select(ControlTest)) is None


def test_view_control_test_404_on_missing(logged_in_client):
    response = logged_in_client.get("/control-tests/does-not-exist")
    assert response.status_code == 404


def test_link_evidence_route(logged_in_client, app):
    control_id = _make_control(app)
    person_id = _make_person(app)

    with app.state.session_factory() as session:
        artifact = EvidenceArtifact(title="Test evidence")
        session.add(artifact)
        session.flush()
        version = EvidenceArtifactVersion(
            id="ev-" + artifact.id[:10],
            artifact_id=artifact.id,
            version_number=1,
            object_key="k",
            original_filename="f.txt",
            media_type="text/plain",
            byte_size=1,
            sha256="a" * 64,
            source_type="manual",
            captured_at=datetime.datetime(2026, 1, 1),
            actor_type="user",
        )
        session.add(version)
        session.commit()
        version_id = version.id

    page = logged_in_client.get("/control-tests/new")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "x",
            "tester_person_id": person_id,
            "performed_on": "2026-01-15",
            "result": "no_exceptions",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    with app.state.session_factory() as session:
        test_id = session.scalar(select(ControlTest)).id

    detail = logged_in_client.get(f"/control-tests/{test_id}")
    csrf_token = extract_csrf_token(detail.text)
    response = logged_in_client.post(
        f"/control-tests/{test_id}/link-evidence",
        data={"evidence_artifact_version_id": version_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]
