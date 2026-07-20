"""PostgreSQL compatibility: dialect-selection unit tests (always run) plus
a gated live-migration integration test (only runs when TEST_DATABASE_URL
points at a real Postgres — set by CI's postgres service container; skipped
locally where no Postgres is available).

See the architecture checkpoint on umbrella issue #5 and ADR #24 in
docs/decisions/architectural-decisions.md.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text

from app.db import build_engine, init_db

POSTGRES_TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


def test_build_engine_defaults_to_sqlite_for_bare_path(tmp_path):
    engine = build_engine(str(tmp_path / "plain.db"))
    assert engine.dialect.name == "sqlite"


def test_build_engine_accepts_sqlite_url(tmp_path):
    db_path = tmp_path / "explicit.db"
    engine = build_engine(f"sqlite:///{db_path}")
    assert engine.dialect.name == "sqlite"


def test_build_engine_selects_postgres_dialect_from_url():
    engine = build_engine("postgresql+psycopg://user:pass@localhost:5432/dbname")
    assert engine.dialect.name == "postgresql"


def test_sqlite_pragma_listener_not_attached_for_postgres_url():
    engine = build_engine("postgresql+psycopg://user:pass@localhost:5432/dbname")
    from sqlalchemy import event

    from app.db import _set_sqlite_pragmas

    assert not event.contains(engine, "connect", _set_sqlite_pragmas)


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TEST_DATABASE_URL not set — no live Postgres available")
def test_migrations_apply_cleanly_against_postgres():
    engine = build_engine(POSTGRES_TEST_URL)
    try:
        init_db(engine)
        tables = set(inspect(engine).get_table_names())
        assert {"frameworks", "framework_requirements", "internal_controls", "risks", "users"}.issubset(
            tables
        )

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO frameworks (id, name, version, description, "
                    "is_placeholder_content, is_active, created_at, updated_at) "
                    "VALUES ('pgtest0000000000000000000000000', 'PG Test', '1.0', '', "
                    "false, true, now(), now())"
                )
            )
            row = conn.execute(
                text("SELECT name FROM frameworks WHERE id = 'pgtest0000000000000000000000000'")
            )
            assert row.scalar_one() == "PG Test"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
