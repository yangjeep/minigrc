"""Browser/E2E tests for issue #36's admin API-token management UI
(app/routers/admin_api_tokens.py) — through real auth/CSRF/routes/forms,
not the service layer directly (that's tests/test_api_tokens.py)."""

from __future__ import annotations

from sqlalchemy import select

from app.models import ApiToken
from tests.conftest import extract_csrf_token


def test_api_tokens_list_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/api-tokens")
    assert response.status_code == 403


def test_new_token_form_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/api-tokens/new")
    assert response.status_code == 403


def test_create_token_flow_shows_plaintext_once_and_lists_it_after(admin_client):
    form_page = admin_client.get("/admin/api-tokens/new")
    assert form_page.status_code == 200
    csrf_token = extract_csrf_token(form_page.text)

    response = admin_client.post(
        "/admin/api-tokens", data={"name": "Claude Desktop", "csrf_token": csrf_token}
    )
    assert response.status_code == 200
    assert "mgrc_" in response.text  # the one-time plaintext reveal

    list_page = admin_client.get("/admin/api-tokens")
    assert list_page.status_code == 200
    assert "Claude Desktop" in list_page.text
    assert "mgrc_" not in list_page.text  # never shown again


def test_create_token_rejects_blank_name(admin_client):
    form_page = admin_client.get("/admin/api-tokens/new")
    csrf_token = extract_csrf_token(form_page.text)

    response = admin_client.post("/admin/api-tokens", data={"name": "  ", "csrf_token": csrf_token})
    assert response.status_code in (200, 303)
    assert "mgrc_" not in response.text


def test_revoke_token_flow(admin_client, app):
    form_page = admin_client.get("/admin/api-tokens/new")
    csrf_token = extract_csrf_token(form_page.text)
    admin_client.post("/admin/api-tokens", data={"name": "to-revoke", "csrf_token": csrf_token})

    with app.state.session_factory() as session:
        token = session.scalar(select(ApiToken).where(ApiToken.name == "to-revoke"))
        assert token.revoked_at is None
        token_id = token.id

    list_page = admin_client.get("/admin/api-tokens")
    csrf_token = extract_csrf_token(list_page.text)
    response = admin_client.post(f"/admin/api-tokens/{token_id}/revoke", data={"csrf_token": csrf_token})
    assert response.status_code in (200, 303)

    with app.state.session_factory() as session:
        token = session.get(ApiToken, token_id)
        assert token.revoked_at is not None


def test_revoke_unknown_token_404s(admin_client):
    list_page = admin_client.get("/admin/api-tokens")
    csrf_token = extract_csrf_token(list_page.text)
    response = admin_client.post("/admin/api-tokens/does-not-exist/revoke", data={"csrf_token": csrf_token})
    assert response.status_code == 404


def test_create_token_rejects_wrong_csrf(admin_client):
    response = admin_client.post(
        "/admin/api-tokens", data={"name": "wrong-csrf", "csrf_token": "not-the-real-token"}
    )
    assert response.status_code == 400
