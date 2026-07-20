from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import cli
from app.config import get_settings
from app.db import build_engine, make_session_factory
from app.deps import require_admin
from app.models import AuditEvent, User


def test_first_created_user_becomes_admin(tmp_path, monkeypatch):
    db_path = tmp_path / "admin_cli.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        assert cli.create_user("first@example.com") == 0
    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        assert cli.create_user("second@example.com") == 0

    engine = build_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        first = session.scalar(select(User).where(User.email == "first@example.com"))
        second = session.scalar(select(User).where(User.email == "second@example.com"))
        assert first.role == "admin"
        assert second.role == "user"

    get_settings.cache_clear()


def test_promote_admin_grants_role_and_audits(tmp_path, monkeypatch):
    db_path = tmp_path / "promote.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        assert cli.create_user("first@example.com") == 0  # becomes admin automatically
    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        assert cli.create_user("member@example.com") == 0

    assert cli.promote_admin("Member@Example.com") == 0

    engine = build_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        member = session.scalar(select(User).where(User.email == "member@example.com"))
        assert member.role == "admin"
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "user", AuditEvent.action == "promote_admin")
        ).all()
        assert len(events) == 1
        assert events[0].actor == "cli"

    get_settings.cache_clear()


def test_promote_admin_rejects_unknown_email(tmp_path, monkeypatch):
    db_path = tmp_path / "promote_missing.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    assert cli.promote_admin("nobody@example.com") == 1
    get_settings.cache_clear()


def test_promote_admin_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "promote_idempotent.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        assert cli.create_user("solo@example.com") == 0  # already admin (first user)

    assert cli.promote_admin("solo@example.com") == 0

    engine = build_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "user", AuditEvent.action == "promote_admin")
        ).all()
        assert events == []  # no-op path doesn't write a duplicate audit event

    get_settings.cache_clear()


def test_require_admin_rejects_regular_user():
    regular = User(email="user@example.com", password_hash="x", role="user")
    with pytest.raises(HTTPException) as exc_info:
        require_admin(user=regular)
    assert exc_info.value.status_code == 403


def test_require_admin_allows_admin_user():
    admin = User(email="admin@example.com", password_hash="x", role="admin")
    assert require_admin(user=admin) is admin


def test_admin_shell_requires_admin_role(logged_in_client):
    response = logged_in_client.get("/admin")
    assert response.status_code == 403


def test_admin_shell_redirects_anonymous_to_login(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_shell_loads_for_admin(admin_client):
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert "Admin" in response.text


def test_admin_nav_link_hidden_from_regular_users(logged_in_client):
    response = logged_in_client.get("/")
    assert 'href="/admin"' not in response.text


def test_admin_nav_link_shown_to_admins(admin_client):
    response = admin_client.get("/")
    assert 'href="/admin"' in response.text
