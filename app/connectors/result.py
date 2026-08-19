"""The normalized connector result envelope (issue #24).

A `ConnectorResult` carries a JSON-shaped fact (`configuration.snapshot`,
`identity.population`, and future fact-shaped capabilities). `file.capture`
results are handled separately (`app.connectors.ingestion.ingest_file_capture`)
since file bytes genuinely don't fit a JSON payload — provenance is the
piece that unifies both shapes, not the payload.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field

from app.connectors.manifest import ConnectorContractError, ConnectorManifest
from app.models import EVIDENCE_STATUSES

MAX_NORMALIZED_PAYLOAD_CHARS = 20000

# Defense-in-depth only — the primary control is "a connector must never
# put secret material in a normalized payload in the first place,"
# matching existing AWS/Drive discipline. This is a safety net, not a
# substitute for that discipline.
#
# Anchored on the key's final segment, not "contains this substring
# anywhere" — a naive substring match would reject real, already-shipped,
# non-secret AWS field names like "password_policy_present" (whether a
# password policy exists, not a password value). A key ending in
# "_password"/"_token"/etc, or exactly equal to one of these terms, names
# the secret itself; a key merely *mentioning* one earlier does not.
_SECRET_SHAPED_KEY_TERMS = (
    "token",
    "secret",
    "password",
    "passwd",
    "access_key",
    "private_key",
    "api_key",
    "refresh_token",
)
_SECRET_SHAPED_KEY_RE = re.compile(r"(^|_)(" + "|".join(_SECRET_SHAPED_KEY_TERMS) + r")$", re.IGNORECASE)


@dataclass(frozen=True)
class ConnectorProvenance:
    source_connection_id: str | None
    collected_at: datetime.datetime
    source_object_id: str | None = None
    source_revision_id: str | None = None
    source_modified_at: datetime.datetime | None = None
    collector_version: str = "1"


@dataclass(frozen=True)
class ConnectorResult:
    capability: str
    check_key: str
    status: str  # one of app.models.EVIDENCE_STATUSES
    title: str
    summary: str
    provenance: ConnectorProvenance
    normalized_payload: dict = field(default_factory=dict)


def _find_secret_shaped_keys(payload: dict, *, prefix: str = "") -> list[str]:
    found = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if _SECRET_SHAPED_KEY_RE.search(key):
            found.append(path)
        if isinstance(value, dict):
            found.extend(_find_secret_shaped_keys(value, prefix=f"{path}."))
    return found


def validate_result(result: ConnectorResult, manifest: ConnectorManifest) -> None:
    """Raise ConnectorContractError if `result` violates the contract.

    Checks capability overclaiming (a connector can't emit a result for
    a capability it didn't declare), status vocabulary, payload size,
    and — as defense-in-depth — an obviously secret-shaped payload key.
    """
    if result.capability not in manifest.capabilities:
        raise ConnectorContractError(
            f"connector {manifest.connector_id!r} emitted a result for undeclared "
            f"capability {result.capability!r} (declared: {manifest.capabilities})"
        )
    if result.status not in EVIDENCE_STATUSES:
        raise ConnectorContractError(
            f"invalid result status {result.status!r}, must be one of {EVIDENCE_STATUSES}"
        )

    canonical_json = json.dumps(result.normalized_payload, sort_keys=True, default=str)
    if len(canonical_json) > MAX_NORMALIZED_PAYLOAD_CHARS:
        raise ConnectorContractError(
            f"normalized_payload exceeds {MAX_NORMALIZED_PAYLOAD_CHARS} chars "
            f"({len(canonical_json)} chars) — connector must summarize, not dump raw provider output"
        )

    secret_shaped_keys = _find_secret_shaped_keys(result.normalized_payload)
    if secret_shaped_keys:
        raise ConnectorContractError(
            f"normalized_payload contains secret-shaped key(s) {secret_shaped_keys} — "
            "connector results must never carry credential material"
        )
