"""PostgreSQL compatibility: dialect-selection unit tests (always run) plus
a gated live-migration integration test (only runs when TEST_DATABASE_URL
points at a real Postgres — set by CI's postgres service container; skipped
locally where no Postgres is available).

See the architecture checkpoint on umbrella issue #5 and ADR #24 in
docs/decisions/architectural-decisions.md.
"""

from __future__ import annotations

import datetime
import logging
import os

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.db import build_engine, init_db, make_session_factory
from app.events import DomainEvent
from app.framework_catalog import reconcile_system_catalogs
from app.models import (
    ControlOccurrence,
    ExternalConnection,
    Framework,
    GoogleOidcSettings,
    ImportJob,
    InternalControl,
    Job,
    Secret,
    TrustCenterSection,
    TrustCenterSettings,
    User,
    new_id,
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
    "google_oidc_settings",
    "domain_events",
)

# Issue #11's tables — added separately from PIVOT_TABLES because they also
# need the partial-unique-index/CHECK-constraint behavior asserted below,
# not just "table exists."
ISSUE_11_TABLES = ("control_periods", "control_occurrences", "control_occurrence_evidence")


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


def test_init_db_passes_real_password_to_alembic_not_masked(monkeypatch):
    """Regression test for issue #40: engine.url's default str()/repr()
    masks the password as "***", but Alembic reparses whatever string
    init_db hands it into its own connection engine — a masked value there
    made password-authenticated PostgreSQL migrations fail authentication
    with the literal string "***". Intercepts what init_db actually passes
    to Alembic (no live Postgres connection needed — command.upgrade is
    replaced with a no-op that just captures the resolved config), so this
    runs in the always-on SQLite test job, not only the Postgres-gated one.
    """
    captured: dict[str, str] = {}

    def fake_upgrade(alembic_cfg, revision):
        captured["url"] = alembic_cfg.get_main_option("sqlalchemy.url")

    monkeypatch.setattr("app.db.command.upgrade", fake_upgrade)

    special_password = "p@ss:w/rd!$complicated"
    url = make_url("postgresql+psycopg://myuser@localhost:5432/mydb").set(password=special_password)
    engine = build_engine(url.render_as_string(hide_password=False))
    init_db(engine)

    assert "url" in captured, "init_db did not call command.upgrade"
    assert "***" not in captured["url"]
    assert make_url(captured["url"]).password == special_password


