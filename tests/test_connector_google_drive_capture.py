"""Integration tests for app/connectors/google_drive_capture.py (issue
#28) — proves Google Drive operating as a real file.capture connector
through the generic platform's install/execution/ingestion boundary
(#24/#25), not the pre-existing, separate policy-document capture path
(tests/test_google_drive.py).
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import boto3
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws
from sqlalchemy import select

from app.connector_lifecycle import ConnectorLifecycleError, set_connector_instance_enabled
from app.connectors.adapters import google_drive_manifest
from app.connectors.google_drive_capture import (
    get_or_create_google_drive_instance,
    run_google_drive_capture,
)
from app.google_drive import DriveFileMetadata, GoogleDriveError
from app.models import (
    ConnectorInstance,
    EvidenceArtifact,
    EvidenceArtifactVersion,
    GoogleDriveConnection,
    User,
)
from app.security import hash_password

TEST_KEY = Fernet.generate_key().decode()
BUCKET = "minigrc-drive-connector-test"
VALID_PDF = b"%PDF-1.4\n%mock pdf content\n%%EOF"
VALID_PDF_V2 = b"%PDF-1.4\n%mock pdf content v2\n%%EOF"
FAKE_PDF = b"this is not a real pdf"


def _metadata(**overrides) -> DriveFileMetadata:
    defaults = dict(
        file_id="drive-file-1",
        name="Security Policy.pdf",
        mime_type="application/pdf",
        web_view_link="https://drive.google.com/file/d/drive-file-1/view",
        current_revision_id="rev-1",
    )
    defaults.update(overrides)
    return DriveFileMetadata(**defaults)


@pytest.fixture
def actor(app):
    with app.state.session_factory() as session:
        user = User(email="drive-admin@example.com", password_hash=hash_password("x"), role="admin")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def connection(app, actor):
    with app.state.session_factory() as session:
        conn = GoogleDriveConnection(
            connected_by_user_id=actor.id,
            granted_scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_refresh_token="irrelevant-for-these-tests",
        )
        session.add(conn)
        session.commit()
        session.refresh(conn)
        return conn


def _configure_s3(app):
    app.state.settings.s3_endpoint_url = "https://s3.amazonaws.com"
    app.state.settings.s3_bucket = BUCKET
    app.state.settings.s3_access_key_id = "fake-key"
    app.state.settings.s3_secret_access_key = "fake-secret"


def _make_artifact(session, title="Drive-sourced evidence") -> EvidenceArtifact:
    artifact = EvidenceArtifact(title=title)
    session.add(artifact)
    session.flush()
    return artifact


def _install_and_enable(session, actor):
    manifest = google_drive_manifest()
    instance = get_or_create_google_drive_instance(session, manifest, actor=actor, encryption_key=TEST_KEY)
    set_connector_instance_enabled(session, instance, True, actor=actor)
    return manifest, instance


# --- get_or_create_google_drive_instance ------------------------------------


def test_get_or_create_installs_exactly_once(app, actor):
    with app.state.session_factory() as session:
        manifest = google_drive_manifest()
        first = get_or_create_google_drive_instance(session, manifest, actor=actor, encryption_key=TEST_KEY)
        session.commit()

        second = get_or_create_google_drive_instance(session, manifest, actor=actor, encryption_key=TEST_KEY)
        session.commit()

        assert first.id == second.id
        instances = session.scalars(
            select(ConnectorInstance).where(ConnectorInstance.connector_id == "google_drive")
        ).all()
        assert len(instances) == 1
        assert instances[0].enabled is False


# --- run_google_drive_capture: happy paths -----------------------------------


@patch("app.connectors.google_drive_capture.list_revisions")
@patch("app.connectors.google_drive_capture.download_file_content", return_value=VALID_PDF)
@patch("app.connectors.google_drive_capture.get_file_metadata")
def test_capture_binary_file_ingests_as_evidence_with_provenance(
    mock_get_metadata, _mock_download, mock_list_revisions, app, actor, connection
):
    mock_get_metadata.return_value = _metadata()
    mock_list_revisions.return_value = [{"id": "rev-1", "modifiedTime": "2026-01-01T00:00:00Z"}]
    _configure_s3(app)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        with app.state.session_factory() as session:
            manifest, instance = _install_and_enable(session, actor)
            artifact = _make_artifact(session)
            session.commit()

            execution = run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="fake-access-token",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-1",
                actor_id=actor.id,
            )
            session.commit()

            assert execution.status == "success"
            version = session.scalar(
                select(EvidenceArtifactVersion).where(EvidenceArtifactVersion.artifact_id == artifact.id)
            )
            assert version is not None
            assert version.source_type == "google_drive"
            assert version.source_connection_id == connection.id
            assert version.source_object_id == "drive-file-1"
            assert version.source_revision_id == "rev-1"
            assert version.source_modified_at == datetime.datetime(2026, 1, 1)
            assert version.original_filename == "Security Policy.pdf"
            instance = session.get(ConnectorInstance, instance.id)
            assert instance.last_success_at is not None


# --- idempotency / versioning ------------------------------------------------


@patch("app.connectors.google_drive_capture.list_revisions", return_value=[])
@patch("app.connectors.google_drive_capture.download_file_content", return_value=VALID_PDF)
@patch("app.connectors.google_drive_capture.get_file_metadata")
def test_same_content_retry_is_idempotent_no_new_version(
    mock_get_metadata, _mock_download, _mock_revisions, app, actor, connection
):
    mock_get_metadata.return_value = _metadata()
    _configure_s3(app)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        with app.state.session_factory() as session:
            manifest, instance = _install_and_enable(session, actor)
            artifact = _make_artifact(session)
            session.commit()

            run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-1",
                actor_id=actor.id,
            )
            session.commit()
            run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-2",
                actor_id=actor.id,
            )
            session.commit()

            versions = session.scalars(
                select(EvidenceArtifactVersion).where(EvidenceArtifactVersion.artifact_id == artifact.id)
            ).all()
            assert len(versions) == 1  # unchanged content -> safe no-op, not a duplicate


@patch("app.connectors.google_drive_capture.list_revisions", return_value=[])
@patch("app.connectors.google_drive_capture.download_file_content", return_value=VALID_PDF)
@patch("app.connectors.google_drive_capture.get_file_metadata")
def test_repeated_idempotency_key_returns_existing_execution_without_recapturing(
    mock_get_metadata, mock_download, _mock_revisions, app, actor, connection
):
    mock_get_metadata.return_value = _metadata()
    _configure_s3(app)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        with app.state.session_factory() as session:
            manifest, instance = _install_and_enable(session, actor)
            artifact = _make_artifact(session)
            session.commit()

            first = run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="same-key",
                actor_id=actor.id,
            )
            session.commit()
            call_count_after_first = mock_download.call_count

            second = run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="same-key",
                actor_id=actor.id,
            )
            session.commit()

            assert second.id == first.id
            assert mock_download.call_count == call_count_after_first  # Drive never called again


@patch("app.connectors.google_drive_capture.list_revisions", return_value=[])
@patch("app.connectors.google_drive_capture.download_file_content")
@patch("app.connectors.google_drive_capture.get_file_metadata")
def test_changed_revision_creates_new_immutable_version(
    mock_get_metadata, mock_download, _mock_revisions, app, actor, connection
):
    _configure_s3(app)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        with app.state.session_factory() as session:
            manifest, instance = _install_and_enable(session, actor)
            artifact = _make_artifact(session)
            session.commit()

            mock_get_metadata.return_value = _metadata(current_revision_id="rev-1")
            mock_download.return_value = VALID_PDF
            run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-1",
                actor_id=actor.id,
            )
            session.commit()

            mock_get_metadata.return_value = _metadata(current_revision_id="rev-2")
            mock_download.return_value = VALID_PDF_V2
            run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-2",
                actor_id=actor.id,
            )
            session.commit()

            versions = session.scalars(
                select(EvidenceArtifactVersion)
                .where(EvidenceArtifactVersion.artifact_id == artifact.id)
                .order_by(EvidenceArtifactVersion.version_number)
            ).all()
            assert len(versions) == 2
            assert versions[0].source_revision_id == "rev-1"
            assert versions[1].source_revision_id == "rev-2"
            assert versions[0].sha256 != versions[1].sha256


# --- rejection / failure paths ------------------------------------------------


def test_disabled_instance_rejected_before_any_drive_call(app, actor, connection):
    with app.state.session_factory() as session:
        manifest = google_drive_manifest()
        instance = get_or_create_google_drive_instance(
            session, manifest, actor=actor, encryption_key=TEST_KEY
        )
        artifact = _make_artifact(session)
        session.commit()

        with (
            patch("app.connectors.google_drive_capture.get_file_metadata") as mock_get_metadata,
            pytest.raises(ConnectorLifecycleError, match="disabled"),
        ):
            run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-1",
                actor_id=actor.id,
            )
        mock_get_metadata.assert_not_called()


def test_malformed_drive_file_id_rejected_before_any_drive_call(app, actor, connection):
    """A caller-supplied value that isn't a recognized Drive ID/URL is
    rejected by the same SSRF-safe parse_drive_file_id guard the
    existing policy-Drive routes already apply — before any request to
    Drive's API is ever made."""
    with app.state.session_factory() as session:
        manifest, instance = _install_and_enable(session, actor)
        artifact = _make_artifact(session)
        session.commit()

        with patch("app.connectors.google_drive_capture.get_file_metadata") as mock_get_metadata:
            execution = run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="https://evil.example.com/not-a-drive-url",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-1",
                actor_id=actor.id,
            )
            session.commit()

        mock_get_metadata.assert_not_called()
        assert execution.status == "failure"


