"""Resolve the active BYOK AI provider configuration (issue #15).

Same "most-recently-updated settings row, else unconfigured; any secret
resolution failure degrades to not-usable rather than raising" shape as
`app/google_oidc_config.py`/`app/oidc_config.py`. There is no legacy
env-var fallback here (unlike those two) — BYOK AI is a new capability
with no prior deployment to preserve compatibility for.

`validate_provider_base_url` is the SSRF guard for the one genuinely
admin-configurable endpoint this feature introduces. See the design doc
(docs/superpowers/specs/2026-08-20-issue15-digital-compliance-tpm-design.md)
§4 for why it blocks cloud-metadata targets specifically rather than a
broad private-IP denylist.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import DecryptionError, EncryptionNotConfiguredError
from app.models import AiProviderSettings, Secret
from app.secrets import SecretNotResolvableError, resolve_secret

DEFAULT_TIMEOUT_SECONDS = 30

# The one class of endpoint with no legitimate reason to ever be a BYOK
# chat-completions target: cloud instance-metadata services, which is the
# textbook SSRF-to-credential-theft pivot. Not a general private-IP
# denylist — see the design doc for why self-hosted/private-network BYOK
# targets are otherwise allowed.
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal"})
# AWS's IPv6 metadata address (fd00:ec2::254) is a Unique Local Address,
# not link-local, so ipaddress.is_link_local below would not catch it.
_BLOCKED_IP_LITERALS = frozenset({ipaddress.ip_address("fd00:ec2::254")})


class InvalidProviderUrlError(ValueError):
    """User-facing reason a provider base URL was rejected."""


def validate_provider_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise InvalidProviderUrlError("Base URL must use http or https.")
    if not parsed.hostname:
        raise InvalidProviderUrlError("Base URL must include a host.")
    if parsed.username or parsed.password:
        raise InvalidProviderUrlError("Base URL must not embed credentials.")

    hostname = parsed.hostname.lower()
    if hostname in _BLOCKED_HOSTNAMES:
        raise InvalidProviderUrlError("Base URL may not target a cloud metadata endpoint.")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return  # a real hostname, not an IP literal — nothing further to check
    if address.is_link_local or address in _BLOCKED_IP_LITERALS:
        raise InvalidProviderUrlError("Base URL may not target a link-local/metadata address.")


@dataclass(frozen=True)
class ResolvedAiProviderConfig:
    usable: bool
    display_name: str
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int
    source: str  # "admin" | "unconfigured"


def _current_row(db: Session) -> AiProviderSettings | None:
    return db.scalar(select(AiProviderSettings).order_by(AiProviderSettings.updated_at.desc()).limit(1))


def _api_key(db: Session, row: AiProviderSettings, *, key: str) -> tuple[str, bool]:
    """Returns (api_key, resolution_failed). A row with no secret at all
    (`secret_id is None`) is a deliberate, valid configuration — some
    self-hosted gateways need no credential — and is never a failure. A
    row *with* a `secret_id` whose plaintext cannot be recovered (wrong/
    rotated GRC_ENCRYPTION_KEY, corrupt ciphertext, deleted row) is a
    real failure and must make the config unusable rather than silently
    proceed with an empty key, which would otherwise look identical to
    "no key needed"."""
    if row.secret_id is None:
        return "", False
    secret = db.get(Secret, row.secret_id)
    if secret is None:
        return "", True
    try:
        return resolve_secret(secret, key=key), False
    except (SecretNotResolvableError, EncryptionNotConfiguredError, DecryptionError):
        return "", True


_UNCONFIGURED = ResolvedAiProviderConfig(
    usable=False,
    display_name="",
    base_url="",
    model="",
    api_key="",
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    source="unconfigured",
)


def resolve_ai_provider_config(db: Session, *, encryption_key: str) -> ResolvedAiProviderConfig:
    """Never raises: a broken or absent BYOK config resolves to
    `usable=False` so every Digital TPM task can degrade gracefully
    rather than fail (issue #15's hard requirement)."""
    row = _current_row(db)
    if row is None or not row.enabled:
        return _UNCONFIGURED

    api_key, secret_resolution_failed = _api_key(db, row, key=encryption_key)
    try:
        validate_provider_base_url(row.base_url)
        url_valid = True
    except InvalidProviderUrlError:
        url_valid = False

    usable = bool(row.base_url and row.model and url_valid and not secret_resolution_failed)
    return ResolvedAiProviderConfig(
        usable=usable,
        display_name=row.display_name or "BYOK provider",
        base_url=row.base_url,
        model=row.model,
        api_key=api_key,
        timeout_seconds=row.timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        source="admin",
    )
