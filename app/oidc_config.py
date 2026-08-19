"""Resolve the active generic OIDC login configuration (issue #17).

Two sources, in priority order:
1. `OidcProviderSettings` (Admin > Authentication > Generic OIDC) — the
   single most recently updated row, when `enabled`.
2. `GRC_OIDC_*` environment variables — for env-var-only deployments that
   never touch the Admin UI, same fallback shape as
   `app/google_oidc_config.py`'s `GRC_GOOGLE_OIDC_*`.

Any failure to resolve the stored client secret (unset/rotated
GRC_ENCRYPTION_KEY, corrupt ciphertext) degrades to "not usable" rather
than raising — a broken generic-OIDC config must never crash a request or
lock out local/break-glass login.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.crypto import DecryptionError, EncryptionNotConfiguredError
from app.models import OidcProviderSettings, Secret
from app.secrets import SecretNotResolvableError, resolve_secret


@dataclass(frozen=True)
class ResolvedOidcConfig:
    usable: bool
    display_name: str
    issuer: str
    client_id: str
    client_secret: str
    allowed_domains: set[str]
    auto_provision_enabled: bool
    redirect_uri: str
    source: str  # "admin" | "env" | "unconfigured"


def _client_secret(db: Session, row: OidcProviderSettings, *, key: str) -> str:
    if row.secret_id is None:
        return ""
    secret = db.get(Secret, row.secret_id)
    if secret is None:
        return ""
    try:
        return resolve_secret(secret, key=key)
    except (SecretNotResolvableError, EncryptionNotConfiguredError, DecryptionError):
        return ""


def resolve_oidc_config(db: Session, settings: Settings) -> ResolvedOidcConfig:
    row = db.scalar(select(OidcProviderSettings).order_by(OidcProviderSettings.updated_at.desc()).limit(1))
    if row is not None and row.enabled:
        client_secret = _client_secret(db, row, key=settings.encryption_key)
        allowed_domains = {d.strip().lower() for d in row.allowed_domains.split(",") if d.strip()}
        usable = bool(row.issuer and row.client_id and client_secret and settings.public_base_url)
        return ResolvedOidcConfig(
            usable=usable,
            display_name=row.display_name or "SSO",
            issuer=row.issuer,
            client_id=row.client_id,
            client_secret=client_secret,
            allowed_domains=allowed_domains,
            auto_provision_enabled=row.auto_provision_enabled,
            redirect_uri=settings.oidc_redirect_uri,
            source="admin",
        )

    return ResolvedOidcConfig(
        usable=settings.oidc_enabled,
        display_name=settings.oidc_display_name or "SSO",
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        allowed_domains=settings.oidc_allowed_domains_set,
        auto_provision_enabled=True,
        redirect_uri=settings.oidc_redirect_uri,
        source="env" if settings.oidc_enabled else "unconfigured",
    )
