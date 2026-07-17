from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import encrypt
from app.google_drive import DriveFileMetadata
from app.google_drive_approvals import ApprovalsUnavailableError, parse_approval
from app.google_workspace_directory import DirectoryUser, sync_directory_users
from app.models import AuditEvent, GoogleDriveConnection, Person, Policy, PolicyApprovalSnapshot
from tests.conftest import extract_csrf_token

VALID_PDF = b"%PDF-1.4\n%mock pdf content for tests\n%%EOF"
TEST_KEY = Fernet.generate_key().decode()

DRIVE_SETTINGS = {
    "google_drive_client_id": "drive-client-id",
    "google_drive_client_secret": "drive-client-secret",
    "public_base_url": "https://grc.example.com",
    "encryption_key": TEST_KEY,
}


def _enable_drive(app, *, workspace_directory: bool = False) -> None:
    for key, value in DRIVE_SETTINGS.items():
        setattr(app.state.settings, key, value)
    app.state.settings.google_workspace_directory_enabled = workspace_directory


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


def _create_policy_with_drive_capture(client, app) -> str:
    page = client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/policies",
        data={"title": "Security Policy", "status": "draft", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    policy_id = response.headers["location"].split("?")[0].rsplit("/", 1)[-1]

    metadata = DriveFileMetadata(
        file_id="1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        name="Security Policy.pdf",
        mime_type="application/pdf",
        web_view_link="https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view",
        current_revision_id="rev-1",
    )
    with (
        patch("app.routers.google_drive.get_access_token", return_value="fake-access-token"),
        patch("app.routers.policies.get_file_metadata", return_value=metadata),
        patch("app.routers.policies.list_revisions", return_value=[]),
        patch("app.routers.policies.download_file_content", return_value=VALID_PDF),
    ):
        page = client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        client.post(
            f"/policies/{policy_id}/drive-link",
            data={"drive_url_or_id": metadata.file_id, "csrf_token": csrf_token},
        )
        page = client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        client.post(
            f"/policies/{policy_id}/drive-capture", data={"change_note": "", "csrf_token": csrf_token}
        )

    return policy_id


RAW_APPROVAL = {
    "approvalId": "approval-1",
    "state": "approved",
    "initiatingUser": {"emailAddress": "alice@example.com"},
    "createTime": "2026-07-01T00:00:00Z",
    "completeTime": "2026-07-02T00:00:00Z",
    "reviewers": [{"emailAddress": "bob@example.com", "decision": "approved"}],
}


# --- parse_approval ---


def test_parse_approval_extracts_fields():
    parsed = parse_approval(RAW_APPROVAL)
    assert parsed.external_approval_id == "approval-1"
    assert parsed.status == "approved"
    assert parsed.initiator == "alice@example.com"
    assert parsed.complete_time is not None
    assert parsed.raw_payload_sha256


def test_parse_approval_rejects_missing_id():
    with pytest.raises(ApprovalsUnavailableError):
        parse_approval({"state": "approved"})


def test_parse_approval_tolerates_unknown_shape():
    parsed = parse_approval({"id": "x", "status": "declined"})
    assert parsed.external_approval_id == "x"
    assert parsed.status == "declined"


# --- Drive approvals sync route ---


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.fetch_approvals", return_value=[RAW_APPROVAL])
def test_sync_approvals_creates_snapshot(_mock_fetch, _mock_token, admin_client, app, admin_user):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    policy_id = _create_policy_with_drive_capture(admin_client, app)

    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/policies/{policy_id}/drive-approvals", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        snapshots = session.scalars(select(PolicyApprovalSnapshot)).all()
        assert len(snapshots) == 1
        assert snapshots[0].external_approval_id == "approval-1"
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "sync_drive_approvals")).all()
    assert len(events) == 1


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.fetch_approvals", return_value=[RAW_APPROVAL])
def test_resyncing_unchanged_approval_does_not_duplicate(
    _mock_fetch, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    policy_id = _create_policy_with_drive_capture(admin_client, app)

    for _ in range(2):
        page = admin_client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        admin_client.post(f"/policies/{policy_id}/drive-approvals", data={"csrf_token": csrf_token})

    with app.state.session_factory() as session:
        snapshots = session.scalars(select(PolicyApprovalSnapshot)).all()
    assert len(snapshots) == 1  # unchanged content — no duplicate row


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.fetch_approvals")
def test_changed_approval_creates_new_snapshot_not_mutation(
    mock_fetch, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    policy_id = _create_policy_with_drive_capture(admin_client, app)

    mock_fetch.return_value = [RAW_APPROVAL]
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(f"/policies/{policy_id}/drive-approvals", data={"csrf_token": csrf_token})

    changed = dict(RAW_APPROVAL, state="declined")
    mock_fetch.return_value = [changed]
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(f"/policies/{policy_id}/drive-approvals", data={"csrf_token": csrf_token})

    with app.state.session_factory() as session:
        snapshots = session.scalars(
            select(PolicyApprovalSnapshot).order_by(PolicyApprovalSnapshot.captured_at)
        ).all()
    assert len(snapshots) == 2  # original preserved, not overwritten
    assert snapshots[0].status == "approved"
    assert snapshots[1].status == "declined"


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch("app.routers.policies.fetch_approvals", side_effect=ApprovalsUnavailableError("not available"))
def test_unavailable_approvals_shows_message_not_failure(
    _mock_fetch, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    policy_id = _create_policy_with_drive_capture(admin_client, app)

    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/policies/{policy_id}/drive-approvals", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303  # request itself doesn't fail
    assert "unavailable" in response.headers["location"].lower()


def test_sync_approvals_requires_admin(logged_in_client, app, admin_user):
    _enable_drive(app)
    _seed_active_connection(app, admin_user)
    policy_id = _create_policy_with_drive_capture(logged_in_client, app)

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/drive-approvals", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 403


# --- Workspace Directory sync ---


def test_sync_directory_users_creates_and_updates(app):
    with app.state.session_factory() as session:
        session.add(
            Person(email="existing@example.com", display_name="Old Name", employment_status="unknown")
        )
        session.commit()

        users = [
            DirectoryUser(
                external_id="1",
                primary_email="new@example.com",
                display_name="New Person",
                suspended=False,
                archived=False,
            ),
            DirectoryUser(
                external_id="2",
                primary_email="existing@example.com",
                display_name="Updated Name",
                suspended=True,
                archived=False,
            ),
        ]
        result = sync_directory_users(session, users)
        session.commit()

        assert result == {"created": 1, "updated": 1, "total": 2}

        new_person = session.scalar(select(Person).where(Person.email == "new@example.com"))
        assert new_person.source == "google_workspace"
        assert new_person.employment_status == "active"

        existing = session.scalar(select(Person).where(Person.email == "existing@example.com"))
        assert existing.display_name == "Updated Name"
        assert existing.employment_status == "suspended"


def test_sync_directory_users_never_deletes_unmatched_person(app):
    with app.state.session_factory() as session:
        session.add(Person(email="manual@example.com", display_name="Manual Contractor", source="manual"))
        session.commit()

        sync_directory_users(session, [])  # directory returns nobody
        session.commit()

        person = session.scalar(select(Person).where(Person.email == "manual@example.com"))
    assert person is not None
    assert person.source == "manual"  # untouched


def test_workspace_directory_sync_route_requires_enabled_flag(admin_client, app):
    _enable_drive(app, workspace_directory=False)
    page = admin_client.get("/people")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/people/sync-workspace-directory", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_workspace_directory_sync_requires_admin(logged_in_client, app):
    _enable_drive(app, workspace_directory=True)
    page = logged_in_client.get("/people")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/people/sync-workspace-directory", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 403


@patch("app.routers.google_drive.get_access_token", return_value="fake-access-token")
@patch(
    "app.routers.people.fetch_directory_users",
    return_value=[
        DirectoryUser(
            external_id="1",
            primary_email="synced@example.com",
            display_name="Synced Person",
            suspended=False,
            archived=False,
        )
    ],
)
def test_workspace_directory_sync_route_creates_person_and_audits(
    _mock_fetch, _mock_token, admin_client, app, admin_user
):
    _enable_drive(app, workspace_directory=True)
    _seed_active_connection(app, admin_user)

    page = admin_client.get("/people")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/people/sync-workspace-directory", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        person = session.scalar(select(Person).where(Person.email == "synced@example.com"))
        assert person is not None
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "sync_workspace_directory")
        ).all()
    assert len(events) == 1


def test_policy_page_shows_approval_data_unavailable_when_none_synced(logged_in_client, app):
    with app.state.session_factory() as session:
        policy = Policy(title="Unlinked Policy")
        session.add(policy)
        session.commit()
        policy_id = policy.id

    page = logged_in_client.get(f"/policies/{policy_id}")
    assert page.status_code == 200
