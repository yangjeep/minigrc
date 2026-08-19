"""Headless UAT: the readiness-stage narrative on the landing page
(issue #33) — walk a fresh deployment through scope -> foundation ->
operating controls -> audit-package-ready, then regress it with an open
finding and confirm it recovers once the finding is closed. Run against
the real app over a real socket. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue33-readiness-landing-page-design.md §5.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.models import InternalControl
from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field, redirect_path

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-stage-admin@example.com"

_STAGE_RE = re.compile(r"Readiness stage: ([^.]+)\.")


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


def _current_stage(client) -> str:
    page = client.get("/")
    match = _STAGE_RE.search(page.text)
    assert match is not None, "no readiness-stage banner found on the dashboard"
    return match.group(1)


def test_stage_progresses_and_regresses_with_real_state(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    client = _login(uat_client, ADMIN_EMAIL)

    assert _current_stage(client) == "Scope incomplete"

    # Define scope -> foundation incomplete (starter controls not yet
    # generated for the primary framework).
    scope_form = client.get("/onboarding/scope")
    define_response = client.post(
        "/onboarding/scope",
        data={"service_description": "UAT service", "csrf_token": extract_csrf_field(scope_form.text)},
        follow_redirects=False,
    )
    assert define_response.status_code == 303
    assert _current_stage(client) == "Foundation incomplete"

    # Generate starter controls, then assign owners to every control that
    # still needs one (the register grid, same as tests/test_register_api.py).
    checklist = client.get("/onboarding")
    generate_response = client.post(
        "/onboarding/generate-starter-controls",
        data={"csrf_token": extract_csrf_field(checklist.text)},
        follow_redirects=False,
    )
    # Asserted explicitly (issue #72): a silent failure here would only
    # surface two steps downstream as a confusing stage mismatch instead
    # of pinpointing the actual failing action.
    assert generate_response.status_code == 303
    assert "flash_kind=error" not in generate_response.headers["location"]
    with session_factory() as session:
        for control in session.scalars(select(InternalControl)).all():
            if not control.owner and control.owner_person_id is None:
                control.owner = "security@example.com"
        session.commit()

    assert _current_stage(client) == "Operating controls"

    # Start an operating period.
    period_form = client.get("/control-periods")
    create_period = client.post(
        "/control-periods",
        data={
            "name": "UAT Period",
            "starts_on": "2026-01-01",
            "ends_on": "2026-12-31",
            "csrf_token": extract_csrf_field(period_form.text),
        },
        follow_redirects=False,
    )
    assert create_period.status_code == 303
    period_path = redirect_path(create_period)
    period_detail = client.get(period_path)
    client.post(
        f"{period_path}/activate",
        data={"csrf_token": extract_csrf_field(period_detail.text)},
        follow_redirects=False,
    )

    assert _current_stage(client) == "Audit-package ready"

    # Introduce a real gap: open a finding.
    finding_form = client.get("/findings/new")
    create_finding = client.post(
        "/findings",
        data={
            "title": "UAT stage regression finding",
            "severity": "high",
            "csrf_token": extract_csrf_field(finding_form.text),
        },
        follow_redirects=False,
    )
    assert create_finding.status_code == 303
    finding_id = redirect_path(create_finding).rsplit("/", 1)[-1]

    assert _current_stage(client) == "Testing/remediation incomplete"

    # Resolve it — the stage recovers deterministically, with no separate
    # "clear the flag" action.
    finding_detail = client.get(f"/findings/{finding_id}")
    close_response = client.post(
        f"/findings/{finding_id}/close",
        data={"decision": "false_positive", "csrf_token": extract_csrf_field(finding_detail.text)},
        follow_redirects=False,
    )
    assert close_response.status_code == 303

    assert _current_stage(client) == "Audit-package ready"
