"""Unit tests for app/connectors/adapters.py (issue #24) — the adapter
functions round-trip real existing result shapes without loss, and the
three reference manifests validate cleanly."""

from __future__ import annotations

import datetime

from app.aws_connector import AwsCheckResult
from app.connectors.adapters import (
    aws_check_result_to_connector_result,
    aws_manifest,
    directory_users_to_connector_result,
    google_drive_manifest,
    workspace_directory_manifest,
)
from app.connectors.manifest import check_compatibility, validate_manifest
from app.connectors.result import validate_result
from app.google_workspace_directory import DirectoryUser

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def test_aws_manifest_is_valid_and_compatible():
    manifest = aws_manifest()
    validate_manifest(manifest)
    check_compatibility(manifest)


def test_google_drive_manifest_is_valid_and_compatible():
    manifest = google_drive_manifest()
    validate_manifest(manifest)
    check_compatibility(manifest)


def test_workspace_directory_manifest_is_valid_and_compatible():
    manifest = workspace_directory_manifest()
    validate_manifest(manifest)
    check_compatibility(manifest)


def test_aws_check_result_round_trips():
    check = AwsCheckResult(
        check_key="iam_posture",
        status="warning",
        title="Some IAM users lack MFA",
        summary="2 of 5 users have no MFA device",
        normalized_payload={"users_without_mfa": 2, "total_users": 5},
    )
    result = aws_check_result_to_connector_result(check, connection_id="conn-1", collected_at=NOW)

    assert result.capability == "configuration.snapshot"
    assert result.check_key == check.check_key
    assert result.status == check.status
    assert result.title == check.title
    assert result.summary == check.summary
    assert result.normalized_payload == check.normalized_payload
    assert result.provenance.source_connection_id == "conn-1"
    assert result.provenance.collected_at == NOW

    validate_result(result, aws_manifest())


def test_directory_users_round_trip():
    users = [
        DirectoryUser(
            external_id="ext-1",
            primary_email="alice@example.com",
            display_name="Alice",
            suspended=False,
            archived=False,
        ),
    ]
    result = directory_users_to_connector_result(users, connection_id="conn-2", collected_at=NOW)

    assert result.capability == "identity.population"
    assert result.normalized_payload["users"][0]["email"] == "alice@example.com"
    assert result.normalized_payload["users"][0]["display_name"] == "Alice"
    assert result.normalized_payload["users"][0]["suspended"] is False

    validate_result(result, workspace_directory_manifest())
