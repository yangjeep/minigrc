"""Symmetric encryption for credentials stored at rest (Google Drive refresh
tokens; potentially other future integration secrets).

Requires an explicit `GRC_ENCRYPTION_KEY` — there is no silent fallback to
storing tokens in plaintext. Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class EncryptionNotConfiguredError(RuntimeError):
    """Raised when an encrypt/decrypt call is attempted without GRC_ENCRYPTION_KEY set."""


class DecryptionError(ValueError):
    """Raised when stored ciphertext can't be decrypted with the configured key."""


def _fernet(key: str) -> Fernet:
    if not key:
        raise EncryptionNotConfiguredError(
            "GRC_ENCRYPTION_KEY is not configured — required to store integration credentials."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfiguredError("GRC_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt(plaintext: str, *, key: str) -> str:
    return _fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str, *, key: str) -> str:
    try:
        return _fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError("Stored credential could not be decrypted with the configured key.") from exc
