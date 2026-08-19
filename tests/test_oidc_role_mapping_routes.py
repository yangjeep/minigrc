"""Route-level tests for OIDC claim/group-to-role mapping (issue #18),
exercised through the real /auth/oidc/login and /auth/oidc/callback
routes built in #17. Mirrors tests/test_oidc_db_config.py's signing/
callback helpers.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import select

from app.models import AuditEvent, ExternalIdentity, OidcProviderSettings, OidcRoleMapping, User
from app.oidc import ProviderMetadata
from app.secrets import create_encrypted_secret

TEST_KEY = Fernet.generate_key().decode()
RSA_KEY = RSAKey.generate_key(2048, parameters={"kid": "role-mapping-test-key"}, private=True)


def _configure_db_oidc(app, *, auto_provision_enabled: bool = True) -> None:
    app.state.settings.public_base_url = "https://grc.example.com"
    app.state.settings.encryption_key = TEST_KEY
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="oidc_client_secret", plaintext="test-secret", actor="admin", key=TEST_KEY
        )
        session.add(
            OidcProviderSettings(
                enabled=True,
                display_name="Company SSO",
                issuer="https://idp.example.test",
                client_id="test-client-id",
                secret_id=secret.id,
                allowed_domains="",
                auto_provision_enabled=auto_provision_enabled,
                role_claim_name="groups",
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
    header = {"alg": "RS256", "kid": "role-mapping-test-key"}
    return jwt.encode(header, claims, RSA_KEY)


def _start_login(client) -> tuple[str, str]:
    with patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()):
        response = client.get("/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 303
    return client.cookies.get("oidc_state"), client.cookies.get("oidc_nonce")


def _do_callback(client, state, raw_token, extra_params=None):
    params = {"code": "abc", "state": state, **(extra_params or {})}
    with (
        patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()),
        patch("app.routers.oidc.exchange_code_for_id_token", return_value=raw_token),
        patch("app.oidc._fetch_key_set", return_value=KeySet([RSA_KEY])),
    ):
        return client.get("/auth/oidc/callback", params=params, follow_redirects=False)


def test_bootstrap_first_user_stays_admin_local_regardless_of_mapping(app, client):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="eng", role="reader", updated_by="admin@example.com"))
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="first-user", email="first@example.com", groups=["eng"])
    response = _do_callback(client, state, token)
    assert response.status_code == 303
    assert "session" in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "first@example.com"))
        assert user.role == "admin"
        assert user.role_source == "local"


def test_second_new_user_gets_mapped_role_at_creation(app, client):
    with app.state.session_factory() as session:
        session.add(
            User(email="existing-admin@example.com", password_hash="x", role="admin", role_source="local")
        )
        session.add(OidcRoleMapping(claim_value="finance", role="auditor", updated_by="admin@example.com"))
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="new-employee", email="employee@example.com", groups=["finance"])
    response = _do_callback(client, state, token)
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "employee@example.com"))
        assert user.role == "auditor"
        assert user.role_source == "oidc_mapped"


def test_second_new_user_gets_plain_default_when_mapping_not_configured(app, client):
    """Regression proof: #17's original default-provisioning behavior is
    unchanged when #18's mapping feature is unused (no mapping rows)."""
    with app.state.session_factory() as session:
        session.add(
            User(email="existing-admin@example.com", password_hash="x", role="admin", role_source="local")
        )
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="new-employee", email="employee2@example.com", groups=["finance"])
    response = _do_callback(client, state, token)
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "employee2@example.com"))
        assert user.role == "operator"


