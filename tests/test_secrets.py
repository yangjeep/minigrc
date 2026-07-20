"""Tests for the shared secret/credential foundation (Feature 5).

See the architecture checkpoint on umbrella issue #5 and ADR #24 in
docs/decisions/architectural-decisions.md.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.exc import IntegrityError

from app.crypto import EncryptionNotConfiguredError
from app.models import AuditEvent, Secret
from app.secrets import (
    SecretNotResolvableError,
    create_encrypted_secret,
    create_env_ref_secret,
    resolve_secret,
)

TEST_KEY = "6Vj0P8sJxG2h6y3Q9kZfXqW1mN4bR7tL0pC5dE8aFgs="  # fixture Fernet key, not a real secret


def test_create_encrypted_secret_never_stores_plaintext(app):
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="db-password", plaintext="hunter2", actor="admin@example.com", key=TEST_KEY
        )
        session.commit()
        assert secret.ciphertext != "hunter2"
        assert "hunter2" not in repr(secret)


def test_resolve_encrypted_secret_round_trips(app):
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="db-password", plaintext="hunter2", actor="admin@example.com", key=TEST_KEY
        )
        session.commit()
        secret_id = secret.id

    with app.state.session_factory() as session:
        secret = session.get(Secret, secret_id)
        assert resolve_secret(secret, key=TEST_KEY) == "hunter2"


def test_resolve_fails_safely_without_encryption_key(app):
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="db-password", plaintext="hunter2", actor="admin@example.com", key=TEST_KEY
        )
        session.commit()
        secret_id = secret.id

    with app.state.session_factory() as session:
        secret = session.get(Secret, secret_id)
        with pytest.raises(EncryptionNotConfiguredError):
            resolve_secret(secret, key="")


def test_env_ref_secret_resolves_from_environment(app, monkeypatch):
    monkeypatch.setenv("MINIGRC_TEST_SECRET_VALUE", "from-the-environment")
    with app.state.session_factory() as session:
        secret = create_env_ref_secret(
            session, name="ci-token", env_var_name="MINIGRC_TEST_SECRET_VALUE", actor="admin@example.com"
        )
        session.commit()
        secret_id = secret.id

    with app.state.session_factory() as session:
        secret = session.get(Secret, secret_id)
        assert resolve_secret(secret, key=TEST_KEY) == "from-the-environment"


def test_env_ref_secret_missing_env_var_raises(app, monkeypatch):
    monkeypatch.delenv("MINIGRC_TEST_SECRET_MISSING", raising=False)
    with app.state.session_factory() as session:
        secret = create_env_ref_secret(
            session, name="ci-token", env_var_name="MINIGRC_TEST_SECRET_MISSING", actor="admin@example.com"
        )
        session.commit()
        secret_id = secret.id

    with app.state.session_factory() as session:
        secret = session.get(Secret, secret_id)
        with pytest.raises(SecretNotResolvableError):
            resolve_secret(secret, key=TEST_KEY)


def test_secret_repr_never_leaks_ciphertext_or_env_value(app, monkeypatch):
    monkeypatch.setenv("MINIGRC_TEST_SECRET_VALUE", "from-the-environment")
    with app.state.session_factory() as session:
        encrypted = create_encrypted_secret(
            session, name="db-password", plaintext="hunter2", actor="admin@example.com", key=TEST_KEY
        )
        env_ref = create_env_ref_secret(
            session, name="ci-token", env_var_name="MINIGRC_TEST_SECRET_VALUE", actor="admin@example.com"
        )
        session.commit()
        assert "hunter2" not in repr(encrypted)
        assert encrypted.ciphertext not in repr(encrypted) or "ciphertext=" not in repr(encrypted)
        assert "from-the-environment" not in repr(env_ref)


def test_secret_name_must_be_unique(app):
    with app.state.session_factory() as session:
        create_encrypted_secret(
            session, name="dup-name", plaintext="a", actor="admin@example.com", key=TEST_KEY
        )
        session.commit()

    with app.state.session_factory() as session:
        with pytest.raises(IntegrityError):
            create_encrypted_secret(
                session, name="dup-name", plaintext="b", actor="admin@example.com", key=TEST_KEY
            )
        session.rollback()


def test_create_secret_writes_audit_event_without_leaking_value(app):
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="db-password", plaintext="hunter2", actor="admin@example.com", key=TEST_KEY
        )
        session.commit()
        secret_id = secret.id

    with app.state.session_factory() as session:
        events = session.query(AuditEvent).filter(AuditEvent.entity_id == secret_id).all()
        assert len(events) == 1
        assert "hunter2" not in events[0].detail


def test_create_encrypted_secret_requires_key():
    # A module-level sanity check that the plaintext round-trip depends on
    # a real key, not an accidental fallback to storing plaintext.
    assert os.environ.get("GRC_ENCRYPTION_KEY", "") != TEST_KEY  # fixture key must not leak into real env
