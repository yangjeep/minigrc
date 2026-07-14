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
    }.issubset(tables)


def test_app_startup_seeds_example_dataset(app):
    from sqlalchemy import select

    from app.models import AuditEvent, Framework

    session_factory = app.state.session_factory
    with session_factory() as session:
        frameworks = session.scalars(select(Framework)).all()
        assert len(frameworks) == 1
        assert frameworks[0].is_placeholder_content is True

        events = session.scalars(select(AuditEvent)).all()
        assert len(events) > 0