def test_returning_user_role_is_promoted_and_demoted_across_logins(app, client):
    with app.state.session_factory() as session:
        session.add(
            User(email="existing-admin@example.com", password_hash="x", role="admin", role_source="local")
        )
        session.add(OidcRoleMapping(claim_value="finance", role="auditor", updated_by="admin@example.com"))
        session.add(OidcRoleMapping(claim_value="sec-admins", role="admin", updated_by="admin@example.com"))
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)

    state1, nonce1 = _start_login(client)
    token1 = _sign(nonce=nonce1, sub="returning-user", email="returning@example.com", groups=["finance"])
    _do_callback(client, state1, token1)
    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "returning@example.com"))
        assert user.role == "auditor"

    state2, nonce2 = _start_login(client)
    token2 = _sign(nonce=nonce2, sub="returning-user", email="returning@example.com", groups=["sec-admins"])
    _do_callback(client, state2, token2)
    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "returning@example.com"))
        assert user.role == "admin"

    state3, nonce3 = _start_login(client)
    token3 = _sign(
        nonce=nonce3, sub="returning-user", email="returning@example.com", groups=["no-such-group"]
    )
    _do_callback(client, state3, token3)
    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "returning@example.com"))
        assert user.role == "reader"


def test_admin_pinned_role_is_never_touched_by_oidc_login(app, client):
    with app.state.session_factory() as session:
        session.add(
            User(email="existing-admin@example.com", password_hash="x", role="admin", role_source="local")
        )
        session.add(OidcRoleMapping(claim_value="sec-admins", role="admin", updated_by="admin@example.com"))
        pinned = User(
            email="pinned@example.com", password_hash="", role="operator", role_source="oidc_mapped"
        )
        session.add(pinned)
        session.flush()
        session.add(
            ExternalIdentity(user_id=pinned.id, issuer="https://idp.example.test", subject="pinned-subject")
        )
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)

    # Simulates an admin manually overriding the role via the real
    # /admin/users/{id}/edit route, which always stamps role_source
    # "local" (see app/routers/admin_users.py::update_user) — asserted
    # directly here since this test's focus is the *pin* behavior on the
    # next OIDC login, already covered end-to-end by
    # tests/test_admin_users.py's own coverage of that route.
    with app.state.session_factory() as session:
        pinned = session.scalar(select(User).where(User.email == "pinned@example.com"))
        pinned.role = "reader"
        pinned.role_source = "local"
        session.commit()

    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="pinned-subject", email="pinned@example.com", groups=["sec-admins"])
    response = _do_callback(client, state, token)
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "pinned@example.com"))
        assert user.role == "reader"
        assert user.role_source == "local"


def test_unmapped_group_never_escalates(app, client):
    with app.state.session_factory() as session:
        session.add(
            User(email="existing-admin@example.com", password_hash="x", role="admin", role_source="local")
        )
        session.add(OidcRoleMapping(claim_value="finance", role="admin", updated_by="admin@example.com"))
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="rando", email="rando@example.com", groups=["totally-unrelated-group"])
    _do_callback(client, state, token)

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "rando@example.com"))
        assert user.role != "admin"
        assert user.role == "reader"


def test_role_cannot_be_injected_via_callback_query_params(app, client):
    with app.state.session_factory() as session:
        session.add(
            User(email="existing-admin@example.com", password_hash="x", role="admin", role_source="local")
        )
        session.add(OidcRoleMapping(claim_value="finance", role="reader", updated_by="admin@example.com"))
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="injector", email="injector@example.com", groups=["finance"])
    response = _do_callback(client, state, token, extra_params={"role": "admin"})
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "injector@example.com"))
        assert user.role == "reader"


def test_role_mapping_change_is_audited(app, client):
    with app.state.session_factory() as session:
        session.add(
            User(email="existing-admin@example.com", password_hash="x", role="admin", role_source="local")
        )
        session.add(OidcRoleMapping(claim_value="finance", role="auditor", updated_by="admin@example.com"))
        session.commit()

    _configure_db_oidc(app, auto_provision_enabled=True)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="audited", email="audited@example.com", groups=["finance"])
    _do_callback(client, state, token)

    with app.state.session_factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "oidc_role_mapping_applied")
        ).all()
        assert len(events) == 1
        assert "auditor" in events[0].detail
