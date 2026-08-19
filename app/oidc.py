"""Generic OpenID Connect login (Authorization Code flow), provider-neutral.

Distinct from `app/google_oidc.py`, which hardcodes Google's own endpoints
and Google's own token-verification helper. This module drives everything
from an admin-configured issuer URL via standards-based discovery
(`{issuer}/.well-known/openid-configuration`) and validates ID tokens with
`joserfc` (signature, `exp`/`iat`, `iss`, `aud`) rather than a
provider-specific library — so Authentik, Keycloak, Okta, Entra ID, and
any other standards-compliant provider all work through the same code
path. `nonce` and email-domain checks are ours, same split as the Google
module.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

LOGIN_SCOPES = "openid email profile"
DISCOVERY_PATH = "/.well-known/openid-configuration"


class OidcError(ValueError):
    """User-facing reason a generic OIDC sign-in attempt was rejected."""


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str
    email: str
    email_verified: bool


def new_state() -> str:
    return secrets.token_urlsafe(24)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def discover_provider_metadata(issuer: str, *, timeout: float = 10.0) -> ProviderMetadata:
    """Fetch and validate the provider's discovery document.

    Rejects a document whose own `issuer` field doesn't match the
    configured issuer exactly — prevents an issuer-confusion attack where
    a discovery endpoint asserts metadata for a different authority than
    the one an admin configured.
    """
    url = issuer.rstrip("/") + DISCOVERY_PATH
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError("Could not fetch the OIDC provider's discovery document.") from exc

    if document.get("issuer") != issuer:
        raise OidcError("The OIDC provider's discovery document does not match the configured issuer.")

    try:
        return ProviderMetadata(
            issuer=issuer,
            authorization_endpoint=document["authorization_endpoint"],
            token_endpoint=document["token_endpoint"],
            jwks_uri=document["jwks_uri"],
        )
    except KeyError as exc:
        raise OidcError("The OIDC provider's discovery document is missing a required field.") from exc


def build_authorization_url(
    metadata: ProviderMetadata, *, client_id: str, redirect_uri: str, state: str, nonce: str
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": LOGIN_SCOPES,
        "state": state,
        "nonce": nonce,
    }
    return f"{metadata.authorization_endpoint}?{urlencode(params)}"


def exchange_code_for_id_token(
    metadata: ProviderMetadata,
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    timeout: float = 10.0,
) -> str:
    """Exchange an authorization code for tokens; returns the raw ID token JWT."""
    try:
        response = httpx.post(
            metadata.token_endpoint,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError("Could not reach the OIDC provider to complete sign-in.") from exc

    id_token_value = payload.get("id_token")
    if not id_token_value:
        raise OidcError("The OIDC provider did not return an ID token.")
    return id_token_value


def _fetch_key_set(jwks_uri: str, *, timeout: float) -> KeySet:
    try:
        response = httpx.get(jwks_uri, timeout=timeout)
        response.raise_for_status()
        return KeySet.import_key_set(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError("Could not fetch the OIDC provider's signing keys.") from exc


def verify_identity(
    raw_id_token: str,
    *,
    metadata: ProviderMetadata,
    client_id: str,
    expected_nonce: str,
    allowed_domains: set[str],
    timeout: float = 10.0,
) -> OidcIdentity:
    """Validate a generic OIDC ID token and return the identity it asserts.

    Signature, expiry, issuer, and audience are verified by `joserfc`
    against the provider's own published JWKS. `nonce`, `email_verified`,
    and the optional domain allowlist are checked manually, since the
    library has no opinion on them.
    """
    key_set = _fetch_key_set(metadata.jwks_uri, timeout=timeout)
    try:
        token = jwt.decode(raw_id_token, key_set)
    except JoseError as exc:
        raise OidcError("The OIDC provider's sign-in could not be verified.") from exc

    claims_registry = jwt.JWTClaimsRegistry(
        iss={"essential": True, "value": metadata.issuer},
        aud={"essential": True, "value": client_id},
        exp={"essential": True},
    )
    try:
        claims_registry.validate(token.claims)
    except JoseError as exc:
        raise OidcError("The OIDC provider's sign-in could not be verified.") from exc

    claims = token.claims
    if not claims.get("sub"):
        raise OidcError("The OIDC provider did not return a stable account identifier.")
    if not expected_nonce or claims.get("nonce") != expected_nonce:
        raise OidcError("Invalid sign-in session (nonce mismatch).")
    if not claims.get("email_verified"):
        raise OidcError("The OIDC provider's account email is not verified.")

    email = claims.get("email")
    if not email:
        raise OidcError("The OIDC provider did not return an email address.")

    if allowed_domains:
        domain = email.rsplit("@", 1)[-1].lower()
        if domain not in allowed_domains:
            raise OidcError("This account's email domain is not permitted to sign in.")

    return OidcIdentity(
        issuer=metadata.issuer,
        subject=claims["sub"],
        email=email,
        email_verified=bool(claims.get("email_verified")),
    )
