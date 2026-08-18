"""Headless UAT: a forbidden/invalid action must fail cleanly rather than
bypassing authorization or corrupting history (issue #38's required
scenario 11).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ControlPeriod, InternalControl, Person
from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field

pytestmark = pytest.mark.uat

NON_ADMIN_EMAIL = "uat-nonadmin@example.com"


def _login(client, email):
    login_page = client.get("/login")
    csrf_token = extract_csrf_field(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": UAT_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_non_admin_cannot_create_control_period(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=NON_ADMIN_EMAIL, role="user")
    client = _login(uat_client, NON_ADMIN_EMAIL)

    periods_page = client.get("/control-periods")
    response = client.post(
        "/control-periods",
        data={
            "name": "Should never exist",
            "starts_on": "2026-01-01",
            "ends_on": "2026-06-30",
            "csrf_token": extract_csrf_field(periods_page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    with session_factory() as session:
        assert session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Should never exist")) is None


def test_stale_csrf_token_fails_closed_without_mutating_state(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=NON_ADMIN_EMAIL, role="user")
    client = _login(uat_client, NON_ADMIN_EMAIL)

    create_control_response = client.post(
        "/api/registers/controls",
        json={"name": "UAT CSRF boundary control"},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert create_control_response.status_code == 201, create_control_response.text
    control_id = create_control_response.json()["id"]

    person_form = client.get("/people/new")
    person_response = client.post(
        "/people",
        data={
            "email": "uat-csrf-boundary-owner@example.com",
            "display_name": "Should never become owner",
            "csrf_token": extract_csrf_field(person_form.text),
        },
        follow_redirects=False,
    )
    assert person_response.status_code == 303
    with session_factory() as session:
        candidate_owner = session.scalar(
            select(Person).where(Person.email == "uat-csrf-boundary-owner@example.com")
        )

    control_page = client.get(f"/controls/{control_id}")
    assert control_page.status_code == 200
    real_csrf = extract_csrf_field(control_page.text)

    response = client.post(
        f"/controls/{control_id}/owner",
        data={"owner_person_id": candidate_owner.id, "csrf_token": real_csrf + "-tampered"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    with session_factory() as session:
        control = session.get(InternalControl, control_id)
        assert control.owner_person_id is None
