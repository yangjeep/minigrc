"""Migration tests for issue #37's 96edf74d9219 (expand user roles for
RBAC): the deterministic "user" -> "operator" rename plus the widened
ck_user_role CHECK constraint.

Uses the same programmatic alembic Config pattern as
tests/test_framework_catalog_migration.py — see that file's module
docstring for why the bare `alembic` CLI is unsuitable here.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PRE_MIGRATION_REVISION = "a248573ccdfe"

_INSERT_USER_SQL = """
    INSERT INTO users (id, email, password_hash, role, status, created_at, updated_at)
    VALUES (:id, :email, :password_hash, :role, 'active', datetime('now'), datetime('now'))
"""


def _alembic_config(db_path) -> Config:
    cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
    cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_user_role_renamed_to_operator_admin_untouched(tmp_path):
    db_path = tmp_path / "rbac.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    user_id = uuid.uuid4().hex
    admin_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_USER_SQL),
            {"id": user_id, "email": "member@example.com", "password_hash": "x", "role": "user"},
        )
        conn.execute(
            text(_INSERT_USER_SQL),
            {"id": admin_id, "email": "boss@example.com", "password_hash": "x", "role": "admin"},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        member_role = conn.execute(text("SELECT role FROM users WHERE id = :id"), {"id": user_id}).scalar()
        admin_role = conn.execute(text("SELECT role FROM users WHERE id = :id"), {"id": admin_id}).scalar()
    assert member_role == "operator"
    assert admin_role == "admin"


def test_old_role_value_rejected_after_migration(tmp_path):
    db_path = tmp_path / "rbac_reject.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(_INSERT_USER_SQL),
                {"id": uuid.uuid4().hex, "email": "bad@example.com", "password_hash": "x", "role": "user"},
            )


def test_new_role_values_accepted_after_migration(tmp_path):
    db_path = tmp_path / "rbac_accept.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for role in ("admin", "operator", "reader", "auditor"):
            conn.execute(
                text(_INSERT_USER_SQL),
                {"id": uuid.uuid4().hex, "email": f"{role}@example.com", "password_hash": "x", "role": role},
            )

    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM users")).scalar()
    assert count == 4


def test_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "rbac_roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    user_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_USER_SQL),
            {"id": user_id, "email": "roundtrip@example.com", "password_hash": "x", "role": "user"},
        )

    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PRE_MIGRATION_REVISION)

    with engine.connect() as conn:
        role = conn.execute(text("SELECT role FROM users WHERE id = :id"), {"id": user_id}).scalar()
    assert role == "user"  # downgrade maps operator back to user (see migration's downgrade docstring)

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        role = conn.execute(text("SELECT role FROM users WHERE id = :id"), {"id": user_id}).scalar()
    assert role == "operator"


POSTGRES_TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TEST_DATABASE_URL not set — no live Postgres available")
def test_role_rename_against_postgres():
    from sqlalchemy.engine import make_url

    from app.db import build_engine

    engine = build_engine(POSTGRES_TEST_URL)
    database_name = (make_url(POSTGRES_TEST_URL).database or "").lower()
    assert "test" in database_name or "uat" in database_name, (
        f"refusing to run a destructive migration test against {database_name!r}"
    )
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))

        cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
        cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
        cfg.set_main_option("sqlalchemy.url", POSTGRES_TEST_URL)
        command.upgrade(cfg, _PRE_MIGRATION_REVISION)

        user_id = uuid.uuid4().hex
        admin_id = uuid.uuid4().hex
        insert_sql = _INSERT_USER_SQL.replace("datetime('now')", "now()")
        with engine.begin() as conn:
            conn.execute(
                text(insert_sql),
                {"id": user_id, "email": "member@example.com", "password_hash": "x", "role": "user"},
            )
            conn.execute(
                text(insert_sql),
                {"id": admin_id, "email": "boss@example.com", "password_hash": "x", "role": "admin"},
            )

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            member_role = conn.execute(
                text("SELECT role FROM users WHERE id = :id"), {"id": user_id}
            ).scalar()
            admin_role = conn.execute(
                text("SELECT role FROM users WHERE id = :id"), {"id": admin_id}
            ).scalar()
        assert member_role == "operator"
        assert admin_role == "admin"

        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(insert_sql),
                    {
                        "id": uuid.uuid4().hex,
                        "email": "bad@example.com",
                        "password_hash": "x",
                        "role": "user",
                    },
                )
    finally:
        engine.dispose()
