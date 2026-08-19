"""Route-level tests for /auth/oidc/* (issue #17), mirroring
tests/test_google_oidc.py's structure. `discover_provider_metadata` and
`exchange_code_for_id_token` are mocked (no real network calls needed for
route tests — that boundary is proven for real in tests/test_oidc.py's
fake-HTTP-provider test); `verify_identity`'s own signature/claims logic
runs for real against a locally-signed test JWT, so these tests exercise
genuine cryptographic verification, not just business logic.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from app.oidc import OidcError, ProviderMetadata

RSA_KEY = RSAKey.generate_key(2048, parameters={"kid": "route-test-key"}, private=True)

OIDC_LOGIN_SETTINGS = {
    "oidc_issuer": "https://idp.example.test",
    "oidc_client_id": "test-client-id",
    "oidc_client_secret": "test-client-secret",
    "public_base_url": "https://grc.example.com",
}


def _enable_oidc(app, allowed_domains: str = "") -> None:
    for key, value in OIDC_LOGIN_SETTINGS.items():
        setattr(app.state.settings, key, value)
    app.state.settings.oidc_allowed_domains = allowed_domains


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        issuer="https://idp.example.test",
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint="https://idp.example.test/token",
        jwks_uri="https://idp.example.test/jwks",
    )


def _sign(**claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.test",
        "sub": "subject-1",
        "aud": "test-client-id",
        "exp": now + 300,
        "iat": now,
        "email": "alice@example.com",
        "email_verified": True,
    }
    claims.update(claim_overrides)
    header = {"alg": "RS256", "kid": "route-test-key"}
    return jwt.encode(header, claims, RSA_KEY)


def _start_login(client) -> tuple[str, str]:
    with patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()):
        response = client.get("/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://idp.example.test/authorize")
    state = client.cookies.get("oidc_state")
    nonce = client.cookies.get("oidc_nonce")
    assert state and nonce
    return state, nonce


def _do_callback(client, state, *, code="abc", raw_token=None, error=""):
    params = {"state": state}
    if error:
        params["error"] = error
    else:
        params["code"] = code
    with (
        patch("app.routers.oidc.discover_provider_metadata", return_value=_metadata()),
        patch("app.routers.oidc.exchange_code_for_id_token", return_value=raw_token or "unused"),
        patch("app.oidc._fetch_key_set", return_value=KeySet([RSA_KEY])),
    ):
        return client.get("/auth/oidc/callback", params=params, follow_redirects=False)


def test_login_disabled_returns_404_when_not_configured(client):
    response = client.get("/auth/oidc/login")
    assert response.status_code == 404


def test_login_redirects_with_state_and_nonce(app, client):
    _enable_oidc(app)
    _start_login(client)


def test_login_flashes_error_when_discovery_fails(app, client):
    _enable_oidc(app)
    with patch("app.routers.oidc.discover_provider_metadata", side_effect=OidcError("boom")):
        response = client.get("/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_callback_rejects_state_mismatch(app, client):
    _enable_oidc(app)
    _start_login(client)
    response = _do_callback(client, "wrong-state")
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "state" in response.headers["location"].lower()


def test_callback_rejects_provider_error(app, client):
    _enable_oidc(app)
    state, _nonce = _start_login(client)
    response = _do_callback(client, state, error="access_denied")
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_callback_rejects_wrong_audience(app, client):
    _enable_oidc(app)
    state, nonce = _start_login(client)
    token = _sign(aud="different-client", nonce=nonce)
    response = _do_callback(client, state, raw_token=token)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_callback_rejects_nonce_mismatch(app, client):
    _enable_oidc(app)
    state, nonce = _start_login(client)
    token = _sign(nonce="a-different-nonce")
    response = _do_callback(client, state, raw_token=token)
    assert response.status_code == 303
    assert "nonce" in response.headers["location"].lower()


def test_callback_rejects_unverified_email(app, client):
    _enable_oidc(app)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, email_verified=False)
    response = _do_callback(client, state, raw_token=token)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_callback_rejects_disallowed_domain(app, client):
    _enable_oidc(app, allowed_domains="allowed.example.com")
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, email="alice@not-allowed.example.com")
    response = _do_callback(client, state, raw_token=token)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_callback_creates_new_active_user_and_logs_in(app, client):
    _enable_oidc(app)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="new-subject", email="brand-new@example.com")
    response = _do_callback(client, state, raw_token=token)
    assert response.status_code == 303
    assert "session" in response.cookies


def test_logout_after_oidc_login_revokes_session(app, client):
    from tests.conftest import extract_csrf_token

    _enable_oidc(app)
    state, nonce = _start_login(client)
    token = _sign(nonce=nonce, sub="logout-subject", email="logout-user@example.com")
    login_response = _do_callback(client, state, raw_token=token)
    assert "session" in login_response.cookies

    page = client.get("/")
    assert page.status_code == 200
    csrf_token = extract_csrf_token(page.text)

    logout_response = client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert logout_response.status_code == 303
    assert logout_response.headers["location"].startswith("/login")

    protected = client.get("/frameworks", follow_redirects=False)
    assert protected.status_code == 303
