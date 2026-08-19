"""Headless UAT: a forbidden/invalid action must fail cleanly rather than
bypassing authorization or corrupting history (issue #38's required
scenario 11).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ControlPeriod, InternalControl, Risk
from tests.uat.harness import (
    UAT_PASSWORD,
    create_uat_user,
    csrf_header_value,
    extract_csrf_field,
    redirect_path,
)

pytestmark = pytest.mark.uat

NON_ADMIN_EMAIL = "uat-nonadmin@example.com"
READER_EMAIL = "uat-reader@example.com"
OPERATOR_EMAIL = "uat-operator@example.com"


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
    create_uat_user(session_factory, email=NON_ADMIN_EMAIL, role="operator")
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
    create_uat_user(session_factory, email=NON_ADMIN_EMAIL, role="operator")
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
    candidate_owner_id = redirect_path(person_response).rsplit("/", 1)[-1]

    control_page = client.get(f"/controls/{control_id}")
    assert control_page.status_code == 200
    real_csrf = extract_csrf_field(control_page.text)

    response = client.post(
        f"/controls/{control_id}/owner",
        data={"owner_person_id": candidate_owner_id, "csrf_token": real_csrf + "-tampered"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    with session_factory() as session:
        control = session.get(InternalControl, control_id)
        assert control.owner_person_id is None


def test_operator_can_record_material_action_reader_cannot(uat_server, uat_client):
    """issue #37's representative RBAC journey: an operator performs a
    real material-state mutation successfully; a reader attempting the
    identical action over the real app is rejected cleanly (403, no state
    change), matching `.agent/LOOP.md` §9's adversarial question
    generalized from "admin" to "material.\""""
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    create_uat_user(session_factory, email=READER_EMAIL, role="reader")

    operator_client = _login(uat_client, OPERATOR_EMAIL)
    create_response = operator_client.post(
        "/api/registers/controls",
        json={"name": "UAT RBAC operator control"},
        headers={"X-CSRF-Token": csrf_header_value(operator_client)},
    )
    assert create_response.status_code == 201, create_response.text
    control_id = create_response.json()["id"]

    risks_page = operator_client.get("/risks")
    risk_response = operator_client.post(
        "/risks",
        data={"title": "UAT RBAC operator risk", "csrf_token": extract_csrf_field(risks_page.text)},
        follow_redirects=False,
    )
    assert risk_response.status_code == 303

    reader_client = _login(uat_client, READER_EMAIL)
    reader_create_response = reader_client.post(
        "/api/registers/controls",
        json={"name": "UAT RBAC reader control — should never exist"},
        headers={"X-CSRF-Token": csrf_header_value(reader_client)},
    )
    assert reader_create_response.status_code == 403

    reader_risks_page = reader_client.get("/risks")
    assert reader_risks_page.status_code == 200
    reader_risk_response = reader_client.post(
        "/risks",
        data={
            "title": "UAT RBAC reader risk — should never exist",
            "csrf_token": extract_csrf_field(reader_risks_page.text),
        },
        follow_redirects=False,
    )
    assert reader_risk_response.status_code == 403

    with session_factory() as session:
        assert session.get(InternalControl, control_id) is not None
        assert (
            session.scalar(
                select(InternalControl).where(
                    InternalControl.name == "UAT RBAC reader control — should never exist"
                )
            )
            is None
        )
        assert session.scalar(select(Risk).where(Risk.title == "UAT RBAC operator risk")) is not None
        assert (
            session.scalar(select(Risk).where(Risk.title == "UAT RBAC reader risk — should never exist"))
            is None
        )
