"""Migration tests for issue #31's c4e8a7f2b391 (add policy version
approval lifecycle): the deterministic per-policy backfill and its
bootstrap DomainEvent rows.

Uses the same programmatic alembic Config pattern as
tests/test_framework_catalog_migration.py / tests/test_rbac_migration.py.
"""

from __future__ import annotations

import datetime
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PRE_MIGRATION_REVISION = "96edf74d9219"

_INSERT_POLICY_SQL = """
    INSERT INTO policies
        (id, title, description, owner, status, archived, created_at, updated_at, source_type)
    VALUES (:id, :title, '', '', :status, 0, :now, :now, 'manual')
"""
_INSERT_VERSION_SQL = """
    INSERT INTO policy_versions
        (id, policy_id, version_number, original_filename, stored_filename, media_type,
         byte_size, sha256, uploader, change_note, created_at, source_type, captured_at)
    VALUES
        (:id, :policy_id, :version_number, 'f.pdf', 's.pdf', 'application/pdf', 100, 'hash',
         'u@example.com', '', :now, 'manual', :captured_at)
"""


def _alembic_config(db_path) -> Config:
    cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
    cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_latest_version_of_approved_policy_becomes_effective(tmp_path):
    db_path = tmp_path / "approved.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    policy_id = uuid.uuid4().hex
    v1_id, v2_id = uuid.uuid4().hex, uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "approved", "now": now}
        )
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {"id": v1_id, "policy_id": policy_id, "version_number": 1, "now": now, "captured_at": now},
        )
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {
                "id": v2_id,
                "policy_id": policy_id,
                "version_number": 2,
                "now": now,
                "captured_at": now + datetime.timedelta(hours=1),
            },
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = {
            row.id: (row.lifecycle_status, row.superseded_by_version_id)
            for row in conn.execute(
                text("SELECT id, lifecycle_status, superseded_by_version_id FROM policy_versions")
            ).fetchall()
        }
    assert rows[v1_id] == ("superseded", v2_id)
    assert rows[v2_id] == ("effective", None)


def test_latest_version_of_retired_policy_becomes_effective(tmp_path):
    db_path = tmp_path / "retired.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    policy_id = uuid.uuid4().hex
    v1_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "retired", "now": now}
        )
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {"id": v1_id, "policy_id": policy_id, "version_number": 1, "now": now, "captured_at": now},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT lifecycle_status FROM policy_versions WHERE id = :id"), {"id": v1_id}
        ).scalar()
    assert status == "effective"


def test_draft_policy_version_stays_draft(tmp_path):
    db_path = tmp_path / "draft.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    policy_id = uuid.uuid4().hex
    v1_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "draft", "now": now})
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {"id": v1_id, "policy_id": policy_id, "version_number": 1, "now": now, "captured_at": now},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT lifecycle_status FROM policy_versions WHERE id = :id"), {"id": v1_id}
        ).scalar()
    assert status == "draft"


def test_policy_with_no_versions_does_not_crash_migration(tmp_path):
    db_path = tmp_path / "empty.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_POLICY_SQL),
            {"id": uuid.uuid4().hex, "title": "Empty policy", "status": "draft", "now": now},
        )

    command.upgrade(cfg, "head")  # must not raise

    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM policy_versions")).scalar()
    assert count == 0


def test_backfill_inserts_bootstrap_events_marked_as_migration(tmp_path):
    db_path = tmp_path / "events.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    policy_id = uuid.uuid4().hex
    v1_id, v2_id = uuid.uuid4().hex, uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "approved", "now": now}
        )
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {"id": v1_id, "policy_id": policy_id, "version_number": 1, "now": now, "captured_at": now},
        )
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {
                "id": v2_id,
                "policy_id": policy_id,
                "version_number": 2,
                "now": now,
                "captured_at": now + datetime.timedelta(hours=1),
            },
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        events = conn.execute(
            text(
                "SELECT aggregate_id, event_type, actor_type, aggregate_sequence FROM domain_events "
                "WHERE aggregate_type = 'policy_version'"
            )
        ).fetchall()
    events_by_id = {e.aggregate_id: e for e in events}
    assert events_by_id[v1_id].event_type == "PolicyVersionSuperseded"
    assert events_by_id[v1_id].actor_type == "migration"
    assert events_by_id[v1_id].aggregate_sequence == 1
    assert events_by_id[v2_id].event_type == "PolicyVersionMadeEffective"
    assert events_by_id[v2_id].actor_type == "migration"


