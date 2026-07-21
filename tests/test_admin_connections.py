from __future__ import annotations

import datetime

from app.models import AwsConnection, ExternalConnection, GoogleDriveConnection


def test_connections_index_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/connections")
    assert response.status_code == 403


def test_connections_index_loads_for_admin(admin_client):
    response = admin_client.get("/admin/connections")
    assert response.status_code == 200
    assert b"Connections" in response.content


def test_connections_index_lists_external_connection(admin_client, app):
    with app.state.session_factory() as session:
        session.add(ExternalConnection(name="Warehouse", db_type="postgres", created_by="admin@example.com"))
        session.commit()

    response = admin_client.get("/admin/connections")
    assert response.status_code == 200
    assert b"Warehouse" in response.content


def test_connections_index_formats_last_activity_timestamp(admin_client, app):
    """UAT finding: the index card showed a raw isoformat() timestamp with
    microseconds ('2026-07-21T00:20:10.063910') instead of the humanized
    '%Y-%m-%d %H:%M UTC' format used everywhere else in the app (e.g. the
    connection edit page's own 'Last test' line). See 2026-07-20
    admin/OAuth/IAM/connections consolidation worklog."""
    tested_at = datetime.datetime(2026, 7, 21, 0, 20, 10, 63910)
    with app.state.session_factory() as session:
        session.add(
            ExternalConnection(
                name="Warehouse",
                db_type="postgres",
                created_by="admin@example.com",
                last_test_status="success",
                last_tested_at=tested_at,
            )
        )
        session.commit()

    response = admin_client.get("/admin/connections")
    assert response.status_code == 200
    assert b"2026-07-21 00:20 UTC" in response.content
    assert b"2026-07-21T00:20:10.063910" not in response.content


def test_connections_index_lists_aws_connection(admin_client, app, admin_user):
    with app.state.session_factory() as session:
        session.add(AwsConnection(account_label="Prod AWS", configured_by_user_id=admin_user.id))
        session.commit()

    response = admin_client.get("/admin/connections")
    assert response.status_code == 200
    assert b"Prod AWS" in response.content


def test_connections_index_lists_active_google_drive_connection(admin_client, app, admin_user):
    with app.state.session_factory() as session:
        session.add(
            GoogleDriveConnection(
                connected_by_user_id=admin_user.id,
                encrypted_refresh_token="ciphertext",
            )
        )
        session.commit()

    response = admin_client.get("/admin/connections")
    assert response.status_code == 200
    assert b"Google Drive (OAuth)" in response.content


def test_connections_index_omits_revoked_google_drive_connection(admin_client, app, admin_user):
    with app.state.session_factory() as session:
        session.add(
            GoogleDriveConnection(
                connected_by_user_id=admin_user.id,
                encrypted_refresh_token="",
                revoked_at=datetime.datetime.now(datetime.UTC),
                revoked_by_user_id=admin_user.id,
            )
        )
        session.commit()

    response = admin_client.get("/admin/connections")
    assert response.status_code == 200
    assert b"Google Drive (OAuth)" not in response.content


def test_legacy_connections_path_redirects(admin_client):
    response = admin_client.get("/connections", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/admin/connections"
