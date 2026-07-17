"""Minimal AWS CloudTrail + IAM posture evidence collector.

Not a CSPM — exactly two fixed check families: CloudTrail logging posture
and basic IAM hygiene (root MFA/keys, per-user MFA/key age, password
policy presence). Uses the standard AWS credential provider chain
(ambient workload credentials preferred), with optional `AssumeRole` on
top. Never accepts, stores, or logs long-lived AWS access keys — the only
AWS-side secret this app ever touches is an optional `external_id` for
`AssumeRole`, encrypted at rest (see app/crypto.py).

A failed API call is `unknown`/`error`, never interpreted as a failed
control — this app can't tell "not compliant" apart from "insufficient
permissions to check" and must not conflate them.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.models import EvidenceSnapshot

MAX_NORMALIZED_PAYLOAD_CHARS = 20000

BOTO_TIMEOUT_CONFIG = BotoConfig(connect_timeout=10, read_timeout=20, retries={"max_attempts": 2})
ASSUME_ROLE_SESSION_NAME = "minigrc-evidence-collector"
ACCESS_KEY_AGE_WARNING_DAYS = 90
MAX_IAM_USERS_IN_PAYLOAD = 200


class AwsConnectionError(ValueError):
    """User-facing reason an AWS connection/check could not run."""


@dataclass(frozen=True)
class AwsCheckResult:
    check_key: str
    status: str  # pass / fail / warning / unknown
    title: str
    summary: str
    normalized_payload: dict = field(default_factory=dict)


def _safe_error(exc: Exception) -> str:
    """First line only, bounded length — never let a raw boto exception
    (which can echo request parameters) reach the UI or an audit log
    unbounded."""
    return str(exc).splitlines()[0][:300]


def build_session(*, role_arn: str | None, external_id: str | None, region: str | None):
    """Ambient credential chain by default; optional AssumeRole on top."""
    base_session = boto3.Session(region_name=region)
    if not role_arn:
        return base_session

    sts = base_session.client("sts", config=BOTO_TIMEOUT_CONFIG)
    try:
        params = {"RoleArn": role_arn, "RoleSessionName": ASSUME_ROLE_SESSION_NAME}
        if external_id:
            params["ExternalId"] = external_id
        assumed = sts.assume_role(**params)
    except (ClientError, BotoCoreError) as exc:
        raise AwsConnectionError(f"AssumeRole failed: {_safe_error(exc)}") from exc

    credentials = assumed["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


def test_connection(session, *, expected_account_id: str = "") -> AwsCheckResult:
    client = session.client("sts", config=BOTO_TIMEOUT_CONFIG)
    try:
        identity = client.get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        raise AwsConnectionError(_safe_error(exc)) from exc

    account_id = identity.get("Account", "")
    if expected_account_id and account_id != expected_account_id:
        raise AwsConnectionError(f"Connected to account {account_id}, expected {expected_account_id}.")

    return AwsCheckResult(
        check_key="connection_test",
        status="pass",
        title="AWS connection",
        summary=f"Connected as {identity.get('Arn', 'unknown')} in account {account_id}.",
        normalized_payload={"account_id": account_id, "arn": identity.get("Arn", "")},
    )


def check_cloudtrail(session) -> AwsCheckResult:
    client = session.client("cloudtrail", config=BOTO_TIMEOUT_CONFIG)
    try:
        trails = client.describe_trails(includeShadowTrails=True).get("trailList", [])
    except (ClientError, BotoCoreError) as exc:
        return AwsCheckResult(
            "cloudtrail_posture",
            "unknown",
            "CloudTrail posture",
            f"Could not read CloudTrail configuration: {_safe_error(exc)}",
        )

    if not trails:
        return AwsCheckResult(
            "cloudtrail_posture",
            "fail",
            "CloudTrail posture",
            "No CloudTrail trail exists in this account/region.",
            {"trail_count": 0},
        )

    trail_summaries = []
    active_trail = None
    for trail in trails:
        try:
            trail_status = client.get_trail_status(Name=trail["TrailARN"])
        except (ClientError, BotoCoreError):
            trail_status = {}
        summary = {
            "name": trail.get("Name"),
            "trail_arn": trail.get("TrailARN"),
            "is_logging": bool(trail_status.get("IsLogging")),
            "multi_region": bool(trail.get("IsMultiRegionTrail")),
            "include_global_service_events": bool(trail.get("IncludeGlobalServiceEvents")),
            "log_file_validation_enabled": bool(trail.get("LogFileValidationEnabled")),
            "destination_bucket": trail.get("S3BucketName"),
        }
        trail_summaries.append(summary)
        if summary["is_logging"] and active_trail is None:
            active_trail = summary

    if active_trail is None:
        return AwsCheckResult(
            "cloudtrail_posture",
            "fail",
            "CloudTrail posture",
            "A trail exists but none is actively logging.",
            {"trails": trail_summaries},
        )

    warnings = []
    if not active_trail["multi_region"]:
        warnings.append("not multi-region")
    if not active_trail["include_global_service_events"]:
        warnings.append("excludes global service events")
    if not active_trail["log_file_validation_enabled"]:
        warnings.append("log file validation disabled")

    status = "warning" if warnings else "pass"
    summary_text = f"Trail '{active_trail['name']}' is actively logging"
    summary_text += f" ({', '.join(warnings)})." if warnings else "."

    return AwsCheckResult(
        "cloudtrail_posture",
        status,
        "CloudTrail posture",
        summary_text,
        {"trails": trail_summaries, "active_trail": active_trail},
    )


def check_iam(session) -> AwsCheckResult:
    client = session.client("iam", config=BOTO_TIMEOUT_CONFIG)
    payload: dict = {}
    failures: list[str] = []
    warnings: list[str] = []

    try:
        summary_map = client.get_account_summary()["SummaryMap"]
    except (ClientError, BotoCoreError) as exc:
        return AwsCheckResult(
            "iam_posture", "unknown", "IAM posture", f"Could not read account summary: {_safe_error(exc)}"
        )

    root_mfa_enabled = bool(summary_map.get("AccountMFAEnabled"))
    root_access_keys_present = bool(summary_map.get("AccountAccessKeysPresent"))
    payload["root_mfa_enabled"] = root_mfa_enabled
    payload["root_access_keys_present"] = root_access_keys_present
    if not root_mfa_enabled:
        failures.append("root MFA disabled")
    if root_access_keys_present:
        failures.append("root access keys present")

    try:
        client.get_account_password_policy()
        payload["password_policy_present"] = True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            payload["password_policy_present"] = False
            warnings.append("no account password policy")
        else:
            payload["password_policy_present"] = None
            warnings.append(f"could not read password policy: {_safe_error(exc)}")
    except BotoCoreError as exc:
        payload["password_policy_present"] = None
        warnings.append(f"could not read password policy: {_safe_error(exc)}")

    users_summary = []
    now = datetime.datetime.now(datetime.UTC)
    try:
        paginator = client.get_paginator("list_users")
        for page in paginator.paginate():
            for user in page.get("Users", []):
                if len(users_summary) >= MAX_IAM_USERS_IN_PAYLOAD:
                    warnings.append(f"more than {MAX_IAM_USERS_IN_PAYLOAD} IAM users — payload truncated")
                    break
                username = user["UserName"]
                mfa_devices = client.list_mfa_devices(UserName=username).get("MFADevices", [])
                access_keys = client.list_access_keys(UserName=username).get("AccessKeyMetadata", [])
                active_keys = [k for k in access_keys if k.get("Status") == "Active"]

                oldest_key_age_days = None
                if active_keys:
                    oldest_created = min(k["CreateDate"] for k in active_keys)
                    if oldest_created.tzinfo is None:
                        oldest_created = oldest_created.replace(tzinfo=datetime.UTC)
                    oldest_key_age_days = (now - oldest_created).days

                has_mfa = bool(mfa_devices)
                if not has_mfa:
                    warnings.append(f"user '{username}' has no MFA device")
                if oldest_key_age_days is not None and oldest_key_age_days > ACCESS_KEY_AGE_WARNING_DAYS:
                    warnings.append(
                        f"user '{username}' has an access key older than {ACCESS_KEY_AGE_WARNING_DAYS} days"
                    )

                users_summary.append(
                    {
                        "username": username,
                        "has_mfa": has_mfa,
                        "active_access_key_count": len(active_keys),
                        "oldest_active_key_age_days": oldest_key_age_days,
                    }
                )
            else:
                continue
            break
    except (ClientError, BotoCoreError) as exc:
        warnings.append(f"could not fully enumerate IAM users: {_safe_error(exc)}")

    payload["iam_user_count"] = len(users_summary)
    payload["users"] = users_summary

    if failures:
        status = "fail"
        summary_text = "; ".join(failures)
    elif warnings:
        status = "warning"
        shown = warnings[:5]
        summary_text = "; ".join(shown)
        if len(warnings) > 5:
            summary_text += f"; and {len(warnings) - 5} more"
    else:
        status = "pass"
        summary_text = "Root MFA enabled, no root access keys, all IAM users have MFA and recent access keys."

    return AwsCheckResult("iam_posture", status, "IAM posture", summary_text, payload)


def build_evidence_snapshot(result: AwsCheckResult, *, connection_id: str) -> EvidenceSnapshot:
    """Construct (but don't add-to-session/commit) an EvidenceSnapshot from
    a check result — shared by the admin UI route and the CLI so both
    write evidence in exactly the same shape."""
    canonical_json = json.dumps(result.normalized_payload, sort_keys=True, default=str)
    canonical_json = canonical_json[:MAX_NORMALIZED_PAYLOAD_CHARS]
    source_type = f"aws_{result.check_key.split('_')[0]}" if "_" in result.check_key else "aws"
    return EvidenceSnapshot(
        source_type=source_type,
        source_connection_id=connection_id,
        check_key=result.check_key,
        status=result.status,
        title=result.title,
        summary=result.summary[:2000],
        normalized_payload_json=canonical_json,
        raw_payload_sha256=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )
