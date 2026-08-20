"""Headless UAT: issue #20's OIDC identity/role population evidence —
an admin captures a snapshot of accounts linked via generic OIDC, then
an operator attaches it as evidence while performing a quarterly
access-review control occurrence, over real HTTP. Run against the real
app over a real socket (see tests/uat/harness.py).
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from app.models import ControlOccurrence
from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-idp-evidence-admin@example.com"
OPERATOR_EMAIL = "uat-idp-evidence-operator@example.com"

_CSRF_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


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


def test_admin_captures_snapshot_and_operator_attaches_it_to_access_review(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")

    admin_client = _login(uat_client, ADMIN_EMAIL)

    # 1. Capture the identity/role population snapshot as evidence.
    oidc_page = admin_client.get("/admin/authentication/oidc")
    assert oidc_page.status_code == 200
    capture_response = admin_client.post(
        "/admin/authentication/oidc/identity-snapshot",
        data={"csrf_token": extract_csrf_field(oidc_page.text)},
        follow_redirects=False,
    )
    assert capture_response.status_code == 303
    assert "flash_kind=error" not in capture_response.headers["location"]

    # 2. The captured snapshot is visible on the real Evidence page.
    evidence_page = admin_client.get("/evidence")
    assert evidence_page.status_code == 200
    assert "oidc_identity_population" in evidence_page.text

    # 3. Create a control representing the quarterly access review, then
    # generate a manual occurrence for it (real HTTP path).
    create_control = admin_client.post(
        "/api/registers/controls",
        json={
            "name": "UAT Quarterly Access Review",
            "owner": "",
            "status": "not_started",
            "review_frequency": "annual",
            "description": "UAT-created control for issue #20's identity-evidence scenario.",
        },
        headers={"X-CSRF-Token": admin_client.cookies.get("csrf_token")},
    )
    assert create_control.status_code == 201, create_control.text
    control_id = create_control.json()["id"]

    control_page = admin_client.get(f"/controls/{control_id}")
    occurrence_response = admin_client.post(
        f"/controls/{control_id}/occurrences",
        data={"due_on": "2020-01-01", "csrf_token": _CSRF_FIELD_RE.search(control_page.text).group(1)},
        follow_redirects=False,
    )
    assert occurrence_response.status_code == 303
    # create_control_occurrence redirects back to the control detail
    # page (occurrences are listed inline there), not to a standalone
    # occurrence page — extract the new occurrence's id from that page.
    control_page_after = admin_client.get(f"/controls/{control_id}")
    occurrence_link_match = re.search(r"/occurrences/([0-9a-f]{32})", control_page_after.text)
    assert occurrence_link_match is not None, "no occurrence link found on the control detail page"
    occurrence_id = occurrence_link_match.group(1)

    # 4. An operator performs the occurrence and attaches the captured
    # identity snapshot as evidence, in one real form submission — the
    # only existing HTTP path for linking evidence to an occurrence.
    operator_client = _login(uat_client, OPERATOR_EMAIL)
    occurrence_page = operator_client.get(f"/occurrences/{occurrence_id}")
    assert occurrence_page.status_code == 200
    assert "OIDC identity/role population" in occurrence_page.text  # shows up in the evidence dropdown

    evidence_id_match = re.search(
        r'<option value="([0-9a-f]{32})">OIDC identity/role population', occurrence_page.text
    )
    assert evidence_id_match is not None, "captured snapshot not offered in the evidence dropdown"

    perform_response = operator_client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={
            "performed_on": "2026-01-01",
            "evidence_snapshot_id": evidence_id_match.group(1),
            "csrf_token": _CSRF_FIELD_RE.search(occurrence_page.text).group(1),
        },
        follow_redirects=False,
    )
    assert perform_response.status_code == 303
    assert "flash_kind=error" not in perform_response.headers["location"]

    final_page = operator_client.get(f"/occurrences/{occurrence_id}")
    assert "performed on" in final_page.text.lower()


def test_capturing_a_snapshot_alone_never_touches_any_occurrence(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    admin_client = _login(uat_client, ADMIN_EMAIL)

    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(ControlOccurrence))

    oidc_page = admin_client.get("/admin/authentication/oidc")
    admin_client.post(
        "/admin/authentication/oidc/identity-snapshot",
        data={"csrf_token": extract_csrf_field(oidc_page.text)},
        follow_redirects=False,
    )

    with session_factory() as session:
        after = session.scalar(select(func.count()).select_from(ControlOccurrence))

    assert after == before


def test_non_admin_cannot_capture_identity_snapshot(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    operator_client = _login(uat_client, OPERATOR_EMAIL)

    assert operator_client.get("/admin/authentication/oidc").status_code == 403
    response = operator_client.post(
        "/admin/authentication/oidc/identity-snapshot", data={"csrf_token": "irrelevant"}
    )
    assert response.status_code == 403
