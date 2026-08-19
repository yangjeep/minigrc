"""Migration tests for issue #32's d81f6c3a9e07 (add evidence artifact
repository). No data backfill — three brand-new tables (see design doc
§1/§4), so this only proves the schema/constraints, mirroring
tests/test_postgres_compat.py's PIVOT_TABLES-style checks.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config(db_path) -> Config:
    cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
    cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_tables_created(tmp_path):
    db_path = tmp_path / "evidence_artifacts.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert {
        "evidence_artifacts",
        "evidence_artifact_versions",
        "control_occurrence_evidence_artifacts",
    }.issubset(tables)


def test_unique_constraint_on_artifact_version_number(tmp_path):
    db_path = tmp_path / "unique_version.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")

    artifact_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO evidence_artifacts (id, title, description, created_at, updated_at) "
                "VALUES (:id, 'T', '', datetime('now'), datetime('now'))"
            ),
            {"id": artifact_id},
        )
        conn.execute(
            text(
                "INSERT INTO evidence_artifact_versions "
                "(id, artifact_id, version_number, object_key, original_filename, media_type, "
                "byte_size, sha256, source_type, captured_at, actor_type) "
                "VALUES (:id, :artifact_id, 1, 'k1', 'f.txt', 'text/plain', 1, 'hash1', 'manual', "
                "datetime('now'), 'user')"
            ),
            {"id": uuid.uuid4().hex, "artifact_id": artifact_id},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO evidence_artifact_versions "
                    "(id, artifact_id, version_number, object_key, original_filename, media_type, "
                    "byte_size, sha256, source_type, captured_at, actor_type) "
                    "VALUES (:id, :artifact_id, 1, 'k2', 'f2.txt', 'text/plain', 1, 'hash2', 'manual', "
                    "datetime('now'), 'user')"
                ),
                {"id": uuid.uuid4().hex, "artifact_id": artifact_id},
            )


def test_downgrade_removes_tables(tmp_path):
    db_path = tmp_path / "downgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "c4e8a7f2b391")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert (
        not {
            "evidence_artifacts",
            "evidence_artifact_versions",
            "control_occurrence_evidence_artifacts",
        }
        & tables
    )


def test_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "c4e8a7f2b391")
    command.upgrade(cfg, "head")  # must not raise

    engine = create_engine(f"sqlite:///{db_path}")
    assert "evidence_artifacts" in set(inspect(engine).get_table_names())


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
        assert {
            "evidence_artifacts",
            "evidence_artifact_versions",
            "control_occurrence_evidence_artifacts",
        }.issubset(tables)
    finally:
        engine.dispose()