def test_old_lifecycle_status_value_rejected_after_migration(tmp_path):
    db_path = tmp_path / "reject.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    policy_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "draft", "now": now})

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy_versions (id, policy_id, version_number, original_filename, "
                    "stored_filename, media_type, byte_size, sha256, uploader, change_note, created_at, "
                    "source_type, captured_at, lifecycle_status) VALUES (:id, :policy_id, 1, 'f.pdf', "
                    "'s.pdf', 'application/pdf', 100, 'hash', 'u@example.com', '', :now, 'manual', :now, "
                    "'bogus')"
                ),
                {"id": uuid.uuid4().hex, "policy_id": policy_id, "now": now},
            )


def test_upgrade_downgrade_upgrade_round_trip_does_not_duplicate_events(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PRE_MIGRATION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    policy_id = uuid.uuid4().hex
    v1_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "approved", "now": now}
        )
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {"id": v1_id, "policy_id": policy_id, "version_number": 1, "now": now, "captured_at": now},
        )

    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PRE_MIGRATION_REVISION)
    command.upgrade(cfg, "head")  # must not raise a UNIQUE violation on re-insert

    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT lifecycle_status FROM policy_versions WHERE id = :id"), {"id": v1_id}
        ).scalar()
        event_count = conn.execute(
            text(
                "SELECT count(*) FROM domain_events "
                "WHERE aggregate_type = 'policy_version' AND aggregate_id = :id"
            ),
            {"id": v1_id},
        ).scalar()
    assert status == "effective"
    assert event_count == 1  # not duplicated by the second upgrade


def test_one_effective_version_per_policy_index_enforced_after_migration(tmp_path):
    db_path = tmp_path / "one_effective.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.datetime.now(datetime.UTC)
    policy_id = uuid.uuid4().hex
    v1_id, v2_id = uuid.uuid4().hex, uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "draft", "now": now})
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {"id": v1_id, "policy_id": policy_id, "version_number": 1, "now": now, "captured_at": now},
        )
        conn.execute(
            text(_INSERT_VERSION_SQL),
            {"id": v2_id, "policy_id": policy_id, "version_number": 2, "now": now, "captured_at": now},
        )
        conn.execute(
            text("UPDATE policy_versions SET lifecycle_status = 'effective' WHERE id = :id"), {"id": v1_id}
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE policy_versions SET lifecycle_status = 'effective' WHERE id = :id"),
                {"id": v2_id},
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
        policy_id = uuid.uuid4().hex
        v1_id, v2_id = uuid.uuid4().hex, uuid.uuid4().hex
        with engine.begin() as conn:
            conn.execute(
                text(_INSERT_POLICY_SQL), {"id": policy_id, "title": "P", "status": "approved", "now": now}
            )
            conn.execute(
                text(_INSERT_VERSION_SQL),
                {"id": v1_id, "policy_id": policy_id, "version_number": 1, "now": now, "captured_at": now},
            )
            conn.execute(
                text(_INSERT_VERSION_SQL),
                {
                    "id": v2_id,
                    "policy_id": policy_id,
                    "version_number": 2,
                    "now": now,
                    "captured_at": now + datetime.timedelta(hours=1),
                },
            )

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            rows = {
                row.id: row.lifecycle_status
                for row in conn.execute(text("SELECT id, lifecycle_status FROM policy_versions")).fetchall()
            }
        assert rows[v1_id] == "superseded"
        assert rows[v2_id] == "effective"
    finally:
        engine.dispose()
