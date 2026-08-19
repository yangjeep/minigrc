"""Headless UAT: generic, provider-neutral OIDC single sign-on (issue
#17) — an admin configures a standards-based OIDC provider by issuer URL
-> a new employee signs in via SSO and lands on the dashboard -> signs
out and signs back in, recognized as the same returning identity, not a
new account -> a second identity whose asserted email collides with an
already-registered local account is rejected, not silently merged. Runs
against the real app over a real socket.

Overrides tests/uat/conftest.py's `uat_server` fixture to set
GRC_PUBLIC_BASE_URL/GRC_ENCRYPTION_KEY before the app is built (same
technique as tests/uat/test_evidence_artifacts.py's S3 env override —
the base fixture has no hook for this otherwise).

The remote provider round trip (discovery/token exchange) is a real,
standards-shaped fake — `verify_identity`'s own signature/claims
validation still runs for real against a locally-signed JWT, over the
running UAT server's real socket/session/CSRF/DB boundary. A full,
unmocked real-HTTP fake provider is exercised separately in
tests/test_oidc.py; duplicating that HTTP server inside this harness
would add complexity without adding new proof (see design doc §7).
"""

from __future__ import annotations

import time
from unittest.mock import patch

import httpx
import pytest
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import select

from app.config import get_settings
from app.models import ExternalIdentity, User
from app.oidc import ProviderMetadata
from tests.uat.harness import (
    UAT_PASSWORD,
    LiveServer,
    build_uat_app,
    create_uat_user,
    extract_csrf_field,
)

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-oidc-admin@example.com"
ISSUER = "https://idp.uat.example.test"
CLIENT_ID = "uat-oidc-client"
RSA_KEY = RSAKey.generate_key(2048, parameters={"kid": "uat-oidc-key"}, private=True)


@pytest.fixture
def uat_server(tmp_path, monkeypatch):
    monkeypatch.setenv("GRC_PUBLIC_BASE_URL", "https://grc.example.com")
    monkeypatch.setenv("GRC_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    app = build_uat_app(tmp_path=tmp_path)
    with LiveServer(app) as server:
        yield server.base_url, app.state.session_factory


def _login_local(client, email):
    login_page = client.get("/login")
    csrf_token = extract_csrf_field(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": UAT_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _configure_oidc(admin_client):
    form = admin_client.get("/admin/authentication/oidc")
    csrf_token = extract_csrf_field(form.text)
    response = admin_client.post(
        "/admin/authentication/oidc",
        data={
            "enabled": "on",
            "display_name": "UAT SSO",
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret": "uat-client-secret",
            "allowed_domains": "",
            "auto_provision_enabled": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        issuer=ISSUER,
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token",
        jwks_uri=f"{ISSUER}/jwks",
    )


def _sign(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "uat-employee-1",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "email": "new-employee@example.com",
        "email_verified": True,
    }
    claims.update(overrides)
    header = {"alg": "RS256", "kid": "uat-oidc-key"}
    return jwt.encode(header, claims, RSA_KEY)


def test_admin_configures_sso_and_new_employee_signs_in_end_to_end(uat_server, uat_client, recorder):
    base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    admin_client = _login_local(uat_client, ADMIN_EMAIL)
    _configure_oidc(admin_client)

    with httpx.Client(base_url=base_url, event_hooks=recorder.event_hooks(), timeout=10.0) as employee_client:
        callback_response = _sso_login_with_correct_nonce(employee_client)

        assert callback_response.status_code == 303
        assert "flash_kind=error" not in callback_response.headers["location"]
        assert "session" in callback_response.cookies

        dashboard = employee_client.get("/", follow_redirects=False)
        assert dashboard.status_code == 200
        assert "new-employee@example.com" in dashboard.text or "Signed in" in dashboard.text

        with session_factory() as session:
            user = session.scalar(select(User).where(User.email == "new-employee@example.com"))
            assert user is not None
            assert user.status == "active"
            identity = session.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id))
            assert identity.issuer == ISSUER
            assert identity.subject == "uat-employee-1"

        # Sign out, then sign back in with the same external identity — recognized as
        # the same returning user, not re-created.
        page = employee_client.get("/")
        csrf_token = extract_csrf_field(page.text)
        employee_client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)

        second_callback = _sso_login_with_correct_nonce(employee_client)
        assert second_callback.status_code == 303
        assert "session" in second_callback.cookies

        with session_factory() as session:
            users = session.scalars(select(User).where(User.email == "new-employee@example.com")).all()
            assert len(users) == 1
            identities = session.scalars(
                select(ExternalIdentity).where(ExternalIdentity.subject == "uat-employee-1")
            ).all()
            assert len(identities) == 1

    # A different external identity asserting the SAME email is rejected, never merged.
    with httpx.Client(base_url=base_url, timeout=10.0) as attacker_client:
        with (
            patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()),
            patch("app.oidc._fetch_key_set", return_value=KeySet([RSA_KEY])),
        ):
            attacker_client.get("/auth/oidc/login", follow_redirects=False)
            nonce = attacker_client.cookies.get("oidc_nonce")
            state = attacker_client.cookies.get("oidc_state")
            token = _sign(sub="a-different-subject", nonce=nonce, email="new-employee@example.com")
            with patch("app.routers.oidc.exchange_code_for_id_token", return_value=token):
                collision_response = attacker_client.get(
                    "/auth/oidc/callback",
                    params={"code": "uat-code-2", "state": state},
                    follow_redirects=False,
                )
        assert collision_response.status_code == 303
        assert "flash_kind=error" in collision_response.headers["location"]
        assert "session" not in collision_response.cookies

    with session_factory() as session:
        rejected = session.scalars(
            select(ExternalIdentity).where(ExternalIdentity.subject == "a-different-subject")
        ).all()
        assert rejected == []


def _sso_login_with_correct_nonce(sso_client):
    with (
        patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()),
        patch("app.oidc._fetch_key_set", return_value=KeySet([RSA_KEY])),
    ):
        login_response = sso_client.get("/auth/oidc/login", follow_redirects=False)
        assert login_response.status_code == 303
        nonce = sso_client.cookies.get("oidc_nonce")
        state = sso_client.cookies.get("oidc_state")
        assert nonce and state

        token = _sign(nonce=nonce)
        with patch("app.routers.oidc.exchange_code_for_id_token", return_value=token):
            return sso_client.get(
                "/auth/oidc/callback", params={"code": "uat-code", "state": state}, follow_redirects=False
            )
