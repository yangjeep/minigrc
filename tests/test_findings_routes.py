"""Route-level tests for issue #13's finding routes
(app/routers/findings.py). Domain-level tests live in
tests/test_finding_lifecycle.py; migration tests in
tests/test_control_test_migration.py.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.control_tests import record_test
from app.finding_lifecycle import open_finding
from app.models import Finding, FindingRemediationUpdate, FindingRetest, InternalControl, Person
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


def test_create_finding(logged_in_client, app):
    control_id = _make_control(app)

    page = logged_in_client.get("/findings/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/findings",
        data={
            "title": "Access review skipped in March",
            "description": "No evidence of review",
            "severity": "high",
            "control_id": control_id,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        finding = session.scalar(select(Finding))
        assert finding is not None
        assert finding.status == "open"
        assert finding.severity == "high"


def test_create_finding_rejects_blank_title(logged_in_client):
    page = logged_in_client.get("/findings/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/findings",
        data={"title": "   ", "severity": "low", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_finding_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/findings/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/findings",
        data={"title": "x", "severity": "low", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert session.scalar(select(Finding)) is None


def test_view_finding_404_on_missing(logged_in_client):
    response = logged_in_client.get("/findings/does-not-exist")
    assert response.status_code == 404


def test_remediation_update_route(logged_in_client, app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        finding_id = finding.id

    page = logged_in_client.get(f"/findings/{finding_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/findings/{finding_id}/remediation-updates",
        data={"note": "Started remediation", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        updates = session.scalars(
            select(FindingRemediationUpdate).where(FindingRemediationUpdate.finding_id == finding_id)
        ).all()
        assert len(updates) == 1
        finding = session.get(Finding, finding_id)
        assert finding.status == "remediating"


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_remediation_update_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        finding_id = finding.id

    page = client.get(f"/findings/{finding_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/findings/{finding_id}/remediation-updates",
        data={"note": "should be rejected", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        query = select(FindingRemediationUpdate).where(FindingRemediationUpdate.finding_id == finding_id)
        assert session.scalar(query) is None


def test_full_remediation_retest_close_journey(logged_in_client, app):
    control_id = _make_control(app)
    person_id = _make_person(app)

    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        person = session.get(Person, person_id)
        original_test = record_test(
            session,
            control,
            procedure_description="Initial test",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 1, 1),
            result="exceptions_noted",
        )
        session.commit()
        finding = open_finding(
            session, title="x", severity="medium", control_id=control_id, source_test_id=original_test.id
        )
        session.commit()
        finding_id = finding.id
        original_test_id = original_test.id

    # Remediation update transitions to "remediating".
    page = logged_in_client.get(f"/findings/{finding_id}")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        f"/findings/{finding_id}/remediation-updates",
        data={"note": "Fixed the process", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    # Request a retest.
    page = logged_in_client.get(f"/findings/{finding_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/findings/{finding_id}/request-retest", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        assert session.get(Finding, finding_id).status == "retesting"

    # Record the actual retest (correctly linked to the original test).
    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        original_test = session.get(type(original_test), original_test_id)
        retest = record_test(
            session,
            control,
            procedure_description="Retest",
            tester_person_id=person_id,
            performed_at=datetime.datetime(2026, 2, 1),
            result="no_exceptions",
            retest_of=original_test,
        )
        session.commit()
        retest_id = retest.id

    page = logged_in_client.get(f"/findings/{finding_id}")
    assert retest_id in page.text
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/findings/{finding_id}/record-retest",
        data={"test_id": retest_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        finding = session.get(Finding, finding_id)
        assert finding.status == "retesting"
        retests = session.scalars(select(FindingRetest).where(FindingRetest.finding_id == finding_id)).all()
        assert len(retests) == 1
        assert retests[0].test_id == retest_id

    # Close the finding.
    page = logged_in_client.get(f"/findings/{finding_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/findings/{finding_id}/close",
        data={
            "decision": "remediated_and_retested",
            "note": "Confirmed fixed",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        finding = session.get(Finding, finding_id)
        assert finding.status == "closed"
        assert finding.closure_decision == "remediated_and_retested"


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_close_finding_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        finding_id = finding.id

    page = client.get(f"/findings/{finding_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/findings/{finding_id}/close",
        data={"decision": "risk_accepted", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert session.get(Finding, finding_id).status == "open"


def test_request_retest_route_rejects_wrong_status(logged_in_client, app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        finding_id = finding.id

    page = logged_in_client.get(f"/findings/{finding_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/findings/{finding_id}/request-retest", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
