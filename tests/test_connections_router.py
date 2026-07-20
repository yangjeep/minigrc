"""HTTP-level tests for the connections admin router (Feature 6)."""

from __future__ import annotations

from app.models import ExternalConnection
from tests.conftest import extract_csrf_token

TEST_KEY = "6Vj0P8sJxG2h6y3Q9kZfXqW1mN4bR7tL0pC5dE8aFgs="


def test_connections_list_requires_admin(logged_in_client):
    response = logged_in_client.get("/connections", follow_redirects=False)
    assert response.status_code == 403


def test_connections_list_allows_admin(admin_client):
    response = admin_client.get("/connections")
    assert response.status_code == 200
    assert b"External Database Connections" in response.content


def test_create_connection_as_admin(admin_client, app):
    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/connections/new")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/connections",
        data={
            "name": "prod-readonly",
            "db_type": "postgres",
            "host": "db.example.com",
            "port": "5432",
            "database_name": "app",
            "username": "reader",
            "tls_mode": "require",
            "owner": "admin@example.com",
            "secret_value": "s3cret",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with app.state.session_factory() as session:
        conn = session.query(ExternalConnection).filter_by(name="prod-readonly").one()
        assert conn.secret_id is not None
        assert conn.host == "db.example.com"


def test_create_connection_as_regular_user_forbidden(logged_in_client):
    response = logged_in_client.post(
        "/connections",
        data={"name": "x", "db_type": "postgres", "csrf_token": "irrelevant"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_create_connection_rejects_mismatched_csrf(admin_client):
    response = admin_client.post(
        "/connections",
        data={
            "name": "bad-csrf",
            "db_type": "sqlite",
            "sqlite_path": "/tmp/x.db",
            "csrf_token": "wrong-token-value",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_connection_list_never_exposes_secret_value(admin_client, app):
    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/connections/new")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/connections",
        data={
            "name": "secret-exposure-check",
            "db_type": "postgres",
            "host": "db.example.com",
            "username": "reader",
            "secret_value": "totally-secret-value",
            "csrf_token": csrf_token,
        },
    )
    grid_response = admin_client.get("/api/registers/connections")
    assert b"totally-secret-value" not in grid_response.content

    with app.state.session_factory() as session:
        conn = session.query(ExternalConnection).filter_by(name="secret-exposure-check").one()
        edit_page = admin_client.get(f"/connections/{conn.id}/edit")
    assert b"totally-secret-value" not in edit_page.content
