"""Read-only MCP API tokens (issue #36).

Deliberately separate from `app/security.py`'s browser session tokens:
same hashing convention (`hash_session_token` — SHA-256 of the raw
token, reused directly rather than duplicated), but a distinct table
(`ApiToken`) with its own revocation/expiry semantics, since a long-lived
external-agent bearer token and a short-lived browser session cookie must
never be interchangeable.

The plaintext token exists only once, as `create_api_token`'s return
value — never logged, never persisted, never included in any audit
event/export (see app/audit.py's call below, which logs the token's
*name* and *id*, not its value).
"""

from __future__ import annotations

import dataclasses
import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import ApiToken, User
from app.security import hash_session_token

_TOKEN_PREFIX = "mgrc_"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _new_raw_token() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


@dataclasses.dataclass(frozen=True)
class CreatedApiToken:
    token: ApiToken
    plaintext: str


def create_api_token(
    session: Session,
    *,
    name: str,
    actor: User,
    expires_at: datetime.datetime | None = None,
) -> CreatedApiToken:
    """Issue a new read-only API token. `plaintext` is returned exactly
    once — the caller must display it immediately and never store it."""
    plaintext = _new_raw_token()
    token = ApiToken(
        name=name,
        token_hash=hash_session_token(plaintext),
        scope="read",
        created_by_user_id=actor.id,
        expires_at=expires_at,
    )
    session.add(token)
    session.flush()

    record_audit_event(
        session,
        entity_type="api_token",
        entity_id=token.id,
        action="create",
        detail=f"Created read-only API token '{name}' (id={token.id}).",
        actor=actor.email,
    )
    return CreatedApiToken(token=token, plaintext=plaintext)


def revoke_api_token(session: Session, token: ApiToken, *, actor: User) -> bool:
    """Revoke a token. Idempotent — revoking an already-revoked token is a
    safe no-op that returns False (vs True for an actual revocation)."""
    if token.revoked_at is not None:
        return False
    token.revoked_at = _now()
    record_audit_event(
        session,
        entity_type="api_token",
        entity_id=token.id,
        action="revoke",
        detail=f"Revoked API token '{token.name}' (id={token.id}).",
        actor=actor.email,
    )
    return True


def verify_api_token(session: Session, raw_token: str) -> ApiToken | None:
    """Look up and validate a presented bearer token. Returns None for any
    unknown, revoked, or expired token — never raises, matching
    app.security.verify_password's fail-closed shape. On success, updates
    `last_used_at` (the caller is responsible for committing)."""
    if not raw_token:
        return None
    token_hash = hash_session_token(raw_token)
    token = session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash))
    if token is None:
        return None
    if token.revoked_at is not None:
        return None
    if token.expires_at is not None and token.expires_at < _now():
        return None

    token.last_used_at = _now()
    return token
