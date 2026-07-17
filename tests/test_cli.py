from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app import cli
from app.config import get_settings
from app.db import build_engine, make_session_factory
from app.models import User


def test_create_user_creates_user(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        exit_code = cli.create_user("New@Example.com")

    assert exit_code == 0

    engine = build_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "new@example.com"))
        assert user is not None
        assert user.password_hash != "a-strong-password"

    get_settings.cache_clear()


def test_create_user_rejects_mismatched_passwords(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test2.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["password-one", "password-two"]):
        exit_code = cli.create_user("mismatch@example.com")

    assert exit_code == 1
    get_settings.cache_clear()


def test_create_user_rejects_duplicate_email(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test3.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        assert cli.create_user("dup@example.com") == 0

    exit_code = cli.create_user("dup@example.com")
    assert exit_code == 1
    get_settings.cache_clear()
