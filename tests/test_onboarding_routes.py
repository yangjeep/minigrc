"""Route-level tests for issue #30: onboarding router (app/routers/onboarding.py)
and the `in_scope` field added to the existing people/vendor edit routes.
Domain-level tests live in tests/test_compliance_scope.py and
tests/test_starter_controls.py; migration tests in
tests/test_compliance_scope_migration.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ComplianceScope, InternalControl, Person, VendorSystem
from tests.conftest import extract_csrf_token

NON_WRITE_CLIENTS = ["reader_client", "auditor_client"]


def test_onboarding_checklist_loads_for_any_logged_in_user(reader_client):
    response = reader_client.get("/onboarding")
    assert response.status_code == 200
    assert "Onboarding" in response.text


def test_scope_form_round_trip(logged_in_client):
    page = logged_in_client.get("/onboarding/scope")
    assert page.status_code == 200
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post(
        "/onboarding/scope",
        data={
            "service_description": "Our SaaS product",
            "trust_service_categories": ["availability", "confidentiality"],
            "audit_period_starts_on": "2026-01-01",
            "audit_period_ends_on": "2026-12-31",
            "data_categories": "Customer PII",
            "locations": "US, EU",
            "exclusions": "Marketing site",
            "exclusions_rationale": "Not part of the audited service",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/onboarding")
    assert "flash_kind=error" not in response.headers["location"]

    checklist = logged_in_client.get("/onboarding")
    assert "Our SaaS product" in checklist.text
    assert "security,availability,confidentiality" in checklist.text


def test_scope_form_rejects_end_before_start(logged_in_client, app):
    page = logged_in_client.get("/onboarding/scope")
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post(
        "/onboarding/scope",
        data={
            "audit_period_starts_on": "2026-06-01",
            "audit_period_ends_on": "2026-01-01",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        assert session.scalar(select(ComplianceScope)) is None


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_submit_scope_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/onboarding/scope")
    csrf_token = extract_csrf_token(page.text)

    response = client.post(
        "/onboarding/scope",
        data={"service_description": "should not be saved", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert session.scalar(select(ComplianceScope)) is None


def test_submit_scope_rejects_missing_csrf(logged_in_client):
    response = logged_in_client.post("/onboarding/scope", data={"service_description": "x"})
    assert response.status_code == 422


def test_submit_scope_rejects_wrong_csrf(logged_in_client):
    response = logged_in_client.post(
        "/onboarding/scope", data={"service_description": "x", "csrf_token": "wrong"}
    )
    assert response.status_code == 400


def test_generate_starter_controls_creates_controls_for_uncovered_requirements(logged_in_client, app):
    page = logged_in_client.get("/onboarding")
    csrf_token = extract_csrf_token(page.text)

    with app.state.session_factory() as session:
        before_count = len(session.scalars(select(InternalControl)).all())

    response = logged_in_client.post(
        "/onboarding/generate-starter-controls",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        after_count = len(session.scalars(select(InternalControl)).all())
    assert after_count > before_count

    # Idempotent: calling again creates nothing further.
    page = logged_in_client.get("/onboarding")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        "/onboarding/generate-starter-controls", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    with app.state.session_factory() as session:
        second_count = len(session.scalars(select(InternalControl)).all())
    assert second_count == after_count


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_generate_starter_controls_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/onboarding")
    csrf_token = extract_csrf_token(page.text)

    with app.state.session_factory() as session:
        before_count = len(session.scalars(select(InternalControl)).all())

    response = client.post(
        "/onboarding/generate-starter-controls", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert len(session.scalars(select(InternalControl)).all()) == before_count


# --- in_scope field on the existing People/VendorSystem edit routes -------


def test_person_edit_persists_in_scope_flag(logged_in_client, app):
    with app.state.session_factory() as session:
        person = Person(email="scope-person@example.com", display_name="Scoped Person")
        session.add(person)
        session.commit()
        person_id = person.id

    page = logged_in_client.get(f"/people/{person_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/people/{person_id}/edit",
        data={
            "display_name": "Scoped Person",
            "employment_status": "active",
            "in_scope": "true",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        assert session.get(Person, person_id).in_scope is True


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_person_edit_in_scope_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    with app.state.session_factory() as session:
        person = Person(email="rbac-scope-person@example.com")
        session.add(person)
        session.commit()
        person_id = person.id

    page = client.get(f"/people/{person_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/people/{person_id}/edit",
        data={"display_name": "x", "in_scope": "true", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert session.get(Person, person_id).in_scope is False


def test_vendor_edit_persists_in_scope_flag(logged_in_client, app):
    with app.state.session_factory() as session:
        vendor = VendorSystem(system_name="GitHub", vendor_name="GitHub Inc.")
        session.add(vendor)
        session.commit()
        vendor_id = vendor.id

    page = logged_in_client.get(f"/vendors/{vendor_id}/edit")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/vendors/{vendor_id}/edit",
        data={
            "system_name": "GitHub",
            "vendor_name": "GitHub Inc.",
            "in_scope": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        assert session.get(VendorSystem, vendor_id).in_scope is True
