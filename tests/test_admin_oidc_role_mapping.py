"""Admin route tests for OIDC claim/group -> role mapping CRUD (issue
#18): /admin/authentication/oidc/roles.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import AuditEvent, OidcProviderSettings, OidcRoleMapping
from tests.conftest import extract_csrf_token


def test_roles_page_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/authentication/oidc/roles")
    assert response.status_code == 403


def test_roles_page_lists_existing_mappings(admin_client, app):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="finance", role="auditor", updated_by="admin@example.com"))
        session.commit()

    response = admin_client.get("/admin/authentication/oidc/roles")
    assert response.status_code == 200
    assert "finance" in response.text
    assert "auditor" in response.text


def test_admin_can_add_a_mapping(admin_client, app):
    page = admin_client.get("/admin/authentication/oidc/roles")
    csrf_token = extract_csrf_token(page.text)

    response = admin_client.post(
        "/admin/authentication/oidc/roles",
        data={"claim_value": "eng", "role": "operator", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        mapping = session.scalar(select(OidcRoleMapping).where(OidcRoleMapping.claim_value == "eng"))
        assert mapping is not None
        assert mapping.role == "operator"
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "create")).all()
        assert any(e.entity_type == "oidc_role_mapping" for e in events)


def test_duplicate_claim_value_rejected(admin_client, app):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="eng", role="operator", updated_by="admin@example.com"))
        session.commit()

    page = admin_client.get("/admin/authentication/oidc/roles")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/admin/authentication/oidc/roles",
        data={"claim_value": "eng", "role": "admin", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        rows = session.scalars(select(OidcRoleMapping).where(OidcRoleMapping.claim_value == "eng")).all()
        assert len(rows) == 1
        assert rows[0].role == "operator"


def test_invalid_role_rejected(admin_client):
    page = admin_client.get("/admin/authentication/oidc/roles")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/admin/authentication/oidc/roles",
        data={"claim_value": "eng", "role": "not-a-real-role", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_admin_can_delete_a_mapping(admin_client, app):
    with app.state.session_factory() as session:
        mapping = OidcRoleMapping(claim_value="eng", role="operator", updated_by="admin@example.com")
        session.add(mapping)
        session.commit()
        mapping_id = mapping.id

    page = admin_client.get("/admin/authentication/oidc/roles")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/admin/authentication/oidc/roles/{mapping_id}/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        assert session.get(OidcRoleMapping, mapping_id) is None
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "delete")).all()
        assert any(e.entity_type == "oidc_role_mapping" for e in events)


def test_deleting_nonexistent_mapping_flashes_error(admin_client):
    page = admin_client.get("/admin/authentication/oidc/roles")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/admin/authentication/oidc/roles/does-not-exist/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_role_claim_name_can_be_saved(admin_client, app):
    app.state.settings.encryption_key = Fernet.generate_key().decode()
    page = admin_client.get("/admin/authentication/oidc")
    csrf_token = extract_csrf_token(page.text)

    response = admin_client.post(
        "/admin/authentication/oidc",
        data={
            "enabled": "on",
            "display_name": "SSO",
            "issuer": "https://idp.example.test",
            "client_id": "client-id",
            "client_secret": "a-secret",
            "allowed_domains": "",
            "role_claim_name": "roles",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        row = session.scalar(select(OidcProviderSettings))
        assert row.role_claim_name == "roles"
