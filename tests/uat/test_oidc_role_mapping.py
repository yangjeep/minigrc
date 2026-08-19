"""Headless UAT: OIDC claim/group-to-role mapping (issue #18) — an admin
configures a mapping and the role/group claim name -> a new SSO employee
whose asserted group matches gets the mapped role -> the same employee's
next login with a different group is re-mapped (promotion) -> the admin
manually overrides the role via the real Users edit form -> a subsequent
SSO login with yet another group no longer overrides the admin's pinned
role. Runs against the real app over a real socket. See
docs/superpowers/specs/2026-08-19-issue18-oidc-role-mapping-design.md §7.
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
from app.models import OidcProviderSettings, User
from app.oidc import ProviderMetadata
from tests.uat.harness import (
    UAT_PASSWORD,
    LiveServer,
    build_uat_app,
    create_uat_user,
    extract_csrf_field,
)

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-role-mapping-admin@example.com"
ISSUER = "https://idp.role-mapping.uat.example.test"
CLIENT_ID = "uat-role-mapping-client"
RSA_KEY = RSAKey.generate_key(2048, parameters={"kid": "uat-role-mapping-key"}, private=True)


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
            "role_claim_name": "groups",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]


def _add_mapping(admin_client, *, claim_value, role):
    page = admin_client.get("/admin/authentication/oidc/roles")
    csrf_token = extract_csrf_field(page.text)
    response = admin_client.post(
        "/admin/authentication/oidc/roles",
        data={"claim_value": claim_value, "role": role, "csrf_token": csrf_token},
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
        "sub": "uat-mapped-employee",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "email": "mapped-employee@example.com",
        "email_verified": True,
    }
    claims.update(overrides)
    header = {"alg": "RS256", "kid": "uat-role-mapping-key"}
    return jwt.encode(header, claims, RSA_KEY)


def _sso_login(sso_client, *, groups):
    with (
        patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()),
        patch("app.oidc._fetch_key_set", return_value=KeySet([RSA_KEY])),
    ):
        login_response = sso_client.get("/auth/oidc/login", follow_redirects=False)
        assert login_response.status_code == 303
        nonce = sso_client.cookies.get("oidc_nonce")
        state = sso_client.cookies.get("oidc_state")
        assert nonce and state

        token = _sign(nonce=nonce, groups=groups)
        with patch("app.routers.oidc.exchange_code_for_id_token", return_value=token):
            return sso_client.get(
                "/auth/oidc/callback", params={"code": "uat-code", "state": state}, follow_redirects=False
            )


def test_role_mapping_promotes_and_admin_pin_wins_over_later_login(uat_server, uat_client):
    base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    admin_client = _login_local(uat_client, ADMIN_EMAIL)
    _configure_oidc(admin_client)
    _add_mapping(admin_client, claim_value="finance", role="auditor")
    _add_mapping(admin_client, claim_value="sec-admins", role="admin")

    with httpx.Client(base_url=base_url, timeout=10.0) as employee_client:
        first = _sso_login(employee_client, groups=["finance"])
        assert first.status_code == 303
        assert "session" in first.cookies

        with session_factory() as session:
            user = session.scalar(select(User).where(User.email == "mapped-employee@example.com"))
            assert user.role == "auditor"
            assert user.role_source == "oidc_mapped"
            user_id = user.id

        # Sign out, sign back in with a different group -> promoted.
        page = employee_client.get("/")
        csrf_token = extract_csrf_field(page.text)
        employee_client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)

        second = _sso_login(employee_client, groups=["sec-admins"])
        assert second.status_code == 303
        with session_factory() as session:
            user = session.get(User, user_id)
            assert user.role == "admin"

    # Admin manually pins the role via the real Users edit form.
    edit_page = admin_client.get(f"/admin/users/{user_id}/edit")
    csrf_token = extract_csrf_field(edit_page.text)
    pin_response = admin_client.post(
        f"/admin/users/{user_id}/edit",
        data={"role": "reader", "status": "active", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert pin_response.status_code == 303
    with session_factory() as session:
        user = session.get(User, user_id)
        assert user.role == "reader"
        assert user.role_source == "local"

    # A later SSO login asserting a group mapped to "admin" no longer
    # overrides the admin's pinned role.
    with httpx.Client(base_url=base_url, timeout=10.0) as employee_client_2:
        third = _sso_login(employee_client_2, groups=["sec-admins"])
        assert third.status_code == 303
        assert "session" in third.cookies

    with session_factory() as session:
        user = session.get(User, user_id)
        assert user.role == "reader"
        assert user.role_source == "local"


def test_role_claim_name_is_configurable(uat_server, uat_client):
    """The admin can point role mapping at a differently-named claim
    (not the default 'groups') and it is honored."""
    base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    admin_client = _login_local(uat_client, ADMIN_EMAIL)
    _configure_oidc(admin_client)

    with session_factory() as session:
        row = session.scalar(select(OidcProviderSettings))
        row.role_claim_name = "roles"
        session.commit()

    _add_mapping(admin_client, claim_value="ops", role="operator")

    with httpx.Client(base_url=base_url, timeout=10.0) as employee_client:
        with (
            patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()),
            patch("app.oidc._fetch_key_set", return_value=KeySet([RSA_KEY])),
        ):
            login_response = employee_client.get("/auth/oidc/login", follow_redirects=False)
            nonce = employee_client.cookies.get("oidc_nonce")
            state = employee_client.cookies.get("oidc_state")
            token = _sign(nonce=nonce, sub="claim-name-subject", roles=["ops"])
            with patch("app.routers.oidc.exchange_code_for_id_token", return_value=token):
                callback_response = employee_client.get(
                    "/auth/oidc/callback",
                    params={"code": "uat-code", "state": state},
                    follow_redirects=False,
                )
        assert callback_response.status_code == 303
        assert login_response.status_code == 303

    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "mapped-employee@example.com"))
        assert user.role == "operator"
