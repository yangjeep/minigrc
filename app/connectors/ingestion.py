"""The only path from a `ConnectorResult`/file-capture request into
domain state (issue #24) — generalizes the ingestion shapes that
already existed ad hoc per-integration
(`app.aws_connector.build_evidence_snapshot`,
`app.google_workspace_directory.sync_directory_users`,
`app.evidence_repository.capture_evidence_version`) into
capability-keyed functions any manifest-declaring connector can reuse.

None of these functions ever touch a control/finding/effectiveness
table — connector output is source fact/evidence, never a compliance
conclusion (RULES.md §9).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.manifest import ConnectorContractError, ConnectorManifest
from app.connectors.result import ConnectorProvenance, ConnectorResult, validate_result
from app.evidence_repository import capture_evidence_version
from app.models import EvidenceArtifact, EvidenceSnapshot, Person


def ingest_configuration_snapshot(
    session: Session, result: ConnectorResult, manifest: ConnectorManifest
) -> EvidenceSnapshot:
    """Turn a `configuration.snapshot` (or generic `evidence.collect`)
    result into an `EvidenceSnapshot` row — the same shape
    `app.aws_connector.build_evidence_snapshot` already produces,
    generalized so any manifest-declaring connector can reuse it."""
    validate_result(result, manifest)

    canonical_json = json.dumps(result.normalized_payload, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    snapshot = EvidenceSnapshot(
        source_type=manifest.connector_id,
        source_connection_id=result.provenance.source_connection_id,
        check_key=result.check_key,
        status=result.status,
        title=result.title,
        summary=result.summary[:2000],
        collected_at=result.provenance.collected_at,
        collector_version=result.provenance.collector_version,
        normalized_payload_json=canonical_json,
        raw_payload_sha256=payload_hash,
    )
    session.add(snapshot)
    return snapshot


def ingest_identity_population(
    session: Session, result: ConnectorResult, manifest: ConnectorManifest
) -> dict:
    """Turn an `identity.population` result into `Person` upserts — the
    same shape `app.google_workspace_directory.sync_directory_users`
    already produces, generalized. Never deletes: a person missing from
    this population is simply not touched, preserving history rather
    than guessing why they're absent."""
    validate_result(result, manifest)

    users = result.normalized_payload.get("users", [])
    created = 0
    updated = 0
    for user in users:
        normalized_email = user["email"].strip().lower()
        if not normalized_email:
            continue

        status = "suspended" if user.get("suspended") else "active"
        person = session.scalar(select(Person).where(Person.email == normalized_email))
        if person is None:
            session.add(
                Person(
                    email=normalized_email,
                    display_name=user.get("display_name", ""),
                    employment_status=status,
                    source=manifest.connector_id,
                    external_id=user.get("external_id"),
                    last_synced_at=result.provenance.collected_at,
                )
            )
            created += 1
        else:
            person.display_name = user.get("display_name") or person.display_name
            person.employment_status = status
            person.source = manifest.connector_id
            person.external_id = user.get("external_id")
            person.last_synced_at = result.provenance.collected_at
            updated += 1

    return {"created": created, "updated": updated, "total": len(users)}


def ingest_file_capture(
    session: Session,
    artifact: EvidenceArtifact,
    *,
    provenance: ConnectorProvenance,
    manifest: ConnectorManifest,
    client,
    bucket: str,
    tmp_path: str,
    sha256: str,
    byte_size: int,
    media_type: str,
    original_filename: str,
    extension: str,
    linked_evidence_snapshot_id: str | None = None,
    actor_type: str = "connector",
    actor_id: str | None = None,
):
    """Thin wrapper around `app.evidence_repository.capture_evidence_version`
    for the `file.capture` capability — file bytes genuinely don't fit a
    JSON `ConnectorResult` payload, so this takes the same provenance
    structure but forwards to the existing authorized capture command
    rather than duplicating its idempotent-by-hash/no-orphaned-upload
    guarantees."""
    if "file.capture" not in manifest.capabilities:
        raise ConnectorContractError(
            f"connector {manifest.connector_id!r} does not declare the file.capture capability"
        )

    return capture_evidence_version(
        session,
        artifact,
        client=client,
        bucket=bucket,
        tmp_path=tmp_path,
        sha256=sha256,
        byte_size=byte_size,
        media_type=media_type,
        original_filename=original_filename,
        extension=extension,
        source_type=manifest.connector_id,
        source_connection_id=provenance.source_connection_id,
        source_object_id=provenance.source_object_id,
        source_revision_id=provenance.source_revision_id,
        source_modified_at=provenance.source_modified_at,
        linked_evidence_snapshot_id=linked_evidence_snapshot_id,
        actor_type=actor_type,
        actor_id=actor_id,
        occurred_at=provenance.collected_at,
    )