def test_init_db_does_not_leak_password_into_captured_logs(monkeypatch, caplog):
    """Negative counterpart to the test above: proves the real password
    passed to Alembic never ends up in application logs/diagnostics —
    only the masked str()/repr() form (or nothing) should ever be logged.

    Two gotchas this test must defeat, both surfaced by adversarial review:
    - `render_as_string(hide_password=False)` percent-encodes special
      characters, so a password containing them (e.g. "p@ss!") never
      appears in its raw form anywhere a leak would actually happen —
      only its *encoded* form does. A password made only of characters
      that never get percent-encoded (matching the convention in
      tests/test_credential_leakage.py's SECRET_MARKER) closes that gap:
      raw and rendered forms are identical, so asserting on the raw
      string is meaningful either way.
    - `migrations/env.py`'s `fileConfig(disable_existing_loggers=True)`
      permanently disables any logger not named in alembic.ini's
      `[loggers]` section the first time a REAL (non-monkeypatched)
      `init_db` runs anywhere in the test session (e.g. via the `app`
      fixture used by nearly every other test file) — silently making a
      caplog-based leak assertion inert for the rest of the run. Resetting
      `.disabled` on this module's own logger before capturing defeats
      that regardless of test execution order.
    """
    monkeypatch.setattr("app.db.command.upgrade", lambda alembic_cfg, revision: None)
    logging.getLogger("app.db").disabled = False

    secret_password = "super-secret-db-password-should-never-leak"
    url = make_url("postgresql+psycopg://myuser@localhost:5432/mydb").set(password=secret_password)
    real_url = url.render_as_string(hide_password=False)
    engine = build_engine(real_url)
    with caplog.at_level(logging.DEBUG):
        init_db(engine)

    logged_text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_password not in logged_text
    assert real_url not in logged_text


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
        assert set(ISSUE_11_TABLES).issubset(tables)

        user_columns = {col["name"] for col in inspect(engine).get_columns("users")}
        assert {"status", "google_subject"}.issubset(user_columns)

        framework_columns = {col["name"] for col in inspect(engine).get_columns("frameworks")}
        assert {"catalog_key", "is_primary"}.issubset(framework_columns)

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
            oidc_settings = GoogleOidcSettings(updated_by="pg-test")
            now = datetime.datetime.now(datetime.UTC)
            domain_event = DomainEvent(
                aggregate_type="pg_test_widget",
                aggregate_id="pgtest0000000000000000000000000",
                aggregate_sequence=1,
                event_type="WidgetCreated",
                payload_json='{"name": "pg-test"}',
                occurred_at=now,
                recorded_at=now,
            )
            session.add_all(
                [secret, connection, job, import_job, settings_row, section, oidc_settings, domain_event]
            )
            session.commit()

            assert session.query(Secret).filter_by(name="pg-secret").one().kind == "env_ref"
            assert session.query(Job).filter_by(job_type="pg_test").one().status == "pending"
            assert (
                session.query(TrustCenterSection).filter_by(title="PG Test Section").one().visibility
                == "internal"
            )
            assert session.query(GoogleOidcSettings).filter_by(updated_by="pg-test").one().enabled is False
            assert (
                session.query(DomainEvent).filter_by(aggregate_type="pg_test_widget").one().aggregate_sequence
                == 1
            )

            with pytest.raises(Exception):  # noqa: B017 - dialect-specific IntegrityError subclass
                session.add(TrustCenterSection(title="Bad visibility", visibility="not-a-real-value"))
                session.commit()
            session.rollback()

            with pytest.raises(Exception):  # noqa: B017 - dialect-specific IntegrityError subclass
                session.add(User(email="pg-bad-status@example.com", password_hash="x", status="not-a-status"))
                session.commit()
            session.rollback()

            # DomainEvent's own constraints are new to this issue, so — unlike
            # the two dialect-specific-subclass cases above — assert the
            # portable sqlalchemy.exc.IntegrityError specifically, since
            # SQLAlchemy wraps both the sqlite3- and psycopg-raised
            # CHECK/UNIQUE violations under that same base class.
            with pytest.raises(IntegrityError):
                session.add(
                    DomainEvent(
                        aggregate_type="pg_test_widget",
                        aggregate_id="pgtestbad000000000000000000000",
                        aggregate_sequence=0,
                        event_type="WidgetCreated",
                        payload_json="{}",
                        occurred_at=now,
                        recorded_at=now,
                    )
                )
                session.commit()
            session.rollback()

            with pytest.raises(IntegrityError):
                session.add(
                    DomainEvent(
                        aggregate_type="pg_test_widget",
                        aggregate_id="pgtest0000000000000000000000000",
                        aggregate_sequence=1,
                        event_type="WidgetDuplicateSequence",
                        payload_json="{}",
                        occurred_at=now,
                        recorded_at=now,
                    )
                )
                session.commit()
            session.rollback()

            # Issue #11: the ControlOccurrence schema is the first to combine
            # a nullable-column UniqueConstraint with a companion partial
            # unique index (uq_occurrence_control_due_no_period) — the most
            # dialect-sensitive piece of that migration, so it needs its own
            # direct proof against a real Postgres server, not just "CREATE
            # TABLE succeeded."
            control = InternalControl(name="PG cadence control", cadence_type="calendar")
            session.add(control)
            session.commit()

            due_at = datetime.datetime(2026, 3, 1, 23, 59, 59, tzinfo=datetime.UTC)
            session.add(
                ControlOccurrence(
                    id=new_id(),
                    control_id=control.id,
                    origin="manual",
                    due_at=due_at,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            # Two period-less (control_period_id IS NULL) occurrences sharing
            # a due_at must collide — this is exactly the case the plain
            # UniqueConstraint("control_id", "control_period_id", "due_at")
            # can NOT catch on its own (NULL != NULL in both SQLite and
            # Postgres), which is why the partial index exists.
            with pytest.raises(IntegrityError):
                session.add(
                    ControlOccurrence(
                        id=new_id(),
                        control_id=control.id,
                        origin="manual",
                        due_at=due_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.commit()
            session.rollback()

            # Multiple due_at=NULL period-less occurrences (event-driven,
            # nothing scheduled) must remain legal — the partial index must
            # not accidentally over-restrict this case.
            session.add_all(
                [
                    ControlOccurrence(
                        id=new_id(), control_id=control.id, origin="manual", created_at=now, updated_at=now
                    ),
                    ControlOccurrence(
                        id=new_id(), control_id=control.id, origin="manual", created_at=now, updated_at=now
                    ),
                ]
            )
            session.commit()

            # ck_control_cadence_interval_positive must reject a non-positive
            # interval on Postgres too (guards the unbounded-due-date-loop
            # defect a negative interval would otherwise cause).
            with pytest.raises(IntegrityError):
                session.add(
                    InternalControl(name="Bad cadence", cadence_type="calendar", cadence_interval_months=-1)
                )
                session.commit()
            session.rollback()

            # Issue #12: system framework catalog reconciliation must be
            # idempotent and the catalog_key uniqueness constraint must
            # hold on Postgres too.
            reconcile_system_catalogs(session)
            session.commit()
            iso = session.scalar(select(Framework).where(Framework.catalog_key == "iso27001-2022-sample"))
            soc2 = session.scalar(select(Framework).where(Framework.catalog_key == "soc2-2017-sample"))
            assert iso is not None and soc2 is not None
            assert len(iso.requirements) == 5
            assert len(soc2.requirements) == 9

            framework_count_before = session.scalar(select(func.count()).select_from(Framework))
            reconcile_system_catalogs(session)
            session.commit()
            assert session.scalar(select(func.count()).select_from(Framework)) == framework_count_before

            with pytest.raises(IntegrityError):
                session.add(Framework(catalog_key="iso27001-2022-sample", name="Dup", version="x"))
                session.commit()
            session.rollback()
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
