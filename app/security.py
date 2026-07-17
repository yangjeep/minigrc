"""Password hashing, session tokens, and CSRF tokens.

Deliberately hand-rolled from stdlib + pwdlib rather than a bigger identity
platform — see CLAUDE.md's authentication constraints. No JWTs: sessions are
opaque random tokens, looked up server-side on every request.
"""

from __future__ import annotations

import hashlib
import secrets

from pwdlib import PasswordHash

SESSION_COOKIE_NAME = "session"
CSRF_COOKIE_NAME = "csrf_token"

_password_hasher = PasswordHash.recommended()

# Precomputed hash of a value nobody will ever type, used to burn roughly the
# same amount of time as a real verify() call when the looked-up user does
# not exist — keeps login timing from revealing whether an email is registered.
_DUMMY_HASH = _password_hasher.hash(secrets.token_hex(32))


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        _password_hasher.verify(password, _DUMMY_HASH)
        return False
    try:
        return _password_hasher.verify(password, password_hash)
    except Exception:
        return False


def normalize_email(email: str) -> str:
    return email.strip().lower()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_value: str | None, submitted_value: str | None) -> bool:
    if not cookie_value or not submitted_value:
        return False
    return secrets.compare_digest(cookie_value, submitted_value)
