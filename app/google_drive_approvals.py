"""Google Drive Approvals — optional, best-effort capability.

https://developers.google.com/workspace/drive/api/reference/rest/v3/approvals

Drive's approval-workflow metadata isn't available on every tenant, scope
grant, or file — this module never claims otherwise. A caller must catch
`ApprovalsUnavailableError` and show "Approval data unavailable" rather
than fail a policy sync. This app never mirrors approvals to build an
internal approval workflow, and never approves/declines on a user's
behalf — it only reads and preserves the history Drive already has.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass

import httpx

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"


class ApprovalsUnavailableError(Exception):
    """Raised on any failure reading approvals — always caught by the caller."""


def fetch_approvals(file_id: str, *, access_token: str, timeout: float = 10.0) -> list[dict]:
    """Best-effort raw approval payloads for one Drive file.

    Raises ApprovalsUnavailableError for a missing/forbidden/malformed
    response — including a tenant where the Approvals API doesn't apply
    at all, which surfaces as an ordinary 404/403 from Google.
    """
    try:
        response = httpx.get(
            f"{DRIVE_API_BASE}/files/{file_id}/approvals",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ApprovalsUnavailableError(str(exc)) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ApprovalsUnavailableError("Malformed approvals response.") from exc

    approvals = payload.get("approvals")
    if not isinstance(approvals, list):
        raise ApprovalsUnavailableError("Unexpected approvals response shape.")
    return approvals


def _parse_timestamp(value: object) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class ParsedApproval:
    external_approval_id: str
    status: str
    initiator: str
    reviewer_responses_json: str
    create_time: datetime.datetime | None
    modify_time: datetime.datetime | None
    complete_time: datetime.datetime | None
    due_time: datetime.datetime | None
    file_content_change_behavior: str | None
    raw_payload_sha256: str


def parse_approval(raw: dict) -> ParsedApproval:
    """Validate + normalize one raw approval payload before it's stored.

    Tolerant of field-name variance across API versions/responses (e.g.
    `state` vs `status`) rather than assuming one exact shape — this app
    doesn't control Drive's response format and must not crash a sync over
    an unexpected-but-benign field.
    """
    external_id = str(raw.get("approvalId") or raw.get("id") or "").strip()
    if not external_id:
        raise ApprovalsUnavailableError("Approval record is missing an id.")

    canonical_json = json.dumps(raw, sort_keys=True, default=str)
    initiator = (raw.get("initiatingUser") or {}).get("emailAddress") or raw.get("initiator") or ""

    return ParsedApproval(
        external_approval_id=external_id,
        status=str(raw.get("state") or raw.get("status") or ""),
        initiator=str(initiator),
        reviewer_responses_json=json.dumps(raw.get("reviewers") or raw.get("responses") or [], default=str),
        create_time=_parse_timestamp(raw.get("createTime")),
        modify_time=_parse_timestamp(raw.get("modifyTime")),
        complete_time=_parse_timestamp(raw.get("completeTime")),
        due_time=_parse_timestamp(raw.get("dueTime")),
        file_content_change_behavior=raw.get("fileContentChangeBehavior"),
        raw_payload_sha256=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )
