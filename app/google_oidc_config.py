"""Resolve the active Google OIDC login configuration.

Two sources, in priority order:
1. `GoogleOidcSettings` (Admin > Authentication > Google OAuth) — the
   single most recently updated row, when `enabled`.
2. `GRC_GOOGLE_OIDC_*` environment variables (legacy, pre-Admin-UI
   deployments) — unchanged behavior, including always-on auto-provision,
   so existing env-var-only deployments keep working exactly as before.

Any failure to resolve the stored client secret (unset/rotated
GRC_ENCRYPTION_KEY, corrupt ciphertext) degrades to "not usable" rather
than raising — a broken Google OAuth config must never crash a request or
lock out local/break-glass login.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.crypto import DecryptionError, EncryptionNotConfiguredError
from app.models import GoogleOidcSettings, Secret
from app.secrets import SecretNotResolvableError, resolve_secret


@dataclass(frozen=True)
class ResolvedGoogleOidcConfig:
    usable: bool
    client_id: str
    client_secret: str
    allowed_domains: set[str]
    auto_provision_enabled: bool
    redirect_uri: str
    source: str  # "admin" | "env" | "unconfigured"


def _client_secret(db: Session, row: GoogleOidcSettings, *, key: str) -> str:
    if row.secret_id is None:
        return ""
    secret = db.get(Secret, row.secret_id)
    if secret is None:
        return ""
    try:
        return resolve_secret(secret, key=key)
    except (SecretNotResolvableError, EncryptionNotConfiguredError, DecryptionError):
        return ""


def resolve_google_oidc_config(db: Session, settings: Settings) -> ResolvedGoogleOidcConfig:
    row = db.scalar(select(GoogleOidcSettings).order_by(GoogleOidcSettings.updated_at.desc()).limit(1))
    if row is not None and row.enabled:
        client_secret = _client_secret(db, row, key=settings.encryption_key)
        allowed_domains = {d.strip().lower() for d in row.allowed_domains.split(",") if d.strip()}
        usable = bool(row.client_id and client_secret and settings.public_base_url)
        return ResolvedGoogleOidcConfig(
            usable=usable,
            client_id=row.client_id,
            client_secret=client_secret,
            allowed_domains=allowed_domains,
            auto_provision_enabled=row.auto_provision_enabled,
            redirect_uri=settings.google_oidc_redirect_uri,
            source="admin",
        )

    return ResolvedGoogleOidcConfig(
        usable=settings.google_oidc_enabled,
        client_id=settings.google_oidc_client_id,
        client_secret=settings.google_oidc_client_secret,
        allowed_domains=settings.google_oidc_allowed_domains_set,
        auto_provision_enabled=True,
        redirect_uri=settings.google_oidc_redirect_uri,
        source="env" if settings.google_oidc_enabled else "unconfigured",
    )
