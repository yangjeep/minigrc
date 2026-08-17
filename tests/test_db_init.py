from sqlalchemy import inspect

from app.db import build_engine, init_db


def test_init_db_creates_expected_tables(tmp_path):
    engine = build_engine(str(tmp_path / "init_test.db"))
    init_db(engine)

    tables = set(inspect(engine).get_table_names())
    assert {
        "frameworks",
        "framework_requirements",
        "internal_controls",
        "control_requirement_mappings",
        "risks",
        "audit_events",
        "users",
        "user_sessions",
        "policies",
        "policy_versions",
        "requirement_assessments",
        "requirement_notes",
        "domain_events",
    }.issubset(tables)


def test_app_startup_seeds_example_dataset(app):
    from sqlalchemy import select

    from app.models import AuditEvent, Framework

    session_factory = app.state.session_factory
    with session_factory() as session:
        # Issue #12: startup now reconciles two system framework catalogs
        # (ISO 27001, SOC 2) unconditionally, in addition to the demo
        # InternalControl/Risk dataset — see app/framework_catalog.py.
        frameworks = session.scalars(select(Framework)).all()
        assert len(frameworks) == 2
        assert all(f.is_placeholder_content is True for f in frameworks)

        events = session.scalars(select(AuditEvent)).all()
        assert len(events) > 0
