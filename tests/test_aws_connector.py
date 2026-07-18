from __future__ import annotations

import datetime
from unittest.mock import ANY, MagicMock, patch

import boto3
import pytest
from botocore.stub import Stubber
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.aws_connector import (
    MAX_NORMALIZED_PAYLOAD_CHARS,
    AwsConnectionError,
    build_evidence_snapshot,
    build_session,
    check_cloudtrail,
    check_iam,
)
from app.aws_connector import test_connection as aws_test_connection
from app.models import AuditEvent, AwsConnection, EvidenceSnapshot
from tests.conftest import extract_csrf_token

TEST_KEY = Fernet.generate_key().decode()


class FakeSession:
    """Deterministic stand-in for boto3.Session — returns a pre-stubbed
    client per service name, never makes a real AWS call."""

    def __init__(self, clients: dict):
        self._clients = clients

    def client(self, service_name, config=None):
        return self._clients[service_name]


def _stubbed_client(service_name: str) -> tuple:
    client = boto3.client(
        service_name, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    return client, Stubber(client)


# --- test_connection ---


def test_connection_success():
    client, stubber = _stubbed_client("sts")
    stubber.add_response(
        "get_caller_identity",
        {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test", "UserId": "AID1"},
    )
    stubber.activate()
    result = aws_test_connection(FakeSession({"sts": client}))
    assert result.status == "pass"
    assert "123456789012" in result.summary
    stubber.deactivate()
    stubber.assert_no_pending_responses()


def test_connection_wrong_account_rejected():
    client, stubber = _stubbed_client("sts")
    stubber.add_response(
        "get_caller_identity",
        {"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/test", "UserId": "AID1"},
    )
    stubber.activate()
    with pytest.raises(AwsConnectionError):
        aws_test_connection(FakeSession({"sts": client}), expected_account_id="123456789012")
    stubber.deactivate()


def test_connection_invalid_account_id_error():
    client, stubber = _stubbed_client("sts")
    stubber.add_client_error("get_caller_identity", service_error_code="InvalidClientTokenId")
    stubber.activate()
    with pytest.raises(AwsConnectionError):
        aws_test_connection(FakeSession({"sts": client}))
    stubber.deactivate()


# --- check_cloudtrail ---


def test_cloudtrail_no_trails_fails():
    client, stubber = _stubbed_client("cloudtrail")
    stubber.add_response("describe_trails", {"trailList": []})
    stubber.activate()
    result = check_cloudtrail(FakeSession({"cloudtrail": client}))
    assert result.status == "fail"
    stubber.deactivate()


def test_cloudtrail_stopped_logging_fails():
    client, stubber = _stubbed_client("cloudtrail")
    trail = {
        "Name": "main",
        "TrailARN": "arn:aws:cloudtrail:us-east-1:123456789012:trail/main",
        "IsMultiRegionTrail": True,
        "IncludeGlobalServiceEvents": True,
        "LogFileValidationEnabled": True,
        "S3BucketName": "bucket",
    }
    stubber.add_response("describe_trails", {"trailList": [trail]})
    stubber.add_response(
        "get_trail_status", {"IsLogging": False}, expected_params={"Name": trail["TrailARN"]}
    )
    stubber.activate()
    result = check_cloudtrail(FakeSession({"cloudtrail": client}))
    assert result.status == "fail"
    stubber.deactivate()


def test_cloudtrail_logging_fully_configured_passes():
    client, stubber = _stubbed_client("cloudtrail")
    trail = {
        "Name": "main",
        "TrailARN": "arn:aws:cloudtrail:us-east-1:123456789012:trail/main",
        "IsMultiRegionTrail": True,
        "IncludeGlobalServiceEvents": True,
        "LogFileValidationEnabled": True,
        "S3BucketName": "bucket",
    }
    stubber.add_response("describe_trails", {"trailList": [trail]})
    stubber.add_response("get_trail_status", {"IsLogging": True}, expected_params={"Name": trail["TrailARN"]})
    stubber.activate()
    result = check_cloudtrail(FakeSession({"cloudtrail": client}))
    assert result.status == "pass"
    stubber.deactivate()


def test_cloudtrail_logging_but_single_region_warns():
    client, stubber = _stubbed_client("cloudtrail")
    trail = {
        "Name": "main",
        "TrailARN": "arn:aws:cloudtrail:us-east-1:123456789012:trail/main",
        "IsMultiRegionTrail": False,
        "IncludeGlobalServiceEvents": True,
        "LogFileValidationEnabled": True,
        "S3BucketName": "bucket",
    }
    stubber.add_response("describe_trails", {"trailList": [trail]})
    stubber.add_response("get_trail_status", {"IsLogging": True}, expected_params={"Name": trail["TrailARN"]})
    stubber.activate()
    result = check_cloudtrail(FakeSession({"cloudtrail": client}))
    assert result.status == "warning"
    stubber.deactivate()


def test_cloudtrail_api_failure_is_unknown_not_fail():
    client, stubber = _stubbed_client("cloudtrail")
    stubber.add_client_error("describe_trails", service_error_code="AccessDenied")
    stubber.activate()
    result = check_cloudtrail(FakeSession({"cloudtrail": client}))
    assert result.status == "unknown"  # insufficient permissions != failed control
    stubber.deactivate()


# --- check_iam ---


def test_iam_root_mfa_and_keys_fail():
    client, stubber = _stubbed_client("iam")
    stubber.add_response(
        "get_account_summary",
        {"SummaryMap": {"AccountMFAEnabled": 0, "AccountAccessKeysPresent": 1}},
    )
    stubber.add_client_error("get_account_password_policy", service_error_code="NoSuchEntity")
    stubber.add_response("list_users", {"Users": []})
    stubber.activate()
    result = check_iam(FakeSession({"iam": client}))
    assert result.status == "fail"
    assert result.normalized_payload["root_mfa_enabled"] is False
    assert result.normalized_payload["root_access_keys_present"] is True
    stubber.deactivate()


def test_iam_missing_password_policy_warns():
    client, stubber = _stubbed_client("iam")
    stubber.add_response(
        "get_account_summary",
        {"SummaryMap": {"AccountMFAEnabled": 1, "AccountAccessKeysPresent": 0}},
    )
    stubber.add_client_error("get_account_password_policy", service_error_code="NoSuchEntity")
    stubber.add_response("list_users", {"Users": []})
    stubber.activate()
    result = check_iam(FakeSession({"iam": client}))
    assert result.status == "warning"
    assert result.normalized_payload["password_policy_present"] is False
    stubber.deactivate()


def test_iam_user_without_mfa_warns():
    client, stubber = _stubbed_client("iam")
    stubber.add_response(
        "get_account_summary", {"SummaryMap": {"AccountMFAEnabled": 1, "AccountAccessKeysPresent": 0}}
    )
    stubber.add_response("get_account_password_policy", {"PasswordPolicy": {"MinimumPasswordLength": 14}})
    stubber.add_response(
        "list_users",
        {
            "Users": [
                {
                    "UserName": "alice",
                    "UserId": "AIDAEXAMPLE0000001",
                    "Arn": "arn:aws:iam::123456789012:user/alice",
                    "Path": "/",
                    "CreateDate": datetime.datetime.now(datetime.UTC),
                }
            ]
        },
    )
    stubber.add_response("list_mfa_devices", {"MFADevices": []}, expected_params={"UserName": "alice"})
    stubber.add_response("list_access_keys", {"AccessKeyMetadata": []}, expected_params={"UserName": "alice"})
    stubber.activate()
    result = check_iam(FakeSession({"iam": client}))
    assert result.status == "warning"
    assert result.normalized_payload["users"][0]["has_mfa"] is False
    stubber.deactivate()


def test_iam_old_access_key_warns():
    client, stubber = _stubbed_client("iam")
    old_date = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=200)
    stubber.add_response(
        "get_account_summary", {"SummaryMap": {"AccountMFAEnabled": 1, "AccountAccessKeysPresent": 0}}
    )
    stubber.add_response("get_account_password_policy", {"PasswordPolicy": {"MinimumPasswordLength": 14}})
    stubber.add_response(
        "list_users",
        {
            "Users": [
                {
                    "UserName": "bob",
                    "UserId": "AIDAEXAMPLE0000002",
                    "Arn": "arn:aws:iam::123456789012:user/bob",
                    "Path": "/",
                    "CreateDate": datetime.datetime.now(datetime.UTC),
                }
            ]
        },
    )
    stubber.add_response(
        "list_mfa_devices",
        {
            "MFADevices": [
                {
                    "UserName": "bob",
                    "SerialNumber": "arn:aws:iam::123456789012:mfa/bob",
                    "EnableDate": old_date,
                }
            ]
        },
        expected_params={"UserName": "bob"},
    )
    stubber.add_response(
        "list_access_keys",
        {
            "AccessKeyMetadata": [
                {
                    "UserName": "bob",
                    "AccessKeyId": "AKIA1EXAMPLE1234567",
                    "Status": "Active",
                    "CreateDate": old_date,
                }
            ]
        },
        expected_params={"UserName": "bob"},
    )
    stubber.activate()
    result = check_iam(FakeSession({"iam": client}))
    assert result.status == "warning"
    assert result.normalized_payload["users"][0]["oldest_active_key_age_days"] > 90
    stubber.deactivate()


def test_iam_fully_healthy_passes():
    client, stubber = _stubbed_client("iam")
    stubber.add_response(
        "get_account_summary", {"SummaryMap": {"AccountMFAEnabled": 1, "AccountAccessKeysPresent": 0}}
    )
    stubber.add_response("get_account_password_policy", {"PasswordPolicy": {"MinimumPasswordLength": 14}})
    stubber.add_response("list_users", {"Users": []})
    stubber.activate()
    result = check_iam(FakeSession({"iam": client}))
    assert result.status == "pass"
    stubber.deactivate()


def test_iam_account_summary_failure_is_unknown():
    client, stubber = _stubbed_client("iam")
    stubber.add_client_error("get_account_summary", service_error_code="AccessDenied")
    stubber.activate()
    result = check_iam(FakeSession({"iam": client}))
    assert result.status == "unknown"
    stubber.deactivate()


def test_iam_evaluates_every_user_without_a_payload_count_cap():
    client = MagicMock()
    client.get_account_summary.return_value = {
        "SummaryMap": {"AccountMFAEnabled": 1, "AccountAccessKeysPresent": 0}
    }
    client.get_account_password_policy.return_value = {"PasswordPolicy": {"MinimumPasswordLength": 14}}
    users = [
        {
            "UserName": f"user-{index:03d}",
            "CreateDate": datetime.datetime.now(datetime.UTC),
        }
        for index in range(205)
    ]
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Users": users}]
    client.get_paginator.return_value = paginator
    client.list_mfa_devices.side_effect = lambda *, UserName: {
        "MFADevices": [{}] if UserName != "user-204" else []
    }
    client.list_access_keys.return_value = {"AccessKeyMetadata": []}

    result = check_iam(FakeSession({"iam": client}))

    assert result.normalized_payload["iam_user_count"] == 205
    assert len(result.normalized_payload["users"]) == 205
    assert client.list_mfa_devices.call_count == 205
    assert "user 'user-204' has no MFA device" in result.summary


# --- build_session / AssumeRole ---


def test_build_session_without_role_arn_uses_ambient_credentials():
    session = build_session(role_arn=None, external_id=None, region="us-east-1")
    assert isinstance(session, boto3.Session)


@patch("app.aws_connector.boto3.Session")
def test_build_session_assumes_role(mock_session_cls):
    sts_client = boto3.client(
        "sts", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    stubber = Stubber(sts_client)
    stubber.add_response(
        "assume_role",
        {
            "Credentials": {
                "AccessKeyId": "AKIAFAKEEXAMPLE12345",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.datetime.now(datetime.UTC),
            }
        },
        expected_params={
            "RoleArn": "arn:aws:iam::123456789012:role/Test",
            "RoleSessionName": ANY,
            "ExternalId": "ext-id",
        },
    )
    stubber.activate()

    base_session_mock = MagicMock()
    base_session_mock.client.return_value = sts_client
    mock_session_cls.side_effect = [base_session_mock, "final-session-sentinel"]

    result = build_session(
        role_arn="arn:aws:iam::123456789012:role/Test", external_id="ext-id", region="us-east-1"
    )
    assert result == "final-session-sentinel"
    stubber.deactivate()
    stubber.assert_no_pending_responses()


@patch("app.aws_connector.boto3.Session")
def test_build_session_assume_role_failure_raises(mock_session_cls):
    sts_client = boto3.client(
        "sts", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    stubber = Stubber(sts_client)
    stubber.add_client_error("assume_role", service_error_code="AccessDenied")
    stubber.activate()

    base_session_mock = MagicMock()
    base_session_mock.client.return_value = sts_client
    mock_session_cls.return_value = base_session_mock

    with pytest.raises(AwsConnectionError):
        build_session(role_arn="arn:aws:iam::123456789012:role/Test", external_id=None, region="us-east-1")
    stubber.deactivate()


def test_build_evidence_snapshot_bounds_and_hashes():
    from app.aws_connector import AwsCheckResult

    result = AwsCheckResult("cloudtrail_posture", "pass", "CloudTrail posture", "ok", {"a": 1})
    snapshot = build_evidence_snapshot(result, connection_id="conn-1")
    assert snapshot.source_type == "aws_cloudtrail"
    assert snapshot.raw_payload_sha256
    assert snapshot.source_connection_id == "conn-1"


def test_evidence_hash_covers_content_beyond_display_limit():
    from app.aws_connector import AwsCheckResult

    shared_prefix = "x" * (MAX_NORMALIZED_PAYLOAD_CHARS + 100)
    first = build_evidence_snapshot(
        AwsCheckResult("iam_posture", "pass", "IAM posture", "ok", {"users": shared_prefix + "a"}),
        connection_id="conn-1",
    )
    second = build_evidence_snapshot(
        AwsCheckResult("iam_posture", "pass", "IAM posture", "ok", {"users": shared_prefix + "b"}),
        connection_id="conn-1",
    )

    assert len(first.normalized_payload_json) == MAX_NORMALIZED_PAYLOAD_CHARS
    assert first.normalized_payload_json == second.normalized_payload_json
    assert first.raw_payload_sha256 != second.raw_payload_sha256


# --- Router: connection settings, admin gating ---


def test_aws_connector_page_shows_not_configured(logged_in_client):
    response = logged_in_client.get("/connectors/aws")
    assert response.status_code == 200
    assert b"Not configured" in response.content


def test_edit_requires_admin(logged_in_client):
    response = logged_in_client.get("/connectors/aws/edit", follow_redirects=False)
    assert response.status_code == 403


def test_admin_can_save_connection_and_external_id_is_encrypted(admin_client, app):
    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/connectors/aws/edit")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/connectors/aws",
        data={
            "account_label": "Prod AWS",
            "expected_account_id": "123456789012",
            "role_arn": "arn:aws:iam::123456789012:role/Evidence",
            "external_id": "super-secret-external-id",
            "regions": "us-east-1",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        connection = session.scalar(select(AwsConnection))
        assert connection.account_label == "Prod AWS"
        assert connection.encrypted_external_id != "super-secret-external-id"
        events = session.scalars(select(AuditEvent).where(AuditEvent.entity_type == "aws_connection")).all()
    assert any(e.action == "save" for e in events)
    assert "super-secret-external-id" not in events[0].detail


def test_non_admin_cannot_save_connection(logged_in_client):
    response = logged_in_client.post(
        "/connectors/aws",
        data={"account_label": "x", "csrf_token": "y"},
        follow_redirects=False,
    )
    assert response.status_code == 403


@patch("app.routers.aws_connector.test_connection")
@patch("app.routers.aws_connector.build_session")
def test_run_test_connection_route(mock_build_session, mock_test_connection, admin_client, app):
    from app.aws_connector import AwsCheckResult

    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/connectors/aws/edit")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/connectors/aws",
        data={"account_label": "Prod AWS", "regions": "us-east-1", "csrf_token": csrf_token},
    )

    mock_test_connection.return_value = AwsCheckResult("connection_test", "pass", "AWS connection", "ok")
    page = admin_client.get("/connectors/aws")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/connectors/aws/test", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]


@pytest.mark.parametrize("route", ["/connectors/aws/test", "/connectors/aws/run-checks"])
def test_aws_routes_stop_when_stored_external_id_cannot_be_decrypted(route, admin_client, app):
    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/connectors/aws/edit")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/connectors/aws",
        data={
            "account_label": "Prod AWS",
            "role_arn": "arn:aws:iam::123456789012:role/Evidence",
            "external_id": "external-id-fixture",
            "regions": "us-east-1",
            "csrf_token": csrf_token,
        },
    )
    app.state.settings.encryption_key = Fernet.generate_key().decode()

    page = admin_client.get("/connectors/aws")
    csrf_token = extract_csrf_token(page.text)
    with patch("app.routers.aws_connector.build_session") as mock_build_session:
        response = admin_client.post(route, data={"csrf_token": csrf_token}, follow_redirects=False)

    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    mock_build_session.assert_not_called()
    with app.state.session_factory() as session:
        connection = session.scalar(select(AwsConnection))
        assert "could not be decrypted" in connection.last_error_summary


@patch("app.routers.aws_connector.check_iam")
@patch("app.routers.aws_connector.check_cloudtrail")
@patch("app.routers.aws_connector.build_session")
def test_run_checks_writes_evidence_snapshots(
    mock_build_session, mock_cloudtrail, mock_iam, admin_client, app
):
    from app.aws_connector import AwsCheckResult

    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/connectors/aws/edit")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/connectors/aws",
        data={"account_label": "Prod AWS", "regions": "us-east-1", "csrf_token": csrf_token},
    )

    mock_cloudtrail.return_value = AwsCheckResult("cloudtrail_posture", "pass", "CloudTrail posture", "ok")
    mock_iam.return_value = AwsCheckResult("iam_posture", "warning", "IAM posture", "some warning")

    page = admin_client.get("/connectors/aws")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/connectors/aws/run-checks", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        snapshots = session.scalars(select(EvidenceSnapshot)).all()
        assert len(snapshots) == 2
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "run_checks")).all()
    assert len(events) == 1