@patch(
    "app.connectors.google_drive_capture.get_file_metadata",
    side_effect=GoogleDriveError("Drive file not found."),
)
def test_drive_error_records_failure_execution_without_ingesting(_mock_get_metadata, app, actor, connection):
    with app.state.session_factory() as session:
        manifest, instance = _install_and_enable(session, actor)
        artifact = _make_artifact(session)
        session.commit()

        execution = run_google_drive_capture(
            session,
            instance,
            manifest,
            drive_file_id="drive-file-1",
            access_token="tok",
            connection=connection,
            artifact=artifact,
            settings=app.state.settings,
            idempotency_key="run-1",
            actor_id=actor.id,
        )
        session.commit()

        assert execution.status == "failure"
        assert "not found" in execution.error_summary
        assert session.scalar(select(EvidenceArtifactVersion)) is None
        instance = session.get(ConnectorInstance, instance.id)
        assert instance.last_success_at is None
        assert instance.last_failure_at is not None


@patch("app.connectors.google_drive_capture.list_revisions", return_value=[])
@patch("app.connectors.google_drive_capture.download_file_content", return_value=b"not a real drive file")
@patch("app.connectors.google_drive_capture.get_file_metadata")
def test_unsupported_file_type_rejected_before_any_upload(
    mock_get_metadata, _mock_download, _mock_revisions, app, actor, connection
):
    mock_get_metadata.return_value = _metadata(name="malware.exe", mime_type="application/octet-stream")

    with app.state.session_factory() as session:
        manifest, instance = _install_and_enable(session, actor)
        artifact = _make_artifact(session)
        session.commit()

        execution = run_google_drive_capture(
            session,
            instance,
            manifest,
            drive_file_id="drive-file-1",
            access_token="tok",
            connection=connection,
            artifact=artifact,
            settings=app.state.settings,
            idempotency_key="run-1",
            actor_id=actor.id,
        )
        session.commit()

        assert execution.status == "failure"
        assert "Unsupported Drive file type" in execution.error_summary
        assert session.scalar(select(EvidenceArtifactVersion)) is None


