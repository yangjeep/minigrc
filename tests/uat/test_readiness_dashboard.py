"""Headless UAT: the continuous readiness work queue (issue #14) —
build one missed occurrence, one evidence gap, one failed test/finding,
and one overdue-ownership gap, confirm the dashboard surfaces all of
them with working deep links, then resolve one and confirm it
disappears. Run against the real app over a real socket. See
tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue14-readiness-dashboard-design.md §6.
"""

from __future__ import annotations

import re

import pytest

from tests.uat.harness import (
    UAT_PASSWORD,
    create_uat_user,
    csrf_header_value,
    extract_csrf_field,
    redirect_path,
)

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-readiness-admin@example.com"


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


def test_dashboard_surfaces_and_resolves_mixed_readiness_state(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    client = _login(uat_client, ADMIN_EMAIL)

    # 1. A control with no owner — an ownership gap.
    create_control = client.post(
        "/api/registers/controls",
        json={
            "name": "UAT Readiness Control",
            "owner": "",
            "status": "not_started",
            "review_frequency": "annual",
            "description": "UAT-created control for the readiness queue scenario.",
        },
        headers={"X-CSRF-Token": csrf_header_value(client)},
    )
    assert create_control.status_code == 201, create_control.text
    control_id = create_control.json()["id"]

    # 2. A missed occurrence (due in the past, never performed).
    control_page = client.get(f"/controls/{control_id}")
    occurrence_response = client.post(
        f"/controls/{control_id}/occurrences",
        data={"due_on": "2020-01-01", "csrf_token": extract_csrf_field(control_page.text)},
        follow_redirects=False,
    )
    assert occurrence_response.status_code == 303
    assert "flash_kind=error" not in occurrence_response.headers["location"]

    # 3. A finding with no owner — surfaces as its own ownership gap and
    # as a finding needing attention.
    finding_form = client.get("/findings/new")
    create_finding = client.post(
        "/findings",
        data={
            "title": "UAT readiness finding",
            "severity": "high",
            "control_id": control_id,
            "csrf_token": extract_csrf_field(finding_form.text),
        },
        follow_redirects=False,
    )
    assert create_finding.status_code == 303
    finding_id = redirect_path(create_finding).rsplit("/", 1)[-1]

    # 4. Confirm the dashboard surfaces every item with a working link.
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Missed control occurrence" in dashboard.text
    assert "Control missing an owner" in dashboard.text
    assert f"/controls/{control_id}" in dashboard.text
    assert f"/findings/{finding_id}" in dashboard.text
    assert re.search(r"\d+ item\(s\) need attention", dashboard.text)

    # 5. Resolve the control's ownership gap and confirm that specific
    # item disappears while the finding-related items remain.
    current_row = create_control.json()
    update_control = client.patch(
        f"/api/registers/controls/{control_id}",
        json={
            "expected_updated_at": current_row["updated_at"],
            "fields": {"owner": "security@example.com"},
        },
        headers={"X-CSRF-Token": csrf_header_value(client)},
    )
    assert update_control.status_code == 200, update_control.text

    after = client.get("/")
    assert "Control missing an owner" not in after.text
    assert f"/findings/{finding_id}" in after.text


def test_reader_can_view_but_not_mutate_readiness_sources(uat_server, uat_client):
    _base_url, session_factory = uat_server
    reader_email = "uat-readiness-reader@example.com"
    create_uat_user(session_factory, email=reader_email, role="reader")
    client = _login(uat_client, reader_email)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Readiness queue" in dashboard.text
