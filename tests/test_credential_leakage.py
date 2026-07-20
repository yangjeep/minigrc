from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import encrypt
from app.models import AuditEvent, AwsConnection, GoogleDriveConnection
from tests.conftest import extract_csrf_token

TEST_KEY = Fernet.generate_key().decode()
SECRET_MARKER = "super-secret-value-should-never-leak"


def test_aws_external_id_never_leaks_in_edit_page(admin_client, app, admin_user):
    app.state.settings.encryption_key = TEST_KEY
    with app.state.session_factory() as session:
        session.add(AwsConnection(account_label="Prod", configured_by_user_id=admin_user.id))
        session.commit()

    edit_page = admin_client.get("/connectors/aws/edit")
    csrf_token = extract_csrf_token(edit_page.text)
    admin_client.post(
        "/connectors/aws",
        data={
            "account_label": "Prod",
            "expected_account_id": "",
            "role_arn": "",
            "external_id": SECRET_MARKER,
            "regions": "us-east-1",
            "csrf_token": csrf_token,
        },
    )

    response = admin_client.get("/connectors/aws/edit")
    assert SECRET_MARKER not in response.text

    with app.state.session_factory() as session:
        events = session.scalars(select(AuditEvent)).all()
    assert all(SECRET_MARKER not in (e.detail or "") for e in events)


def test_google_drive_refresh_token_never_leaks_on_connectors_page(admin_client, app, admin_user):
    with app.state.session_factory() as session:
        session.add(
            GoogleDriveConnection(
                connected_by_user_id=admin_user.id,
                encrypted_refresh_token=encrypt(SECRET_MARKER, key=TEST_KEY),
            )
        )
        session.commit()

    response = admin_client.get("/connectors/google-drive")
    assert SECRET_MARKER not in response.text
    assert encrypt(SECRET_MARKER, key=TEST_KEY) not in response.text

    with app.state.session_factory() as session:
        events = session.scalars(select(AuditEvent)).all()
    assert all(SECRET_MARKER not in (e.detail or "") for e in events)


def test_connections_index_never_renders_credential_material(admin_client, app, admin_user):
    with app.state.session_factory() as session:
        session.add(AwsConnection(account_label="Prod AWS", configured_by_user_id=admin_user.id))
        session.add(
            GoogleDriveConnection(
                connected_by_user_id=admin_user.id,
                encrypted_refresh_token=encrypt(SECRET_MARKER, key=TEST_KEY),
            )
        )
        session.commit()

    response = admin_client.get("/admin/connections")
    assert response.status_code == 200
    assert SECRET_MARKER not in response.text
    assert "ciphertext" not in response.text.lower()
