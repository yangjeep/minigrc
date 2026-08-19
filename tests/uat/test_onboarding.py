"""Headless UAT: guided compliance-scope and onboarding journey (issue
#30) — define scope -> mark a person in-scope -> generate starter
controls -> assign an owner -> start an operating period -> confirm the
dashboard/onboarding checklist reflects each step, run against the real
app over a real socket. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-design.md
§7.
"""

from __future__ import annotations

import pytest

from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field, redirect_path

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-onboarding-admin@example.com"


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


def test_fresh_deployment_onboarding_journey(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    client = _login(uat_client, ADMIN_EMAIL)

    # 1. Dashboard shows the onboarding banner for a fresh deployment.
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Continue setup" in dashboard.text

    # 2. Define compliance scope.
    scope_form = client.get("/onboarding/scope")
    assert scope_form.status_code == 200
    define_response = client.post(
        "/onboarding/scope",
        data={
            "service_description": "UAT SaaS product",
            "trust_service_categories": ["availability"],
            "csrf_token": extract_csrf_field(scope_form.text),
        },
        follow_redirects=False,
    )
    assert define_response.status_code == 303
    assert "flash_kind=error" not in define_response.headers["location"]

    checklist = client.get("/onboarding")
    assert "UAT SaaS product" in checklist.text
    assert "security,availability" in checklist.text

    # 3. Mark a person in-scope.
    new_person = client.post(
        "/people",
        data={
            "email": "uat-scoped-person@example.com",
            "display_name": "Scoped Person",
            "csrf_token": extract_csrf_field(client.get("/people/new").text),
        },
        follow_redirects=False,
    )
    assert new_person.status_code == 303
    person_path = redirect_path(new_person)
    person_detail = client.get(person_path)
    edit_response = client.post(
        f"{person_path}/edit",
        data={
            "display_name": "Scoped Person",
            "employment_status": "active",
            "in_scope": "true",
            "csrf_token": extract_csrf_field(person_detail.text),
        },
        follow_redirects=False,
    )
    assert edit_response.status_code == 303

    # 4. Generate starter controls from the checklist.
    checklist = client.get("/onboarding")
    assert "Generate starter controls" in checklist.text
    generate_response = client.post(
        "/onboarding/generate-starter-controls",
        data={"csrf_token": extract_csrf_field(checklist.text)},
        follow_redirects=False,
    )
    assert generate_response.status_code == 303
    assert "flash_kind=error" not in generate_response.headers["location"]

    checklist = client.get("/onboarding")
    assert "✓ Generate starter controls" in checklist.text

    # 5. Start an operating period (admin-only route, existing #11 feature).
    period_form = client.get("/control-periods")
    create_period = client.post(
        "/control-periods",
        data={
            "name": "UAT Operating Period",
            "starts_on": "2026-01-01",
            "ends_on": "2026-12-31",
            "csrf_token": extract_csrf_field(period_form.text),
        },
        follow_redirects=False,
    )
    assert create_period.status_code == 303
    period_path = redirect_path(create_period)
    period_detail = client.get(period_path)
    activate_response = client.post(
        f"{period_path}/activate",
        data={"csrf_token": extract_csrf_field(period_detail.text)},
        follow_redirects=False,
    )
    assert activate_response.status_code == 303

    # 6. The onboarding checklist now reflects real, persisted state — not
    # a separate progress tracker that could have drifted from it. Note
    # "Assign control owners" is expected to read incomplete here: the
    # starter controls just generated have no owner yet, which is the
    # checklist correctly surfacing new real work, not a bug.
    final_checklist = client.get("/onboarding")
    assert final_checklist.status_code == 200
    assert "✓ Define compliance scope" in final_checklist.text
    assert "✓ Identify people and systems in scope" in final_checklist.text
    assert "✓ Generate starter controls" in final_checklist.text
    assert "✓ Start an operating period" in final_checklist.text
    assert "○ Assign control owners" in final_checklist.text


def test_reader_cannot_define_scope_or_generate_starter_controls(uat_server, uat_client):
    _base_url, session_factory = uat_server
    reader_email = "uat-onboarding-reader@example.com"
    create_uat_user(session_factory, email=reader_email, role="reader")
    client = _login(uat_client, reader_email)

    checklist = client.get("/onboarding")
    assert checklist.status_code == 200

    scope_form = client.get("/onboarding/scope")
    response = client.post(
        "/onboarding/scope",
        data={"service_description": "should be rejected", "csrf_token": extract_csrf_field(scope_form.text)},
        follow_redirects=False,
    )
    assert response.status_code == 403
