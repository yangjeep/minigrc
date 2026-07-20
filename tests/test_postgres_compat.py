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

from app.db import build_engine, init_db, make_session_factory
from app.models import (
    ExternalConnection,
    ImportJob,
    Job,
    Secret,
    TrustCenterSection,
    TrustCenterSettings,
)

POSTGRES_TEST_URL = os.environ.get("TEST_DATABASE_URL", "")

# Tables added by this platform pivot (Features 4-12) — the pre-pivot
# assertion below only covered the original MVP schema, so a Postgres-only
# portability bug in any of these (e.g. a CHECK constraint that doesn't
# compile, a dialect-specific default) would never have been caught by CI.
PIVOT_TABLES = (
    "secrets",
    "external_connections",
    "jobs",
    "import_jobs",
    "trust_center_settings",
    "trust_center_sections",
)


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
        assert set(PIVOT_TABLES).issubset(tables)

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

        # ORM-level round trip through every pivot table — exercises each
        # table's CHECK constraints, defaults, and column types against a
        # real Postgres server, not just "does CREATE TABLE succeed."
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            secret = Secret(
                name="pg-secret", kind="env_ref", env_var_name="PG_TEST_SECRET", created_by="pg-test"
            )
            connection = ExternalConnection(
                name="pg-connection", db_type="postgres", secret_id=None, created_by="pg-test"
            )
            job = Job(job_type="pg_test", created_by="pg-test")
            import_job = ImportJob(source="cli", importer_name="pg_test", created_by="pg-test")
            settings_row = TrustCenterSettings()
            section = TrustCenterSection(title="PG Test Section")
            session.add_all([secret, connection, job, import_job, settings_row, section])
            session.commit()

            assert session.query(Secret).filter_by(name="pg-secret").one().kind == "env_ref"
            assert session.query(Job).filter_by(job_type="pg_test").one().status == "pending"
            assert (
                session.query(TrustCenterSection).filter_by(title="PG Test Section").one().visibility
                == "internal"
            )

            with pytest.raises(Exception):  # noqa: B017 - dialect-specific IntegrityError subclass
                session.add(TrustCenterSection(title="Bad visibility", visibility="not-a-real-value"))
                session.commit()
            session.rollback()
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
