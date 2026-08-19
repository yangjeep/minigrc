"""Migration tests for issue #13's a7c2e9f1b3d5 (add control test
population/sample/test and finding lifecycle). No data backfill — nine
brand-new tables (same category as #11's ControlOccurrence / #32's
evidence_artifacts), so this only proves the schema/constraints, mirroring
tests/test_evidence_artifact_migration.py's shape.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent

NEW_TABLES = {
    "control_test_populations",
    "control_test_population_items",
    "control_test_samples",
    "control_test_sample_items",
    "control_tests",
    "control_test_exceptions",
    "control_test_evidence_artifacts",
    "findings",
    "finding_remediation_updates",
    "finding_retests",
}


def _alembic_config(db_path) -> Config:
    cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
    cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_tables_created(tmp_path):
    db_path = tmp_path / "control_tests.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert NEW_TABLES.issubset(tables)


def test_control_test_result_check_constraint(tmp_path):
    db_path = tmp_path / "check_constraint.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO internal_controls (id, name, description, owner, status, "
                "review_frequency, created_at, updated_at) VALUES "
                "('c1', 'C', '', '', 'not_started', 'annual', datetime('now'), datetime('now'))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO people (id, email, display_name, employment_status, source, "
                "in_scope, created_at, updated_at) VALUES "
                "('p1', 'x@example.com', '', 'unknown', 'manual', 0, datetime('now'), datetime('now'))"
            )
        )

    with pytest.raises(Exception):  # noqa: B017 - sqlite raises IntegrityError via a generic path here
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO control_tests (id, control_id, procedure_description, "
                    "tester_person_id, performed_at, result, notes, created_at, updated_at) "
                    "VALUES ('t1', 'c1', '', 'p1', datetime('now'), 'not-a-real-result', '', "
                    "datetime('now'), datetime('now'))"
                )
            )


def test_downgrade_removes_tables(tmp_path):
    db_path = tmp_path / "downgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "f3a1b6c2d4e8")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert not (NEW_TABLES & tables)


def test_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "f3a1b6c2d4e8")
    command.upgrade(cfg, "head")  # must not raise

    engine = create_engine(f"sqlite:///{db_path}")
    assert "control_tests" in set(inspect(engine).get_table_names())


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

        tables = set(inspect(engine).get_table_names())
        assert NEW_TABLES.issubset(tables)
    finally:
        engine.dispose()
