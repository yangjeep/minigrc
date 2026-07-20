"""Tests for external database connections (Feature 6).

See the architecture checkpoint on umbrella issue #5 and ADR #24.
"""

from __future__ import annotations

from app.connections import (
    ConnectionTestError,
    build_connection_url,
    run_connection_test,
)
from app.models import AuditEvent, ExternalConnection
from app.secrets import create_encrypted_secret

TEST_KEY = "6Vj0P8sJxG2h6y3Q9kZfXqW1mN4bR7tL0pC5dE8aFgs="


def _make_postgres_connection(app, **overrides) -> str:
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="conn-secret", plaintext="s3cret", actor="admin@example.com", key=TEST_KEY
        )
        session.flush()
        conn = ExternalConnection(
            name=overrides.get("name", "prod-readonly"),
            db_type="postgres",
            host="127.0.0.1",
            port=59999,  # deliberately unreachable — bounded-timeout failure path
            database_name="app",
            username="reader",
            secret_id=secret.id,
            tls_mode="prefer",
            owner="admin@example.com",
            created_by="admin@example.com",
        )
        session.add(conn)
        session.commit()
        return conn.id


def test_build_connection_url_postgres_never_includes_plaintext_in_repr(app):
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="url-secret", plaintext="s3cret", actor="admin@example.com", key=TEST_KEY
        )
        session.flush()
        conn = ExternalConnection(
            name="url-test",
            db_type="postgres",
            host="db.example.com",
            port=5432,
            database_name="app",
            username="reader",
            secret_id=secret.id,
            owner="admin@example.com",
            created_by="admin@example.com",
        )
        session.add(conn)
        session.flush()
        url = build_connection_url(conn, password="s3cret")
        assert url.drivername == "postgresql+psycopg"
        assert "s3cret" not in repr(url)
        assert "s3cret" not in str(url)  # SQLAlchemy URL.__str__ masks the password by default


def test_build_connection_url_mysql_uses_pymysql():
    conn = ExternalConnection(
        id="x",
        name="mysql-test",
        db_type="mysql",
        host="db.example.com",
        port=3306,
        database_name="app",
        username="reader",
        owner="admin@example.com",
        created_by="admin@example.com",
    )
    url = build_connection_url(conn, password="p")
    assert url.drivername == "mysql+pymysql"


def test_build_connection_url_sqlite_uses_file_path():
    conn = ExternalConnection(
        id="x",
        name="sqlite-test",
        db_type="sqlite",
        sqlite_path="/data/external/readonly.db",
        owner="admin@example.com",
        created_by="admin@example.com",
    )
    url = build_connection_url(conn, password=None)
    assert url.drivername == "sqlite"
    assert "readonly.db" in str(url)


def test_connection_test_against_unreachable_host_fails_within_bound(app):
    connection_id = _make_postgres_connection(app)
    with app.state.session_factory() as session:
        conn = session.get(ExternalConnection, connection_id)
        result = run_connection_test(
            session, conn, key=TEST_KEY, actor="admin@example.com", timeout_seconds=2
        )
        session.commit()
        assert result.status == "failure"
        # Sanitized: never echoes the resolved password, and never dumps a
        # raw driver conninfo string containing it.
        assert "s3cret" not in result.message

    with app.state.session_factory() as session:
        conn = session.get(ExternalConnection, connection_id)
        assert conn.last_test_status == "failure"
        assert conn.last_tested_at is not None
        assert "s3cret" not in (conn.last_test_message or "")


def test_connection_test_writes_audit_event_without_leaking_credential(app):
    connection_id = _make_postgres_connection(app)
    with app.state.session_factory() as session:
        conn = session.get(ExternalConnection, connection_id)
        run_connection_test(session, conn, key=TEST_KEY, actor="admin@example.com", timeout_seconds=2)
        session.commit()

    with app.state.session_factory() as session:
        events = session.query(AuditEvent).filter(AuditEvent.entity_id == connection_id).all()
        assert any(e.action == "test" for e in events)
        assert all("s3cret" not in e.detail for e in events)


def test_connection_repr_never_leaks_credential(app):
    connection_id = _make_postgres_connection(app)
    with app.state.session_factory() as session:
        conn = session.get(ExternalConnection, connection_id)
        assert "s3cret" not in repr(conn)


def test_generic_connection_requires_secret_holding_full_url():
    conn = ExternalConnection(
        id="x",
        name="generic-test",
        db_type="generic",
        secret_id="some-secret-id",
        owner="admin@example.com",
        created_by="admin@example.com",
    )
    # build_connection_url for generic needs the resolved secret value itself
    # (the full URL), not host/port fields — passing it via `password` here
    # would be wrong; generic mode raises to force callers through the
    # dedicated resolution path in test_connection.
    try:
        build_connection_url(conn, password="unused")
    except ConnectionTestError:
        pass
    else:
        raise AssertionError("expected ConnectionTestError for generic db_type via build_connection_url")
