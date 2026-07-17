from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import DecryptionError, EncryptionNotConfiguredError, decrypt, encrypt
from app.google_drive import DriveFileMetadata, GoogleDriveError, parse_drive_file_id
from app.models import AuditEvent, GoogleDriveConnection, Policy
from tests.conftest import extract_csrf_token

VALID_PDF = b"%PDF-1.4\n%mock pdf content for tests\n%%EOF"
VALID_PDF_V2 = b"%PDF-1.4\n%mock pdf content v2\n%%EOF"
FAKE_PDF = b"this is not a real pdf"
TEST_KEY = Fernet.generate_key().decode()

DRIVE_SETTINGS = {
    "google_drive_client_id": "drive-client-id",
    "google_drive_client_secret": "drive-client-secret",
    "public_base_url": "https://grc.example.com",
    "encryption_key": TEST_KEY,
}


def _enable_drive(app) -> None:
    for key, value in DRIVE_SETTINGS.items():
        setattr(app.state.settings, key, value)


def _seed_active_connection(app, admin_user) -> str:
    with app.state.session_factory() as session:
        connection = GoogleDriveConnection(
            connected_by_user_id=admin_user.id,
            granted_scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_refresh_token=encrypt("fake-refresh-token", key=TEST_KEY),
        )
        session.add(connection)
        session.commit()
        return connection.id


def _create_policy(client) -> str:
    page = client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/policies",
        data={"title": "Security Policy", "status": "draft", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("?")[0].rsplit("/", 1)[-1]


# --- crypto ---


def test_encrypt_decrypt_roundtrip():
    ciphertext = encrypt("super-secret-refresh-token", key=TEST_KEY)
    assert ciphertext != "super-secret-refresh-token"
    assert decrypt(ciphertext, key=TEST_KEY) == "super-secret-refresh-token"


def test_encrypt_without_key_configured_raises():
    with pytest.raises(EncryptionNotConfiguredError):
        encrypt("x", key="")


def test_decrypt_with_wrong_key_raises():
    ciphertext = encrypt("x", key=TEST_KEY)
    with pytest.raises(DecryptionError):
        decrypt(ciphertext, key=Fernet.generate_key().decode())


# --- Drive ID/URL parsing (SSRF avoidance) ---


@pytest.mark.parametrize(
    "value",
    [
        "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view",
        "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit",
    ],
)
def test_parse_drive_file_id_accepts_valid_forms(value):
    assert parse_drive_file_id(value) == "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize(
    "value", ["", "short", "http://evil.example.com/../../etc/passwd", "!!!not-an-id!!!"]
)
def test_parse_drive_file_id_rejects_invalid_forms(value):
    with pytest.raises(GoogleDriveError):
        parse_drive_file_id(value)


# --- Connector connect/disconnect ---


def test_connector_page_shows_not_configured(logged_in_client):
    response = logged_in_client.get("/connectors/google-drive")
    assert response.status_code == 200
    assert b"Not configured" in response.content


def test_connect_requires_admin(logged_in_client, app):
    _enable_drive(app)
    response = logged_in_client.get("/connectors/google-drive/connect", follow_redirects=False)
    assert response.status_code == 403


def test_connect_disabled_returns_404_when_not_configured(admin_client):
    response = admin_client.get("/connectors/google-drive/connect")
    assert response.status_code == 404


def test_connect_redirects_to_google(admin_client, app):
    _enable_drive(app)
    response = admin_client.get("/connectors/google-drive/connect", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.com/")
    assert "access_type=offline" in response.headers["location"]


@patch("app.routers.google_drive.exchange_code_for_tokens")
def test_callback_stores_encrypted_token_and_audits(mock_exchange, admin_client, app):
    _enable_drive(app)
    admin_client.get("/connectors/google-drive/connect", follow_redirects=False)
    state = admin_client.cookies.get("google_drive_oauth_state")

    mock_exchange.return_value = {
        "refresh_token": "real-refresh-token",
        "scope": "https://www.googleapis.com/auth/drive.readonly",
    }
    response = admin_client.get(
        "/connectors/google-drive/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        connection = session.scalar(select(GoogleDriveConnection))
        assert connection is not None
        assert connection.encrypted_refresh_token != "real-refresh-token"
        assert decrypt(connection.encrypted_refresh_token, key=TEST_KEY) == "real-refresh-token"

        events = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "google_drive_connection")
        ).all()
    assert any(e.action == "connect" for e in events)


