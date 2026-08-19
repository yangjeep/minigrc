"""Headless UAT: the readiness-stage narrative on the landing page
(issue #33) — walk a fresh deployment through scope -> foundation ->
operating controls -> audit-package-ready, then regress it with an open
finding and confirm it recovers once the finding is closed. Run against
the real app over a real socket. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue33-readiness-landing-page-design.md §5.
"""

from __future__ import annotations

import re
import time

import pytest
from sqlalchemy import select

from app.models import InternalControl
from tests.uat.harness import (
    UAT_PASSWORD,
    create_uat_user,
    extract_csrf_field,
    poll_for_visibility,
    redirect_path,
)

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-stage-admin@example.com"

_STAGE_RE = re.compile(r"Readiness stage: ([^.]+)\.")
_STAGE_POLL_TIMEOUT_SECONDS = 2.0
_STAGE_POLL_INTERVAL_SECONDS = 0.05


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


def _assert_stage_eventually(client, expected: str) -> None:
    """Retry the stage read for a short bounded window before failing.

    Guards against issue #57's residual: the response to a preceding
    state-changing POST can be sent to the client before that request's
    own `get_db` dependency cleanup (`session.commit()`) actually lands,
    under thread-pool contention — a real, measured, backend-independent
    gap (see tests/test_sqlite_read_after_write.py), not a SQLite-only
    quirk. `_current_stage` reads through a fresh GET, so — unlike a raw
    DB peek — a single miss here is expected to resolve on a quick
    bounded retry. See issue #72 for the UAT-observed flake this fixes.
    """
    deadline = time.monotonic() + _STAGE_POLL_TIMEOUT_SECONDS
    while True:
        stage = _current_stage(client)
        if stage == expected:
            return
        if time.monotonic() > deadline:
            assert stage == expected
            return
        time.sleep(_STAGE_POLL_INTERVAL_SECONDS)


def test_stage_progresses_and_regresses_with_real_state(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    client = _login(uat_client, ADMIN_EMAIL)

    _assert_stage_eventually(client, "Scope incomplete")

    # Define scope -> foundation incomplete (starter controls not yet
    # generated for the primary framework).
    scope_form = client.get("/onboarding/scope")
    define_response = client.post(
        "/onboarding/scope",
        data={"service_description": "UAT service", "csrf_token": extract_csrf_field(scope_form.text)},
        follow_redirects=False,
    )
    assert define_response.status_code == 303
    _assert_stage_eventually(client, "Foundation incomplete")

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

    # Wait for the just-created controls to actually be visible before
    # backfilling owners — the same #57 residual (see
    # _assert_stage_eventually's docstring) can otherwise make this
    # raw DB peek race the POST's own commit and silently back-fill
    # nothing, which then reads as a confusing downstream stage
    # mismatch rather than a visibility gap in this peek itself.
    poll_for_visibility(session_factory, lambda s: s.scalars(select(InternalControl)).all() or None)

    with session_factory() as session:
        for control in session.scalars(select(InternalControl)).all():
            if not control.owner and control.owner_person_id is None:
                control.owner = "security@example.com"
        session.commit()

    _assert_stage_eventually(client, "Operating controls")

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

    _assert_stage_eventually(client, "Audit-package ready")

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

    _assert_stage_eventually(client, "Testing/remediation incomplete")

    # Resolve it — the stage recovers deterministically, with no separate
    # "clear the flag" action.
    finding_detail = client.get(f"/findings/{finding_id}")
    close_response = client.post(
        f"/findings/{finding_id}/close",
        data={"decision": "false_positive", "csrf_token": extract_csrf_field(finding_detail.text)},
        follow_redirects=False,
    )
    assert close_response.status_code == 303

    _assert_stage_eventually(client, "Audit-package ready")
