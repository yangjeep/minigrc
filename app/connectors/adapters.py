"""Manifests + result adapters for the three existing integrations,
proving the connector contract fits real shapes without rewriting their
provider logic (issue #24 §2.4 — the live production routes are not
rewired to call through this contract in this slice; see the design
doc's deferred section for why).
"""

from __future__ import annotations

from app.aws_connector import AwsCheckResult
from app.connectors.manifest import ConfigFieldSpec, ConnectorManifest
from app.connectors.result import ConnectorProvenance, ConnectorResult
from app.google_workspace_directory import DirectoryUser


def aws_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_id="aws_cloudtrail_iam",
        display_name="AWS CloudTrail + IAM posture",
        publisher="minigrc",
        version="1.0.0",
        capabilities=("configuration.snapshot",),
        required_permissions=("cloudtrail:DescribeTrails", "iam:GenerateCredentialReport"),
        supported_auth_methods=("ambient_credentials", "assume_role"),
        config_schema=(
            ConfigFieldSpec(name="account_label", type="str"),
            ConfigFieldSpec(name="expected_account_id", type="str", required=False),
            ConfigFieldSpec(name="role_arn", type="str", required=False),
            ConfigFieldSpec(name="external_id", type="str", required=False, secret=True),
            ConfigFieldSpec(name="regions", type="str", required=False),
        ),
    )


def aws_check_result_to_connector_result(
    check: AwsCheckResult, *, connection_id: str, collected_at
) -> ConnectorResult:
    """Adapt the existing, unmodified `AwsCheckResult` into the generic
    envelope — no change to `app.aws_connector`'s provider-calling logic."""
    return ConnectorResult(
        capability="configuration.snapshot",
        check_key=check.check_key,
        status=check.status,
        title=check.title,
        summary=check.summary,
        provenance=ConnectorProvenance(source_connection_id=connection_id, collected_at=collected_at),
        normalized_payload=check.normalized_payload,
    )


def google_drive_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_id="google_drive",
        display_name="Google Drive",
        publisher="minigrc",
        version="1.0.0",
        capabilities=("file.capture",),
        required_permissions=("drive.readonly",),
        supported_auth_methods=("oauth2",),
        config_schema=(),  # org-level OAuth grant; no per-instance config today
    )


def workspace_directory_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_id="google_workspace",
        display_name="Google Workspace Directory",
        publisher="minigrc",
        version="1.0.0",
        capabilities=("identity.population",),
        required_permissions=("admin.directory.user.readonly",),
        supported_auth_methods=("oauth2",),
        config_schema=(),
    )


def directory_users_to_connector_result(
    users: list[DirectoryUser], *, connection_id: str, collected_at
) -> ConnectorResult:
    """Adapt the existing, unmodified `DirectoryUser` list into the
    generic envelope — no change to
    `app.google_workspace_directory.fetch_directory_users`."""
    return ConnectorResult(
        capability="identity.population",
        check_key="directory_users",
        status="pass",
        title="Google Workspace Directory population",
        summary=f"{len(users)} directory user(s) fetched",
        provenance=ConnectorProvenance(source_connection_id=connection_id, collected_at=collected_at),
        normalized_payload={
            "users": [
                {
                    "email": user.primary_email,
                    "display_name": user.display_name,
                    "suspended": user.suspended or user.archived,
                    "external_id": user.external_id,
                }
                for user in users
            ]
        },
    )