@patch("app.routers.google_drive.exchange_code_for_tokens")
def test_callback_rejects_missing_refresh_token(mock_exchange, admin_client, app):
    from app.google_drive import GoogleDriveError as _Err

    _enable_drive(app)
    admin_client.get("/connectors/google-drive/connect", follow_redirects=False)
    state = admin_client.cookies.get("google_drive_oauth_state")

    mock_exchange.side_effect = _Err("Google did not return a refresh token.")
    response = admin_client.get(
        "/connectors/google-drive/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        assert session.scalar(select(GoogleDriveConnection)) is None


def test_disconnect_requires_admin(logged_in_client, app, admin_user):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    page = logged_in_client.get("/connectors/google-drive")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/connectors/google-drive/disconnect", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 403


@patch("app.routers.google_drive.revoke_token")
def test_disconnect_erases_token_but_keeps_row(mock_revoke, admin_client, app, admin_user):
    _enable_drive(app)
    connection_id = _seed_active_connection(app, admin_user)

    page = admin_client.get("/connectors/google-drive")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/connectors/google-drive/disconnect", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        connection = session.get(GoogleDriveConnection, connection_id)
        assert connection.encrypted_refresh_token == ""
        assert connection.revoked_at is not None
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "google_drive_connection", AuditEvent.action == "disconnect"
            )
        ).all()
    assert len(events) == 1
    mock_revoke.assert_called_once()


# --- Policy Drive linking and capture ---