def test_run_checks_requires_admin(logged_in_client):
    response = logged_in_client.post(
        "/connectors/aws/run-checks", data={"csrf_token": "x"}, follow_redirects=False
    )
    assert response.status_code == 403


# --- Evidence list/detail/mapping ---


def test_evidence_list_loads(logged_in_client):
    response = logged_in_client.get("/evidence")
    assert response.status_code == 200
    assert b"Evidence" in response.content


def test_evidence_detail_and_mapping(logged_in_client, app):
    with app.state.session_factory() as session:
        import hashlib

        snapshot = EvidenceSnapshot(
            source_type="aws_cloudtrail",
            check_key="cloudtrail_posture",
            status="pass",
            title="CloudTrail posture",
            summary="ok",
            normalized_payload_json="{}",
            raw_payload_sha256=hashlib.sha256(b"{}").hexdigest(),
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = snapshot.id

        from app.models import Framework, FrameworkRequirement

        framework = Framework(name="Test", version="1.0")
        session.add(framework)
        session.flush()
        requirement = FrameworkRequirement(framework_id=framework.id, reference_code="X.1", title="Req")
        session.add(requirement)
        session.commit()
        requirement_id = requirement.id

    detail = logged_in_client.get(f"/evidence/{snapshot_id}")
    assert detail.status_code == 200
    csrf_token = extract_csrf_token(detail.text)

    response = logged_in_client.post(
        f"/evidence/{snapshot_id}/map-requirement",
        data={"requirement_id": requirement_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    # duplicate mapping is rejected cleanly, not a 500
    detail = logged_in_client.get(f"/evidence/{snapshot_id}")
    csrf_token = extract_csrf_token(detail.text)
    dup = logged_in_client.post(
        f"/evidence/{snapshot_id}/map-requirement",
        data={"requirement_id": requirement_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert dup.status_code == 303
    assert "flash_kind=error" in dup.headers["location"]


# --- CLI ---


def test_cli_aws_run_checks_requires_connection(tmp_path, monkeypatch):
    from app import cli
    from app.config import get_settings

    db_path = tmp_path / "aws_cli.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    exit_code = cli.aws_run_checks()
    assert exit_code == 1
    get_settings.cache_clear()


@patch("app.cli.check_iam")
@patch("app.cli.check_cloudtrail")
@patch("app.cli.build_session")
def test_cli_aws_run_checks_writes_snapshots(
    mock_build_session, mock_cloudtrail, mock_iam, tmp_path, monkeypatch
):
    from app import cli
    from app.aws_connector import AwsCheckResult
    from app.config import get_settings
    from app.db import build_engine, make_session_factory

    db_path = tmp_path / "aws_cli2.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        cli.create_user("admin@example.com")

    engine = build_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        admin = session.scalar(select(__import__("app.models", fromlist=["User"]).User))
        session.add(
            AwsConnection(account_label="CLI Test", configured_by_user_id=admin.id, regions="us-east-1")
        )
        session.commit()

    mock_cloudtrail.return_value = AwsCheckResult("cloudtrail_posture", "pass", "CloudTrail posture", "ok")
    mock_iam.return_value = AwsCheckResult("iam_posture", "pass", "IAM posture", "ok")

    exit_code = cli.aws_run_checks()
    assert exit_code == 0

    with session_factory() as session:
        snapshots = session.scalars(select(EvidenceSnapshot)).all()
    assert len(snapshots) == 2
    get_settings.cache_clear()


def test_cli_aws_run_checks_handles_assume_role_failure(tmp_path, monkeypatch):
    from app import cli
    from app.config import get_settings
    from app.db import build_engine, make_session_factory

    db_path = tmp_path / "aws_cli3.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        cli.create_user("admin@example.com")

    engine = build_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        admin = session.scalar(select(__import__("app.models", fromlist=["User"]).User))
        session.add(
            AwsConnection(
                account_label="CLI Test",
                configured_by_user_id=admin.id,
                regions="us-east-1",
                role_arn="arn:aws:iam::123456789012:role/Bad",
            )
        )
        session.commit()

    with patch("app.cli.build_session", side_effect=AwsConnectionError("AssumeRole failed: access denied")):
        exit_code = cli.aws_run_checks()
    assert exit_code == 1
    get_settings.cache_clear()
