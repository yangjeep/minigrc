"""Google OpenID Connect login (Authorization Code flow).

Distinct from the Google Drive OAuth connection (a later commit on this
branch) — this module only authenticates a user's identity (`openid email
profile` scopes) and never requests Drive access. See
https://developers.google.com/identity/openid-connect/openid-connect

Signature/issuer/audience/expiry validation is delegated to
`google.oauth2.id_token.verify_oauth2_token` (fetches Google's current
signing keys itself); nonce, `email_verified`, and hosted-domain (`hd`)
checks are ours, since the library doesn't know about them.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
LOGIN_SCOPES = "openid email profile"
ALLOWED_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleOidcError(ValueError):
    """User-facing reason a Google sign-in attempt was rejected."""


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool
    hosted_domain: str | None


def new_state() -> str:
    return secrets.token_urlsafe(24)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def build_authorization_url(*, client_id: str, redirect_uri: str, state: str, nonce: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": LOGIN_SCOPES,
        "state": state,
        "nonce": nonce,
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_id_token(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str, timeout: float = 10.0
) -> str:
    """Exchange an authorization code for tokens; returns the raw ID token JWT.

    The ID token itself isn't persisted anywhere past this request.
    """
    try:
        response = httpx.post(
            TOKEN_ENDPOINT,
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
    except httpx.HTTPError as exc:
        raise GoogleOidcError("Could not reach Google to complete sign-in.") from exc

    payload = response.json()
    id_token_value = payload.get("id_token")
    if not id_token_value:
        raise GoogleOidcError("Google did not return an ID token.")
    return id_token_value


def verify_identity(
    raw_id_token: str,
    *,
    client_id: str,
    expected_nonce: str,
    allowed_domains: set[str],
) -> GoogleIdentity:
    """Validate a Google ID token and return the identity it asserts.

    `allowed_domains` empty means no Workspace-domain restriction is
    configured — any verified Google account may sign in. Non-empty means
    `hd` must be present and in the set.
    """
    try:
        claims = google_id_token.verify_oauth2_token(
            raw_id_token, google_auth_requests.Request(), audience=client_id
        )
    except ValueError as exc:
        raise GoogleOidcError("Google sign-in could not be verified.") from exc

    if claims.get("iss") not in ALLOWED_ISSUERS:
        raise GoogleOidcError("Unexpected token issuer.")
    if not claims.get("sub"):
        raise GoogleOidcError("Google did not return a stable account identifier.")
    if not expected_nonce or claims.get("nonce") != expected_nonce:
        raise GoogleOidcError("Invalid sign-in session (nonce mismatch).")
    if not claims.get("email_verified"):
        raise GoogleOidcError("Google account email is not verified.")

    hosted_domain = claims.get("hd")
    if allowed_domains and hosted_domain not in allowed_domains:
        raise GoogleOidcError("This Google Workspace domain is not permitted to sign in.")

    email = claims.get("email")
    if not email:
        raise GoogleOidcError("Google did not return an email address.")

    return GoogleIdentity(
        subject=claims["sub"],
        email=email,
        email_verified=bool(claims.get("email_verified")),
        hosted_domain=hosted_domain,
    )
