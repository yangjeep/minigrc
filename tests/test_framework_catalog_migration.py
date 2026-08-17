"""Migration tests for issue #12's a248573ccdfe (add framework catalog_key
and is_primary flag), specifically its count-checked legacy-ISO backfill.

Uses a programmatic alembic Config (matching app/db.py::init_db's exact
pattern) rather than the bare `alembic` CLI — bare CLI commands read
alembic.ini's static `sqlalchemy.url` and never see GRC_ env var
overrides, so they silently operate on ./data/grc.db regardless of any
env var passed to the test process. This was discovered the hard way
while manually verifying this exact migration; see
docs/superpowers/specs/2026-08-17-issue12-soc2-primary-framework-design.md
§5.4 for the backfill's design.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = "/home/yangjeep/orca/workspaces/minigrc/epic"

_LEGACY_ISO_NAME = "ISO/IEC 27001:2022 Annex A (sample catalogue)"
_LEGACY_ISO_VERSION = "2022"

_INSERT_FRAMEWORK_SQL = """
    INSERT INTO frameworks
        (id, name, version, description, is_placeholder_content, is_active, created_at, updated_at)
    VALUES (:id, :name, :version, '', 1, 1, datetime('now'), datetime('now'))
"""


def _alembic_config(db_path) -> Config:
    cfg = Config(f"{PROJECT_ROOT}/alembic.ini")
    cfg.set_main_option("script_location", f"{PROJECT_ROOT}/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_backfill_zero_matches_is_a_safe_noop(tmp_path):
    db_path = tmp_path / "zero.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM frameworks")).scalar() == 0


def test_backfill_single_match_is_identified(tmp_path):
    db_path = tmp_path / "one.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "2f6df407060b")

    engine = create_engine(f"sqlite:///{db_path}")
    framework_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_FRAMEWORK_SQL),
            {"id": framework_id, "name": _LEGACY_ISO_NAME, "version": _LEGACY_ISO_VERSION},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT catalog_key FROM frameworks WHERE id = :id"), {"id": framework_id}
        ).fetchone()
        assert row.catalog_key == "iso27001-2022-sample"


def test_backfill_ambiguous_matches_are_left_untouched(tmp_path):
    db_path = tmp_path / "two.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "2f6df407060b")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for _ in range(2):
            conn.execute(
                text(_INSERT_FRAMEWORK_SQL),
                {"id": uuid.uuid4().hex, "name": _LEGACY_ISO_NAME, "version": _LEGACY_ISO_VERSION},
            )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT catalog_key FROM frameworks")).fetchall()
        assert len(rows) == 2
        assert all(row.catalog_key is None for row in rows)


def test_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(frameworks)")).fetchall()}
        assert {"catalog_key", "is_primary"}.issubset(columns)


def test_catalog_key_unique_constraint_enforced_after_migration(tmp_path):
    db_path = tmp_path / "unique.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO frameworks (id, name, version, description, is_placeholder_content, "
                "is_active, catalog_key, is_primary, created_at, updated_at) VALUES "
                "(:id1, 'A', '1', '', 1, 1, 'dup-key', 0, datetime('now'), datetime('now'))"
            ),
            {"id1": uuid.uuid4().hex},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO frameworks (id, name, version, description, is_placeholder_content, "
                    "is_active, catalog_key, is_primary, created_at, updated_at) VALUES "
                    "(:id2, 'B', '1', '', 1, 1, 'dup-key', 0, datetime('now'), datetime('now'))"
                ),
                {"id2": uuid.uuid4().hex},
            )
