"""DB-backed generic OIDC config + admin page tests (issue #17), mirroring
tests/test_google_oidc_db_config.py. Also covers the identity-linking
policy that deliberately diverges from Google's: no silent email merge.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import select

from app.models import AuditEvent, ExternalIdentity, OidcProviderSettings, User
from app.oidc import ProviderMetadata
from app.secrets import create_encrypted_secret

TEST_KEY = Fernet.generate_key().decode()
RSA_KEY = RSAKey.generate_key(2048, parameters={"kid": "db-config-test-key"}, private=True)


def _configure_db_oidc(
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
            session, name="oidc_client_secret", plaintext=client_secret, actor="admin", key=TEST_KEY
        )
        session.add(
            OidcProviderSettings(
                enabled=enabled,
                display_name="Company SSO",
                issuer="https://idp.example.test",
                client_id="test-client-id",
                secret_id=secret.id,
                allowed_domains=allowed_domains,
                auto_provision_enabled=auto_provision_enabled,
                updated_by="admin@example.com",
            )
        )
        session.commit()


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        issuer="https://idp.example.test",
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint="https://idp.example.test/token",
        jwks_uri="https://idp.example.test/jwks",
    )


def _sign(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.test",
        "sub": "subj-1",
        "aud": "test-client-id",
        "exp": now + 300,
        "iat": now,
        "email": "person@example.com",
        "email_verified": True,
    }
    claims.update(overrides)
    header = {"alg": "RS256", "kid": "db-config-test-key"}
    return jwt.encode(header, claims, RSA_KEY)


def _start_login(client) -> tuple[str, str]:
    with patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()):
        response = client.get("/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 303
    return client.cookies.get("oidc_state"), client.cookies.get("oidc_nonce")


def _do_callback(client, state, raw_token):
    with (
        patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()),
        patch("app.routers.oidc.exchange_code_for_id_token", return_value=raw_token),
        patch("app.oidc._fetch_key_set", return_value=KeySet([RSA_KEY])),
    ):
        return client.get(
            "/auth/oidc/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )


def test_db_config_takes_priority_and_creates_active_user(app, client):
    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="subj-1", email="person@example.com")
    response = _do_callback(client, state, token)
    assert response.status_code == 303
    assert "session" in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "person@example.com"))
        assert user is not None
        assert user.status == "active"
        identity = session.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id))
        assert identity.issuer == "https://idp.example.test"
        assert identity.subject == "subj-1"


def test_auto_provision_disabled_creates_pending_user_and_rejects_login(app, client):
    _configure_db_oidc(app, auto_provision_enabled=False)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="subj-2", email="newcomer@example.com")
    response = _do_callback(client, state, token)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "session" not in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "newcomer@example.com"))
        assert user is not None
        assert user.status == "pending"
        identity = session.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id))
        assert identity is not None
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "user", AuditEvent.action == "create_via_oidc_pending"
            )
        ).all()
        assert len(events) == 1


def test_approved_pending_user_can_then_sign_in(app, client):
    _configure_db_oidc(app, auto_provision_enabled=False)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="subj-3", email="approve-me@example.com")
    _do_callback(client, state, token)

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "approve-me@example.com"))
        user.status = "active"
        session.commit()

    state2, nonce2 = _start_login(client)
    token2 = _sign(nonce=nonce2, sub="subj-3", email="approve-me@example.com")
    response = _do_callback(client, state2, token2)
    assert response.status_code == 303
    assert "session" in response.cookies


def test_disabled_user_oidc_login_rejected(app, client):
    _configure_db_oidc(app, auto_provision_enabled=True)
    with app.state.session_factory() as session:
        user = User(email="blocked@example.com", password_hash="", status="disabled")
        session.add(user)
        session.flush()
        session.add(
            ExternalIdentity(user_id=user.id, issuer="https://idp.example.test", subject="subj-blocked")
        )
        session.commit()

    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="subj-blocked", email="blocked@example.com")
    response = _do_callback(client, state, token)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "session" not in response.cookies


def test_returning_identity_logs_in_without_re_creating_user(app, client):
    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="returning-subject", email="returning@example.com")
    _do_callback(client, state, token)

    with app.state.session_factory() as session:
        assert session.scalar(select(User).where(User.email == "returning@example.com")) is not None
        user_count_after_first_login = len(session.scalars(select(User)).all())

    state2, nonce2 = _start_login(client)
    token2 = _sign(nonce=nonce2, sub="returning-subject", email="returning@example.com")
    response = _do_callback(client, state2, token2)
    assert response.status_code == 303
    assert "session" in response.cookies

    with app.state.session_factory() as session:
        user_count_after_second_login = len(session.scalars(select(User)).all())
        identities = session.scalars(
            select(ExternalIdentity).where(ExternalIdentity.subject == "returning-subject")
        ).all()
        assert len(identities) == 1
    assert user_count_after_second_login == user_count_after_first_login


def test_first_login_email_collision_is_rejected_not_merged(app, client):
    """The regression-relevant divergence from Google's own (looser)
    behavior: a brand-new (issuer, subject) whose asserted email matches
    an existing local user must never be silently linked."""
    _configure_db_oidc(app, auto_provision_enabled=True)
    with app.state.session_factory() as session:
        session.add(User(email="shared@example.com", password_hash="hash-not-empty", status="active"))
        session.commit()

    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="attacker-subject", email="shared@example.com")
    response = _do_callback(client, state, token)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "session" not in response.cookies

    with app.state.session_factory() as session:
        matching_identities = session.scalars(
            select(ExternalIdentity).where(ExternalIdentity.subject == "attacker-subject")
        ).all()
        assert matching_identities == []
        # the original user's password hash (i.e. its identity) is untouched
        user = session.scalar(select(User).where(User.email == "shared@example.com"))
        assert user.password_hash == "hash-not-empty"


def test_broken_encryption_key_falls_back_to_not_configured(app, client):
    _configure_db_oidc(app, auto_provision_enabled=True)
    app.state.settings.encryption_key = Fernet.generate_key().decode()  # wrong key now

    response = client.get("/auth/oidc/login")
    assert response.status_code == 404


def test_admin_authentication_page_reflects_db_config(admin_client, app):
    _configure_db_oidc(app, auto_provision_enabled=True, allowed_domains="example.com")
    response = admin_client.get("/admin/authentication/oidc")
    assert response.status_code == 200
    assert b"test-client-id" in response.content
    assert b"https://idp.example.test" in response.content
    assert b"test-client-secret" not in response.content


def test_login_page_shows_oidc_link_for_db_only_config(client, app):
    _configure_db_oidc(app, auto_provision_enabled=True)
    assert app.state.settings.oidc_client_id == ""  # legacy env path is NOT configured

    response = client.get("/login")
    assert response.status_code == 200
    assert b"/auth/oidc/login" in response.content
    assert b"Company SSO" in response.content


def test_admin_page_explains_missing_public_base_url(admin_client, app):
    _configure_db_oidc(app, auto_provision_enabled=True)
    app.state.settings.public_base_url = ""

    response = admin_client.get("/admin/authentication/oidc")
    assert response.status_code == 200
    assert b"Not configured" in response.content
    assert b"GRC_PUBLIC_BASE_URL" in response.content


def test_admin_can_save_oidc_settings(admin_client, app):
    from tests.conftest import extract_csrf_token

    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/admin/authentication/oidc")
    csrf_token = extract_csrf_token(page.text)

    response = admin_client.post(
        "/admin/authentication/oidc",
        data={
            "enabled": "on",
            "display_name": "Acme SSO",
            "issuer": "https://idp.example.test/",
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
        row = session.scalar(select(OidcProviderSettings))
        assert row.enabled is True
        assert row.display_name == "Acme SSO"
        assert row.issuer == "https://idp.example.test"  # trailing slash stripped
        assert row.client_id == "new-client-id"
        assert row.auto_provision_enabled is True
        assert row.secret_id is not None


def test_saving_with_blank_secret_keeps_existing_secret(admin_client, app):
    from tests.conftest import extract_csrf_token

    _configure_db_oidc(app, auto_provision_enabled=True)
    with app.state.session_factory() as session:
        original_secret_id = session.scalar(select(OidcProviderSettings)).secret_id

    page = admin_client.get("/admin/authentication/oidc")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/admin/authentication/oidc",
        data={
            "enabled": "on",
            "display_name": "Company SSO",
            "issuer": "https://idp.example.test",
            "client_id": "test-client-id",
            "client_secret": "",
            "allowed_domains": "",
            "csrf_token": csrf_token,
        },
    )

    with app.state.session_factory() as session:
        row = session.scalar(select(OidcProviderSettings).order_by(OidcProviderSettings.updated_at.desc()))
        assert row.secret_id == original_secret_id


def test_non_admin_cannot_view_or_save_oidc_settings(logged_in_client):
    response = logged_in_client.get("/admin/authentication/oidc")
    assert response.status_code in (401, 403)
