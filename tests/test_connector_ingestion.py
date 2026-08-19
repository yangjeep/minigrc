"""Integration tests for app/connectors/ingestion.py (issue #24) —
proves the connector contract fits three real existing shapes
(AWS configuration.snapshot, Google Workspace identity.population,
Google Drive file.capture) without changing any of their provider-
calling logic.
"""

from __future__ import annotations

import datetime
import os

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from app.aws_connector import AwsCheckResult
from app.connectors.adapters import (
    aws_check_result_to_connector_result,
    aws_manifest,
    directory_users_to_connector_result,
    google_drive_manifest,
    workspace_directory_manifest,
)
from app.connectors.ingestion import (
    ingest_configuration_snapshot,
    ingest_file_capture,
    ingest_identity_population,
)
from app.connectors.manifest import ConnectorContractError
from app.connectors.result import ConnectorProvenance, ConnectorResult
from app.google_workspace_directory import DirectoryUser
from app.models import EvidenceArtifact, EvidenceSnapshot, Person
from app.object_storage import capture_raw_bytes

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
BUCKET = "minigrc-connector-test"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


# --- configuration.snapshot (AWS) -----------------------------------


def test_aws_check_result_ingests_as_evidence_snapshot(app):
    check = AwsCheckResult(
        check_key="cloudtrail_posture",
        status="pass",
        title="CloudTrail enabled",
        summary="Multi-region trail found",
        normalized_payload={"trail_count": 1, "multi_region": True},
    )
    manifest = aws_manifest()
    result = aws_check_result_to_connector_result(check, connection_id="conn-1", collected_at=NOW)

    with app.state.session_factory() as session:
        snapshot = ingest_configuration_snapshot(session, result, manifest)
        session.commit()

        assert snapshot.source_type == "aws_cloudtrail_iam"
        assert snapshot.source_connection_id == "conn-1"
        assert snapshot.check_key == "cloudtrail_posture"
        assert snapshot.status == "pass"
        assert snapshot.collected_at == NOW

        row = session.scalar(
            select(EvidenceSnapshot).where(EvidenceSnapshot.check_key == "cloudtrail_posture")
        )
        assert row is not None
        assert '"trail_count": 1' in row.normalized_payload_json


def test_aws_result_for_undeclared_capability_rejected(app):
    manifest = aws_manifest()
    bad_result = ConnectorResult(
        capability="identity.population",  # aws_manifest only declares configuration.snapshot
        check_key="k",
        status="pass",
        title="t",
        summary="s",
        provenance=ConnectorProvenance(source_connection_id="conn-1", collected_at=NOW),
    )
    with app.state.session_factory() as session:
        with pytest.raises(ConnectorContractError, match="undeclared"):
            ingest_configuration_snapshot(session, bad_result, manifest)


# --- identity.population (Google Workspace Directory) ----------------


def test_directory_users_ingest_as_person_upserts(app):
    users = [
        DirectoryUser(
            external_id="ext-1",
            primary_email="alice@example.com",
            display_name="Alice",
            suspended=False,
            archived=False,
        ),
        DirectoryUser(
            external_id="ext-2",
            primary_email="bob@example.com",
            display_name="Bob",
            suspended=True,
            archived=False,
        ),
    ]
    manifest = workspace_directory_manifest()
    result = directory_users_to_connector_result(users, connection_id="conn-2", collected_at=NOW)

    with app.state.session_factory() as session:
        summary = ingest_identity_population(session, result, manifest)
        session.commit()

        assert summary == {"created": 2, "updated": 0, "total": 2}

        alice = session.scalar(select(Person).where(Person.email == "alice@example.com"))
        assert alice.employment_status == "active"
        assert alice.source == "google_workspace"
        assert alice.external_id == "ext-1"

        bob = session.scalar(select(Person).where(Person.email == "bob@example.com"))
        assert bob.employment_status == "suspended"


def test_directory_population_never_deletes_missing_person(app):
    manifest = workspace_directory_manifest()
    with app.state.session_factory() as session:
        session.add(Person(email="carol@example.com", display_name="Carol", source="manual"))
        session.commit()

    # A sync that no longer includes Carol must not touch/remove her.
    users = [
        DirectoryUser(
            external_id="ext-1",
            primary_email="alice@example.com",
            display_name="Alice",
            suspended=False,
            archived=False,
        )
    ]
    result = directory_users_to_connector_result(users, connection_id="conn-2", collected_at=NOW)
    with app.state.session_factory() as session:
        ingest_identity_population(session, result, manifest)
        session.commit()

        carol = session.scalar(select(Person).where(Person.email == "carol@example.com"))
        assert carol is not None
        assert carol.source == "manual"


def test_directory_population_updates_existing_person(app):
    manifest = workspace_directory_manifest()
    with app.state.session_factory() as session:
        session.add(
            Person(
                email="alice@example.com",
                display_name="Old Name",
                source="manual",
                employment_status="unknown",
            )
        )
        session.commit()

    users = [
        DirectoryUser(
            external_id="ext-1",
            primary_email="alice@example.com",
            display_name="Alice New",
            suspended=False,
            archived=False,
        )
    ]
    result = directory_users_to_connector_result(users, connection_id="conn-2", collected_at=NOW)
    with app.state.session_factory() as session:
        summary = ingest_identity_population(session, result, manifest)
        session.commit()

        assert summary == {"created": 0, "updated": 1, "total": 1}
        alice = session.scalar(select(Person).where(Person.email == "alice@example.com"))
        assert alice.display_name == "Alice New"
        assert alice.employment_status == "active"
        assert alice.source == "google_workspace"


# --- file.capture (Google Drive) -------------------------------------


def _make_artifact(session, title="Drive file"):
    artifact = EvidenceArtifact(title=title)
    session.add(artifact)
    session.flush()
    return artifact


def test_file_capture_ingestion_forwards_provenance(app, s3):
    manifest = google_drive_manifest()
    provenance = ConnectorProvenance(
        source_connection_id="drive-conn-1",
        collected_at=NOW,
        source_object_id="drive-file-id-1",
        source_revision_id="rev-3",
    )

    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        captured = capture_raw_bytes(b"drive file content", max_bytes=1024 * 1024)
        version, created = ingest_file_capture(
            session,
            artifact,
            provenance=provenance,
            manifest=manifest,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured.tmp_path,
            sha256=captured.sha256,
            byte_size=captured.byte_size,
            media_type="text/plain",
            original_filename="report.txt",
            extension="txt",
        )
        session.commit()

        assert created is True
        assert version.source_type == "google_drive"
        assert version.source_connection_id == "drive-conn-1"
        assert version.source_object_id == "drive-file-id-1"
        assert version.source_revision_id == "rev-3"
        assert not os.path.exists(captured.tmp_path)


def test_file_capture_rejects_manifest_without_capability(app, s3):
    manifest = aws_manifest()  # declares configuration.snapshot, not file.capture
    provenance = ConnectorProvenance(source_connection_id="conn-1", collected_at=NOW)

    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        captured = capture_raw_bytes(b"x", max_bytes=1024)
        with pytest.raises(ConnectorContractError, match="does not declare"):
            ingest_file_capture(
                session,
                artifact,
                provenance=provenance,
                manifest=manifest,
                client=s3,
                bucket=BUCKET,
                tmp_path=captured.tmp_path,
                sha256=captured.sha256,
                byte_size=captured.byte_size,
                media_type="text/plain",
                original_filename="x.txt",
                extension="txt",
            )
        os.remove(captured.tmp_path)
