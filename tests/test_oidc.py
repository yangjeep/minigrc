"""Unit tests for app/oidc.py — the provider-neutral generic OIDC client
(issue #17). Uses a real RSA keypair and real joserfc signing/verification
throughout; one test stands up a real local HTTP server that behaves like
a standards-compliant OIDC provider (discovery + JWKS + signed ID token)
with no mocking at all, satisfying the issue's "demonstrate one
standards-compliant test provider" verification requirement.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from app.oidc import (
    OidcError,
    ProviderMetadata,
    build_authorization_url,
    discover_provider_metadata,
    exchange_code_for_id_token,
    new_nonce,
    new_state,
    verify_identity,
)

RSA_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key-1"}, private=True)


def _sign(claims: dict) -> str:
    header = {"alg": "RS256", "kid": "test-key-1"}
    return jwt.encode(header, claims, RSA_KEY)


def _base_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.test",
        "sub": "subject-123",
        "aud": "client-abc",
        "exp": now + 300,
        "iat": now,
        "nonce": "expected-nonce",
        "email": "alice@example.com",
        "email_verified": True,
    }
    claims.update(overrides)
    return claims


def _metadata(jwks_uri: str = "https://idp.example.test/jwks") -> ProviderMetadata:
    return ProviderMetadata(
        issuer="https://idp.example.test",
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint="https://idp.example.test/token",
        jwks_uri=jwks_uri,
    )


class _FakeResponse:
    def __init__(self, json_body=None, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_body


def test_new_state_and_nonce_are_distinct_and_url_safe():
    assert new_state() != new_state()
    assert new_nonce() != new_nonce()


def test_build_authorization_url_includes_required_params():
    metadata = _metadata()
    url = build_authorization_url(
        metadata,
        client_id="client-abc",
        redirect_uri="https://grc.example.com/auth/oidc/callback",
        state="s1",
        nonce="n1",
    )
    assert url.startswith("https://idp.example.test/authorize?")
    for fragment in ("client_id=client-abc", "state=s1", "nonce=n1", "response_type=code"):
        assert fragment in url


def test_discover_provider_metadata_rejects_issuer_mismatch(monkeypatch):
    def fake_get(url, timeout):
        return _FakeResponse(
            {
                "issuer": "https://different-issuer.test",
                "authorization_endpoint": "x",
                "token_endpoint": "y",
                "jwks_uri": "z",
            }
        )

    monkeypatch.setattr("app.oidc.httpx.get", fake_get)
    with pytest.raises(OidcError, match="does not match"):
        discover_provider_metadata("https://idp.example.test")


def test_discover_provider_metadata_rejects_missing_fields(monkeypatch):
    monkeypatch.setattr(
        "app.oidc.httpx.get", lambda url, timeout: _FakeResponse({"issuer": "https://idp.example.test"})
    )
    with pytest.raises(OidcError, match="missing a required field"):
        discover_provider_metadata("https://idp.example.test")


def test_discover_provider_metadata_rejects_network_failure(monkeypatch):
    import httpx

    def fake_get(url, timeout):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("app.oidc.httpx.get", fake_get)
    with pytest.raises(OidcError, match="Could not fetch"):
        discover_provider_metadata("https://idp.example.test")


def test_exchange_code_for_id_token_returns_id_token(monkeypatch):
    monkeypatch.setattr(
        "app.oidc.httpx.post", lambda url, data, timeout: _FakeResponse({"id_token": "abc.def.ghi"})
    )
    token = exchange_code_for_id_token(
        _metadata(),
        code="c1",
        client_id="client-abc",
        client_secret="secret",
        redirect_uri="https://grc.example.com/auth/oidc/callback",
    )
    assert token == "abc.def.ghi"


def test_exchange_code_for_id_token_rejects_missing_id_token(monkeypatch):
    monkeypatch.setattr("app.oidc.httpx.post", lambda url, data, timeout: _FakeResponse({}))
    with pytest.raises(OidcError, match="did not return an ID token"):
        exchange_code_for_id_token(
            _metadata(),
            code="c1",
            client_id="client-abc",
            client_secret="secret",
            redirect_uri="https://grc.example.com/auth/oidc/callback",
        )


def _jwks_response(monkeypatch):
    jwks = KeySet([RSA_KEY]).as_dict(private=False)
    monkeypatch.setattr("app.oidc.httpx.get", lambda url, timeout: _FakeResponse(jwks))


def test_verify_identity_accepts_a_valid_token(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims())
    identity = verify_identity(
        token,
        metadata=_metadata(),
        client_id="client-abc",
        expected_nonce="expected-nonce",
        allowed_domains=set(),
    )
    assert identity.issuer == "https://idp.example.test"
    assert identity.subject == "subject-123"
    assert identity.email == "alice@example.com"
    assert identity.email_verified is True


def test_verify_identity_rejects_wrong_audience(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims(aud="different-client"))
    with pytest.raises(OidcError):
        verify_identity(
            token,
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="expected-nonce",
            allowed_domains=set(),
        )


def test_verify_identity_rejects_wrong_issuer(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims(iss="https://attacker.test"))
    with pytest.raises(OidcError):
        verify_identity(
            token,
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="expected-nonce",
            allowed_domains=set(),
        )


def test_verify_identity_rejects_expired_token(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims(exp=int(time.time()) - 10))
    with pytest.raises(OidcError):
        verify_identity(
            token,
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="expected-nonce",
            allowed_domains=set(),
        )


def test_verify_identity_rejects_nonce_mismatch(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims())
    with pytest.raises(OidcError, match="nonce"):
        verify_identity(
            token,
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="wrong-nonce",
            allowed_domains=set(),
        )


def test_verify_identity_rejects_unverified_email(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims(email_verified=False))
    with pytest.raises(OidcError, match="not verified"):
        verify_identity(
            token,
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="expected-nonce",
            allowed_domains=set(),
        )


def test_verify_identity_rejects_disallowed_domain(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims(email="alice@not-allowed.test"))
    with pytest.raises(OidcError, match="domain"):
        verify_identity(
            token,
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="expected-nonce",
            allowed_domains={"allowed.test"},
        )


def test_verify_identity_accepts_allowed_domain(monkeypatch):
    _jwks_response(monkeypatch)
    token = _sign(_base_claims(email="alice@allowed.test"))
    identity = verify_identity(
        token,
        metadata=_metadata(),
        client_id="client-abc",
        expected_nonce="expected-nonce",
        allowed_domains={"allowed.test"},
    )
    assert identity.email == "alice@allowed.test"


def test_verify_identity_rejects_malformed_token(monkeypatch):
    _jwks_response(monkeypatch)
    with pytest.raises(OidcError):
        verify_identity(
            "not-a-jwt-at-all",
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="expected-nonce",
            allowed_domains=set(),
        )


def test_verify_identity_rejects_bad_signature(monkeypatch):
    _jwks_response(monkeypatch)
    other_key = RSAKey.generate_key(2048, parameters={"kid": "test-key-1"}, private=True)
    header = {"alg": "RS256", "kid": "test-key-1"}
    token = jwt.encode(header, _base_claims(), other_key)
    with pytest.raises(OidcError):
        verify_identity(
            token,
            metadata=_metadata(),
            client_id="client-abc",
            expected_nonce="expected-nonce",
            allowed_domains=set(),
        )


# --- Real HTTP fake provider: no mocking at all below this line ---------


class _FakeProviderHandler(BaseHTTPRequestHandler):
    """Behaves like a minimal standards-compliant OIDC provider: serves
    discovery + JWKS, and issues a fixed authorization code / signed ID
    token pair — enough to drive discover -> exchange -> verify for real,
    over a real socket, with real cryptographic signing."""

    key = RSA_KEY
    issued_nonce: str | None = None

    def _write_json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming convention
        if self.path == "/.well-known/openid-configuration":
            base = f"http://{self.headers['Host']}"
            self._write_json(
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/jwks",
                }
            )
        elif self.path == "/jwks":
            self._write_json(KeySet([self.key]).as_dict(private=False))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path == "/token":
            base = f"http://{self.headers['Host']}"
            now = int(time.time())
            claims = {
                "iss": base,
                "sub": "fake-provider-subject",
                "aud": "client-under-test",
                "exp": now + 300,
                "iat": now,
                "nonce": self.issued_nonce,
                "email": "carol@example.com",
                "email_verified": True,
            }
            header = {"alg": "RS256", "kid": self.key.as_dict()["kid"]}
            id_token = jwt.encode(header, claims, self.key)
            self._write_json({"id_token": id_token})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # keep test output quiet


@pytest.fixture
def fake_oidc_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_end_to_end_against_a_real_standards_shaped_provider(fake_oidc_provider):
    """No mocking anywhere in this test: real HTTP discovery, real JWKS
    fetch, real RSA-signed JWT, real joserfc signature/claims validation."""
    metadata = discover_provider_metadata(fake_oidc_provider)
    assert metadata.issuer == fake_oidc_provider
    assert metadata.jwks_uri == f"{fake_oidc_provider}/jwks"

    nonce = new_nonce()
    _FakeProviderHandler.issued_nonce = nonce

    raw_id_token = exchange_code_for_id_token(
        metadata,
        code="fixed-code",
        client_id="client-under-test",
        client_secret="anything",
        redirect_uri="https://grc.example.com/auth/oidc/callback",
    )
    identity = verify_identity(
        raw_id_token,
        metadata=metadata,
        client_id="client-under-test",
        expected_nonce=nonce,
        allowed_domains=set(),
    )
    assert identity.issuer == fake_oidc_provider
    assert identity.subject == "fake-provider-subject"
    assert identity.email == "carol@example.com"