def _drive_metadata(mime_type="application/pdf", revision_id="rev-1") -> DriveFileMetadata:
    return DriveFileMetadata(
        file_id="1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        name="Security Policy",
        mime_type=mime_type,
        web_view_link="https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view",
        current_revision_id=revision_id,
    )


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.get_file_metadata")
def test_admin_can_link_policy_to_drive_file(mock_get_metadata, _mock_token, admin_client, app, admin_user):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    mock_get_metadata.return_value = _drive_metadata()

    policy_id = _create_policy(admin_client)
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/policies/{policy_id}/drive-link",
        data={"drive_url_or_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert policy.source_type == "drive"
        assert policy.drive_file_id == "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


def test_non_admin_cannot_link_policy_to_drive(logged_in_client):
    policy_id = _create_policy(logged_in_client)
    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/drive-link",
        data={"drive_url_or_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_link_without_active_connection_fails_cleanly(admin_client, app):
    _enable_drive(app)  # configured, but nobody has connected yet
    policy_id = _create_policy(admin_client)
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/policies/{policy_id}/drive-link",
        data={"drive_url_or_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.download_file_content", return_value=VALID_PDF)
@patch(
    "app.routers.policies.list_revisions",
    return_value=[{"id": "rev-1", "modifiedTime": "2026-07-01T00:00:00Z"}],
)
@patch("app.routers.policies.get_file_metadata")
def test_capture_creates_immutable_version_with_provenance(
    mock_get_metadata, _mock_revisions, _mock_download, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    mock_get_metadata.return_value = _drive_metadata()

    policy_id = _create_policy(admin_client)
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        f"/policies/{policy_id}/drive-link",
        data={"drive_url_or_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "csrf_token": csrf_token},
    )

    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/policies/{policy_id}/drive-capture",
        data={"change_note": "", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        version = policy.latest_version
        assert version.version_number == 1
        assert version.source_type == "drive"
        assert version.source_revision_id == "rev-1"
        assert version.sha256 is not None


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.download_file_content", return_value=FAKE_PDF)
@patch("app.routers.policies.list_revisions", return_value=[])
@patch("app.routers.policies.get_file_metadata")
def test_capture_rejects_spoofed_content_and_cleans_temp_files(
    mock_get_metadata, _mock_revisions, _mock_download, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    mock_get_metadata.return_value = _drive_metadata()

    policy_id = _create_policy(admin_client)
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        f"/policies/{policy_id}/drive-link",
        data={"drive_url_or_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "csrf_token": csrf_token},
    )

    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/policies/{policy_id}/drive-capture",
        data={"change_note": "", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert policy.versions == []  # no partial version created

    tmp_dir = os.path.join(app.state.settings.data_dir, "tmp")
    leftover = os.listdir(tmp_dir) if os.path.isdir(tmp_dir) else []
    assert leftover == []


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.get_file_metadata", side_effect=GoogleDriveError("Drive file not found."))
def test_capture_drive_failure_creates_no_partial_version(
    mock_get_metadata, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    connection_id = _seed_active_connection(app, admin_user)

    with app.state.session_factory() as session:
        policy = Policy(
            title="Linked Policy", source_type="drive", drive_file_id="1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        )
        session.add(policy)
        session.commit()
        policy_id = policy.id

    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/policies/{policy_id}/drive-capture",
        data={"change_note": "", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert policy.versions == []
        connection = session.get(GoogleDriveConnection, connection_id)
        assert connection.last_successful_sync_at is None


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.list_revisions", return_value=[])
@patch("app.routers.policies.get_file_metadata")
def test_second_capture_creates_new_version_and_does_not_mutate_first(
    mock_get_metadata, _mock_revisions, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    mock_get_metadata.return_value = _drive_metadata(revision_id="rev-1")

    policy_id = _create_policy(admin_client)
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        f"/policies/{policy_id}/drive-link",
        data={"drive_url_or_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "csrf_token": csrf_token},
    )

    with patch("app.routers.policies.download_file_content", return_value=VALID_PDF):
        page = admin_client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        admin_client.post(
            f"/policies/{policy_id}/drive-capture",
            data={"change_note": "", "csrf_token": csrf_token},
        )

    with app.state.session_factory() as session:
        v1_sha256 = session.get(Policy, policy_id).latest_version.sha256

    # Drive content changes; capture again — intentional: a fresh capture
    # always creates a new version, even if the content turned out to be
    # identical, since capturing is a deliberate compliance action.
    mock_get_metadata.return_value = _drive_metadata(revision_id="rev-2")
    with patch("app.routers.policies.download_file_content", return_value=VALID_PDF_V2):
        page = admin_client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        admin_client.post(
            f"/policies/{policy_id}/drive-capture",
            data={"change_note": "", "csrf_token": csrf_token},
        )

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        versions = sorted(policy.versions, key=lambda v: v.version_number)
        assert len(versions) == 2
        assert versions[0].sha256 == v1_sha256  # v1 untouched
        assert versions[1].sha256 != v1_sha256  # v2 reflects new content
        assert versions[1].source_revision_id == "rev-2"


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.download_file_content", return_value=VALID_PDF)
@patch("app.routers.policies.list_revisions", return_value=[])
def test_capture_of_google_doc_exports_to_pdf(
    _mock_revisions, _mock_download, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)

    policy_id = _create_policy(admin_client)
    with patch("app.routers.policies.get_file_metadata") as mock_get_metadata:
        mock_get_metadata.return_value = _drive_metadata(mime_type="application/vnd.google-apps.document")
        page = admin_client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        admin_client.post(
            f"/policies/{policy_id}/drive-link",
            data={"drive_url_or_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "csrf_token": csrf_token},
        )
        page = admin_client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        admin_client.post(
            f"/policies/{policy_id}/drive-capture", data={"change_note": "", "csrf_token": csrf_token}
        )

    with app.state.session_factory() as session:
        version = session.get(Policy, policy_id).latest_version
        assert version.media_type == "application/pdf"
        assert version.original_filename.endswith(".pdf")


def test_manual_upload_still_works_alongside_drive_fields(logged_in_client, app):
    policy_id = _create_policy(logged_in_client)
    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("policy.pdf", VALID_PDF, "application/pdf")}
    response = logged_in_client.post(
        f"/policies/{policy_id}/versions",
        data={"change_note": "manual", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        version = session.get(Policy, policy_id).latest_version
        assert version.source_type == "manual"
        assert version.source_file_id is None
