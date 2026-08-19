"""Migration tests for issue #30's f3a1b6c2d4e8 (add compliance scope and
in_scope flags). `compliance_scopes` is a brand-new table (no backfill,
same category as #32's evidence_artifacts); `in_scope` on `people`/
`vendor_systems` backfills every existing row to False via server_default —
see the migration's own docstring and design doc §4.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config(db_path) -> Config:
    cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
    cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_compliance_scopes_table_created(tmp_path):
    db_path = tmp_path / "compliance_scope.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    assert "compliance_scopes" in set(inspect(engine).get_table_names())


def test_in_scope_columns_added_and_default_to_false(tmp_path):
    db_path = tmp_path / "in_scope.db"
    cfg = _alembic_config(db_path)
    # Upgrade to the prior head first so a pre-existing person/vendor row
    # (created before this migration ran) proves the backfill, not just a
    # freshly-created column's Python-side default.
    command.upgrade(cfg, "d81f6c3a9e07")
    engine = create_engine(f"sqlite:///{db_path}")
    person_id = uuid.uuid4().hex
    vendor_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO people (id, email, display_name, employment_status, source, "
                "created_at, updated_at) VALUES (:id, 'x@example.com', '', 'unknown', 'manual', "
                "datetime('now'), datetime('now'))"
            ),
            {"id": person_id},
        )
        conn.execute(
            text(
                "INSERT INTO vendor_systems (id, system_name, vendor_name, lifecycle_status, "
                "access_url, primary_department, uses_shared_login, billing_frequency, currency, "
                "auto_renew, emergency_escalation_instructions, created_at, updated_at) "
                "VALUES (:id, 'GitHub', 'GitHub Inc.', 'active', '', '', 0, 'other', 'USD', "
                "'unknown', '', datetime('now'), datetime('now'))"
            ),
            {"id": vendor_id},
        )
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        person_in_scope = conn.execute(
            text("SELECT in_scope FROM people WHERE id = :id"), {"id": person_id}
        ).scalar()
        vendor_in_scope = conn.execute(
            text("SELECT in_scope FROM vendor_systems WHERE id = :id"), {"id": vendor_id}
        ).scalar()
    assert person_in_scope == 0
    assert vendor_in_scope == 0


def test_downgrade_removes_table_and_columns(tmp_path):
    db_path = tmp_path / "downgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "d81f6c3a9e07")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "compliance_scopes" not in set(inspector.get_table_names())
    people_columns = {c["name"] for c in inspector.get_columns("people")}
    vendor_columns = {c["name"] for c in inspector.get_columns("vendor_systems")}
    assert "in_scope" not in people_columns
    assert "in_scope" not in vendor_columns


def test_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "d81f6c3a9e07")
    command.upgrade(cfg, "head")  # must not raise

    engine = create_engine(f"sqlite:///{db_path}")
    assert "compliance_scopes" in set(inspect(engine).get_table_names())


POSTGRES_TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TEST_DATABASE_URL not set — no live Postgres available")
def test_migration_against_postgres():
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
        command.upgrade(cfg, "head")

        inspector = inspect(engine)
        assert "compliance_scopes" in set(inspector.get_table_names())
        people_columns = {c["name"] for c in inspector.get_columns("people")}
        assert "in_scope" in people_columns
    finally:
        engine.dispose()
