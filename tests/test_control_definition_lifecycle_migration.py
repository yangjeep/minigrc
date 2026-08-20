"""Migration tests for issue #42's 96fd3c35a6ae (add control
definition version lifecycle): the deterministic per-control backfill
and its bootstrap DomainEvent rows.

Uses the same programmatic alembic Config pattern as
tests/test_policy_lifecycle_migration.py / tests/test_framework_catalog_migration.py.
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PRE_MIGRATION_REVISION = "d3a8f61c9e27"

_INSERT_CONTROL_SQL = """
    INSERT INTO internal_controls
        (id, name, description, owner, status, review_frequency, created_at, updated_at)
    VALUES (:id, :name, '', '', :status, 'annual', :now, :now)
"""
_INSERT_REQUIREMENT_MAPPING_SQL = """
    INSERT INTO control_requirement_mappings (id, control_id, requirement_id, created_at)
    VALUES (:id, :control_id, :requirement_id, :now)
"""
_INSERT_FRAMEWORK_SQL = """
    INSERT INTO frameworks
        (id, name, version, description, is_placeholder_content, is_active, created_at, updated_at)
    VALUES (:id, 'F', '1', '', 0, 1, :now, :now)
"""
_INSERT_REQUIREMENT_SQL = """
    INSERT INTO framework_requirements
        (id, framework_id, reference_code, title, summary, display_order, created_at, updated_at)
    VALUES (:id, :framework_id, 'R1', 'Requirement 1', '', 0, :now, :now)
