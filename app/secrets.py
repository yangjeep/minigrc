"""Create/resolve service for the shared Secret model.

Two storage modes, chosen by the caller (see docs/decisions/architectural-
decisions.md ADR #24 and the architecture checkpoint on issue #5):

- `encrypted` — the caller's plaintext is Fernet-encrypted (app/crypto.py)
  before it ever touches the database; resolving requires the deployment's
  GRC_ENCRYPTION_KEY.
- `env_ref` — nothing secret is stored at all, only the *name* of an
  environment variable; resolving reads it from the process environment
  at call time. For Kubernetes deployments that mount a Secret as an env
  var rather than trusting this app with the plaintext.

Neither mode ever returns a stored value through an API response after
creation — callers that need the plaintext (e.g. a connection test) call
`resolve_secret` directly, server-side only.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.crypto import decrypt, encrypt
from app.models import Secret


class SecretNotResolvableError(RuntimeError):
    """Raised when an env_ref secret's target environment variable is unset."""


def create_encrypted_secret(session: Session, *, name: str, plaintext: str, actor: str, key: str) -> Secret:
    secret = Secret(
        name=name,
        kind="encrypted",
        ciphertext=encrypt(plaintext, key=key),
        created_by=actor,
    )
    session.add(secret)
    session.flush()
    record_audit_event(
        session,
        entity_type="secret",
        entity_id=secret.id,
        action="create",
        detail=f"Created encrypted secret '{name}'",
        actor=actor,
    )
    return secret


def create_env_ref_secret(session: Session, *, name: str, env_var_name: str, actor: str) -> Secret:
    secret = Secret(
        name=name,
        kind="env_ref",
        env_var_name=env_var_name,
        created_by=actor,
    )
    session.add(secret)
    session.flush()
    record_audit_event(
        session,
        entity_type="secret",
        entity_id=secret.id,
        action="create",
        detail=f"Created env-ref secret '{name}' -> ${env_var_name}",
        actor=actor,
    )
    return secret


def resolve_secret(secret: Secret, *, key: str) -> str:
    """Return the secret's plaintext value. Server-side use only — never
    return this value through an API response."""
    if secret.kind == "encrypted":
        return decrypt(secret.ciphertext, key=key)
    value = os.environ.get(secret.env_var_name)
    if value is None:
        raise SecretNotResolvableError(
            f"Secret '{secret.name}' references environment variable "
            f"'{secret.env_var_name}', which is not set in this process."
        )
    return value
