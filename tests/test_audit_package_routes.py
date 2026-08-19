"""Route-level tests for issue #34's audit/PBC package export routes
(app/routers/audit_package.py). Domain-level tests live in
tests/test_audit_package.py.
"""

from __future__ import annotations

import datetime
import io
import zipfile

import pytest

from app.compliance_scope import define_or_revise_scope
from tests.conftest import extract_csrf_token

NON_ADMIN_CLIENTS = ["logged_in_client", "reader_client", "auditor_client"]


def test_form_shows_scope_incomplete_hint_when_no_scope_defined(admin_client):
    response = admin_client.get("/admin/audit-package")
    assert response.status_code == 200
    assert "No compliance scope has been defined yet" in response.text


def test_generate_round_trip_returns_a_real_zip(admin_client, app):
    with app.state.session_factory() as session:
        define_or_revise_scope(
            session,
            service_description="x",
            audit_period_starts_on=datetime.date(2026, 1, 1),
            audit_period_ends_on=datetime.date(2026, 12, 31),
        )
        session.commit()

    page = admin_client.get("/admin/audit-package")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/admin/audit-package", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "manifest.json" in archive.namelist()
        assert "scope.json" in archive.namelist()


def test_generate_without_scope_flashes_a_clear_error_not_a_500(admin_client):
    page = admin_client.get("/admin/audit-package")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/admin/audit-package", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@pytest.mark.parametrize("client_fixture", NON_ADMIN_CLIENTS)
def test_get_requires_admin(client_fixture, request):
    client = request.getfixturevalue(client_fixture)
    response = client.get("/admin/audit-package", follow_redirects=False)
    assert response.status_code == 403


@pytest.mark.parametrize("client_fixture", NON_ADMIN_CLIENTS)
def test_post_requires_admin(client_fixture, request):
    client = request.getfixturevalue(client_fixture)
    response = client.post("/admin/audit-package", data={"csrf_token": "irrelevant"}, follow_redirects=False)
    assert response.status_code == 403