"""


def _alembic_config(db_path) -> Config:
    cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
    cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_existing_control_gets_one_effective_version(tmp_path):
    db_path = tmp_path / "control.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    control_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_CONTROL_SQL),
            {"id": control_id, "name": "Access Review", "status": "implemented", "now": now},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT control_id, version_number, name, status_snapshot, lifecycle_status "
                "FROM internal_control_versions WHERE control_id = :control_id"
            ),
            {"control_id": control_id},
        ).fetchone()
    assert row is not None
    assert row.version_number == 1
    assert row.name == "Access Review"
    assert row.status_snapshot == "implemented"
    assert row.lifecycle_status == "effective"


def test_backfill_snapshots_current_mapping_set(tmp_path):
    db_path = tmp_path / "mappings.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    control_id = uuid.uuid4().hex
    framework_id = uuid.uuid4().hex
    requirement_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_CONTROL_SQL),
            {"id": control_id, "name": "Access Review", "status": "implemented", "now": now},
        )
        conn.execute(text(_INSERT_FRAMEWORK_SQL), {"id": framework_id, "now": now})
        conn.execute(
            text(_INSERT_REQUIREMENT_SQL), {"id": requirement_id, "framework_id": framework_id, "now": now}
        )
        conn.execute(
            text(_INSERT_REQUIREMENT_MAPPING_SQL),
            {"id": uuid.uuid4().hex, "control_id": control_id, "requirement_id": requirement_id, "now": now},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT mapped_requirement_ids_json FROM internal_control_versions "
                "WHERE control_id = :control_id"
            ),
            {"control_id": control_id},
        ).fetchone()
    assert json.loads(row.mapped_requirement_ids_json) == [requirement_id]


def test_control_with_no_mappings_gets_empty_snapshot(tmp_path):
    db_path = tmp_path / "no_mappings.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    control_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_CONTROL_SQL),
            {"id": control_id, "name": "Bare Control", "status": "not_started", "now": now},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT mapped_requirement_ids_json FROM internal_control_versions "
                "WHERE control_id = :control_id"
            ),
            {"control_id": control_id},
        ).fetchone()
    assert json.loads(row.mapped_requirement_ids_json) == []


def test_backfill_inserts_bootstrap_events_marked_as_migration(tmp_path):
    db_path = tmp_path / "events.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    control_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_CONTROL_SQL), {"id": control_id, "name": "C", "status": "implemented", "now": now}
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type, actor_type, actor_id FROM domain_events "
                "WHERE aggregate_type = 'internal_control_version' ORDER BY aggregate_sequence"
            )
        ).fetchall()
    assert [r.event_type for r in rows] == [
        "InternalControlVersionDrafted",
        "InternalControlVersionMadeEffective",
    ]
    assert all(r.actor_type == "migration" for r in rows)
    assert all(r.actor_id is None for r in rows)


def test_old_control_definition_lifecycle_status_value_rejected_after_migration(tmp_path):
    db_path = tmp_path / "check_constraint.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    control_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_CONTROL_SQL), {"id": control_id, "name": "C", "status": "implemented", "now": now}
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO internal_control_versions "
                    "(id, control_id, version_number, name, status_snapshot, review_frequency_snapshot, "
                    "lifecycle_status, drafted_at) "
                    "VALUES (:id, :control_id, 1, 'C', 'implemented', 'annual', 'not_a_real_status', :now)"
                ),
                {"id": uuid.uuid4().hex, "control_id": control_id, "now": now},
            )


def test_upgrade_downgrade_upgrade_round_trip_does_not_duplicate_events(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    control_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_CONTROL_SQL), {"id": control_id, "name": "C", "status": "implemented", "now": now}
        )

    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PRE_MIGRATION_REVISION)
    command.upgrade(cfg, "head")  # must not raise a UNIQUE violation on re-insert

    with engine.connect() as conn:
        version_id = conn.execute(
            text("SELECT id FROM internal_control_versions WHERE control_id = :control_id"),
            {"control_id": control_id},
        ).scalar()
        version_count = conn.execute(
            text("SELECT count(*) FROM internal_control_versions WHERE control_id = :control_id"),
            {"control_id": control_id},
        ).scalar()
        event_count = conn.execute(
            text(
                "SELECT count(*) FROM domain_events "
                "WHERE aggregate_type = 'internal_control_version' AND aggregate_id = :version_id"
            ),
            {"version_id": version_id},
        ).scalar()
    assert version_count == 1  # projection row re-inserted, not duplicated or missing
    assert event_count == 2  # events not duplicated by the second upgrade


def test_one_effective_version_per_control_index_enforced_after_migration(tmp_path):
    db_path = tmp_path / "one_effective.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    control_id = uuid.uuid4().hex
    v1_id, v2_id = uuid.uuid4().hex, uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_CONTROL_SQL), {"id": control_id, "name": "C", "status": "not_started", "now": now}
        )
        conn.execute(
            text(
                "INSERT INTO internal_control_versions "
                "(id, control_id, version_number, name, status_snapshot, review_frequency_snapshot, "
                "lifecycle_status, drafted_at, effective_at) "
                "VALUES (:id, :control_id, 1, 'C', 'not_started', 'annual', 'effective', :now, :now)"
            ),
            {"id": v1_id, "control_id": control_id, "now": now},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO internal_control_versions "
                    "(id, control_id, version_number, name, status_snapshot, review_frequency_snapshot, "
                    "lifecycle_status, drafted_at, effective_at) "
                    "VALUES (:id, :control_id, 2, 'C', 'not_started', 'annual', 'effective', :now, :now)"
                ),
                {"id": v2_id, "control_id": control_id, "now": now},
            )


POSTGRES_TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TEST_DATABASE_URL not set — no live Postgres available")
def test_backfill_against_postgres():
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

        now = datetime.datetime.now(datetime.UTC)
        control_id = uuid.uuid4().hex
        with engine.begin() as conn:
            conn.execute(
                text(_INSERT_CONTROL_SQL),
                {"id": control_id, "name": "C", "status": "implemented", "now": now},
            )

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT lifecycle_status, version_number FROM internal_control_versions "
                    "WHERE control_id = :control_id"
                ),
                {"control_id": control_id},
            ).fetchone()
        assert row.lifecycle_status == "effective"
        assert row.version_number == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
