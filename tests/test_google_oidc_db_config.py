from __future__ import annotations

from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import AuditEvent, GoogleOidcSettings, User
from app.secrets import create_encrypted_secret

TEST_KEY = Fernet.generate_key().decode()


def _configure_db_google_oidc(
    app,
    *,
    enabled: bool = True,
    auto_provision_enabled: bool = True,
    allowed_domains: str = "",
    client_secret: str = "test-client-secret",
) -> None:
    app.state.settings.public_base_url = "https://grc.example.com"
    app.state.settings.encryption_key = TEST_KEY
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="google_oidc_client_secret", plaintext=client_secret, actor="admin", key=TEST_KEY
        )
        session.add(
            GoogleOidcSettings(
                enabled=enabled,
                client_id="test-client-id",
                secret_id=secret.id,
                allowed_domains=allowed_domains,
                auto_provision_enabled=auto_provision_enabled,
                updated_by="admin@example.com",
            )
        )
        session.commit()


def _start_login(client) -> tuple[str, str]:
    response = client.get("/auth/google/login", follow_redirects=False)
    assert response.status_code == 303
    state = client.cookies.get("google_oidc_state")
    nonce = client.cookies.get("google_oidc_nonce")
    return state, nonce


def _do_callback(client, state, claims):
    with (
        patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token"),
        patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims),
    ):
        return client.get(
            "/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )


def test_db_config_takes_priority_and_creates_active_user(app, client):
    _configure_db_google_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "subj-1",
        "email": "person@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    response = _do_callback(client, state, claims)
    assert response.status_code == 303
    assert "session" in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "person@example.com"))
        assert user is not None
        assert user.status == "active"
        assert user.google_subject == "subj-1"


def test_auto_provision_disabled_creates_pending_user_and_rejects_login(app, client):
    _configure_db_google_oidc(app, auto_provision_enabled=False)
    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "subj-2",
        "email": "newcomer@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    response = _do_callback(client, state, claims)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "session" not in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "newcomer@example.com"))
        assert user is not None
        assert user.status == "pending"
        assert user.google_subject == "subj-2"
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "user", AuditEvent.action == "create_via_google_oidc_pending"
            )
        ).all()
        assert len(events) == 1


def test_approved_pending_user_can_then_sign_in(app, client):
    _configure_db_google_oidc(app, auto_provision_enabled=False)
    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "subj-3",
        "email": "approve-me@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    _do_callback(client, state, claims)

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "approve-me@example.com"))
        user.status = "active"
        session.commit()

    state2, nonce2 = _start_login(client)
    claims2 = {**claims, "nonce": nonce2}
    response = _do_callback(client, state2, claims2)
    assert response.status_code == 303
    assert "session" in response.cookies


def test_disabled_user_google_login_rejected(app, client):
    _configure_db_google_oidc(app, auto_provision_enabled=True)
    with app.state.session_factory() as session:
        session.add(
            User(
                email="blocked@example.com",
                password_hash="",
                status="disabled",
                google_subject="subj-blocked",
            )
        )
        session.commit()

    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "subj-blocked",
        "email": "blocked@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    response = _do_callback(client, state, claims)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "session" not in response.cookies


def test_identity_collision_rejected_without_changing_existing_user(app, client):
    _configure_db_google_oidc(app, auto_provision_enabled=True)
    with app.state.session_factory() as session:
        session.add(
            User(
                email="shared@example.com",
                password_hash="",
                status="active",
                google_subject="original-subject",
            )
        )
        session.commit()

    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "different-subject",
        "email": "shared@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    response = _do_callback(client, state, claims)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "session" not in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "shared@example.com"))
        assert user.google_subject == "original-subject"


def test_changed_email_updates_user_matched_by_subject(app, client):
    _configure_db_google_oidc(app, auto_provision_enabled=True)
    with app.state.session_factory() as session:
        session.add(
            User(
                email="old-address@example.com",
                password_hash="",
                status="active",
                google_subject="stable-subject",
            )
        )
        session.commit()

    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "stable-subject",
        "email": "new-address@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    response = _do_callback(client, state, claims)
    assert response.status_code == 303
    assert "session" in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.google_subject == "stable-subject"))
        assert user.email == "new-address@example.com"
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "user", AuditEvent.action == "google_identity_email_changed"
            )
        ).all()
        assert len(events) == 1


def test_broken_encryption_key_falls_back_to_not_configured(app, client):
    _configure_db_google_oidc(app, auto_provision_enabled=True)
    app.state.settings.encryption_key = Fernet.generate_key().decode()  # wrong key now

    response = client.get("/auth/google/login")
    assert response.status_code == 404


def test_admin_authentication_page_reflects_db_config(admin_client, app):
    _configure_db_google_oidc(app, auto_provision_enabled=True, allowed_domains="example.com")
    response = admin_client.get("/admin/authentication/google")
    assert response.status_code == 200
    assert b"test-client-id" in response.content
    assert b"test-client-secret" not in response.content


def test_admin_page_explains_missing_public_base_url(admin_client, app):
    """UAT finding: a fully filled-in form (enabled, client ID, secret)
    still shows 'Not configured' when GRC_PUBLIC_BASE_URL isn't set, with
    no indication why — see 2026-07-20 admin/OAuth/IAM/connections
    consolidation worklog."""
    _configure_db_google_oidc(app, auto_provision_enabled=True)
    app.state.settings.public_base_url = ""

    response = admin_client.get("/admin/authentication/google")
    assert response.status_code == 200
    assert b"Not configured" in response.content
    assert b"GRC_PUBLIC_BASE_URL" in response.content


def test_admin_can_save_google_oauth_settings(admin_client, app):
    from tests.conftest import extract_csrf_token

    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/admin/authentication/google")
    csrf_token = extract_csrf_token(page.text)

    response = admin_client.post(
        "/admin/authentication/google",
        data={
            "enabled": "on",
            "client_id": "new-client-id",
            "client_secret": "brand-new-secret",
            "allowed_domains": "example.com",
            "auto_provision_enabled": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        row = session.scalar(select(GoogleOidcSettings))
        assert row.enabled is True
        assert row.client_id == "new-client-id"
        assert row.auto_provision_enabled is True
        assert row.secret_id is not None


def test_saving_with_blank_secret_keeps_existing_secret(admin_client, app):
    from tests.conftest import extract_csrf_token

    _configure_db_google_oidc(app, auto_provision_enabled=True)
    with app.state.session_factory() as session:
        original_secret_id = session.scalar(select(GoogleOidcSettings)).secret_id

    page = admin_client.get("/admin/authentication/google")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/admin/authentication/google",
        data={
            "enabled": "on",
            "client_id": "test-client-id",
            "client_secret": "",
            "allowed_domains": "",
            "csrf_token": csrf_token,
        },
    )

    with app.state.session_factory() as session:
        row = session.scalar(select(GoogleOidcSettings).order_by(GoogleOidcSettings.updated_at.desc()))
        assert row.secret_id == original_secret_id