@patch("app.connectors.google_drive_capture.list_revisions", return_value=[])
@patch("app.connectors.google_drive_capture.download_file_content", return_value=FAKE_PDF)
@patch("app.connectors.google_drive_capture.get_file_metadata")
def test_content_that_does_not_match_declared_extension_rejected(
    mock_get_metadata, _mock_download, _mock_revisions, app, actor, connection
):
    mock_get_metadata.return_value = _metadata()  # claims application/pdf, but bytes aren't a real PDF

    with app.state.session_factory() as session:
        manifest, instance = _install_and_enable(session, actor)
        artifact = _make_artifact(session)
        session.commit()

        execution = run_google_drive_capture(
            session,
            instance,
            manifest,
            drive_file_id="drive-file-1",
            access_token="tok",
            connection=connection,
            artifact=artifact,
            settings=app.state.settings,
            idempotency_key="run-1",
            actor_id=actor.id,
        )
        session.commit()

        assert execution.status == "failure"
        assert "does not look like" in execution.error_summary
        assert session.scalar(select(EvidenceArtifactVersion)) is None


# --- historical immutability --------------------------------------------------


@patch("app.connectors.google_drive_capture.list_revisions", return_value=[])
@patch("app.connectors.google_drive_capture.download_file_content", return_value=VALID_PDF)
@patch("app.connectors.google_drive_capture.get_file_metadata")
def test_removing_connector_instance_leaves_captured_evidence_intact(
    mock_get_metadata, _mock_download, _mock_revisions, app, actor, connection
):
    from app.connector_lifecycle import remove_connector_instance

    mock_get_metadata.return_value = _metadata()
    _configure_s3(app)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        with app.state.session_factory() as session:
            manifest, instance = _install_and_enable(session, actor)
            artifact = _make_artifact(session)
            session.commit()

            run_google_drive_capture(
                session,
                instance,
                manifest,
                drive_file_id="drive-file-1",
                access_token="tok",
                connection=connection,
                artifact=artifact,
                settings=app.state.settings,
                idempotency_key="run-1",
                actor_id=actor.id,
            )
            session.commit()
            version_id = session.scalar(select(EvidenceArtifactVersion)).id

            remove_connector_instance(session, instance, actor=actor)
            session.commit()

            assert session.get(ConnectorInstance, instance.id) is None
            surviving_version = session.get(EvidenceArtifactVersion, version_id)
            assert surviving_version is not None
            assert surviving_version.source_object_id == "drive-file-1"
