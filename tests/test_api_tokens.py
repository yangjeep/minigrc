"""Unit tests for issue #36's API token module (app/api_tokens.py)."""

from __future__ import annotations

import datetime

from app.api_tokens import create_api_token, revoke_api_token, verify_api_token
from app.models import AuditEvent, User
from app.security import hash_password


def _make_admin(session) -> User:
    admin = User(email="admin@example.com", password_hash=hash_password("x"), role="admin")
    session.add(admin)
    session.flush()
    return admin


def test_create_api_token_returns_plaintext_once_and_stores_only_hash(app):
    with app.state.session_factory() as session:
        admin = _make_admin(session)
        created = create_api_token(session, name="ci-agent", actor=admin)
        session.commit()

        assert created.plaintext.startswith("mgrc_")
        assert created.token.token_hash != created.plaintext
        assert created.token.scope == "read"
        assert created.token.revoked_at is None


def test_create_api_token_records_audit_event(app):
    from sqlalchemy import select

    with app.state.session_factory() as session:
        admin = _make_admin(session)
        created = create_api_token(session, name="ci-agent", actor=admin)
        session.commit()

        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.entity_type == "api_token", AuditEvent.action == "create")
        )
        assert audit is not None
        assert audit.actor == admin.email
        assert created.plaintext not in audit.detail
        assert created.token.id in audit.detail


def test_verify_api_token_accepts_valid_token_and_updates_last_used_at(app):
    with app.state.session_factory() as session:
        admin = _make_admin(session)
        created = create_api_token(session, name="ci-agent", actor=admin)
        session.commit()

        assert created.token.last_used_at is None

        verified = verify_api_token(session, created.plaintext)
        session.commit()

        assert verified is not None
        assert verified.id == created.token.id
        assert verified.last_used_at is not None


def test_verify_api_token_rejects_unknown_token(app):
    with app.state.session_factory() as session:
        assert verify_api_token(session, "mgrc_does-not-exist") is None
        assert verify_api_token(session, "") is None


def test_verify_api_token_rejects_revoked_token(app):
    with app.state.session_factory() as session:
        admin = _make_admin(session)
        created = create_api_token(session, name="ci-agent", actor=admin)
        session.commit()

        revoke_api_token(session, created.token, actor=admin)
        session.commit()

        assert verify_api_token(session, created.plaintext) is None


def test_revoke_api_token_is_idempotent(app):
    with app.state.session_factory() as session:
        admin = _make_admin(session)
        created = create_api_token(session, name="ci-agent", actor=admin)
        session.commit()

        first = revoke_api_token(session, created.token, actor=admin)
        session.commit()
        second = revoke_api_token(session, created.token, actor=admin)
        session.commit()

        assert first is True
        assert second is False


def test_verify_api_token_rejects_expired_token(app):
    with app.state.session_factory() as session:
        admin = _make_admin(session)
        past = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=1)
        created = create_api_token(session, name="ci-agent", actor=admin, expires_at=past)
        session.commit()

        assert verify_api_token(session, created.plaintext) is None


def test_verify_api_token_accepts_token_with_future_expiry(app):
    with app.state.session_factory() as session:
        admin = _make_admin(session)
        future = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(hours=1)
        created = create_api_token(session, name="ci-agent", actor=admin, expires_at=future)
        session.commit()

        assert verify_api_token(session, created.plaintext) is not None
