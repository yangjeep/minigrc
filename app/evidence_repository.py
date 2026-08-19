"""Evidence artifact capture and event projectors (issue #32).

Builds on app/events.py (issue #22): unlike PolicyVersion (#31), which
kept file-capture as a plain, non-event-sourced fact,
`EvidenceArtifactVersion` is a canonical projection over the
"evidence_artifact_version" DomainEvent aggregate — the row does not
exist independent of its capture event (same category as
ControlOccurrence, #11). See
docs/superpowers/specs/2026-08-19-issue32-evidence-repository-design.md
§2 for why this domain makes a different choice than #31.
"""

from __future__ import annotations

import datetime
import json
import os

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import ControlOccurrenceEvidenceArtifact, EvidenceArtifact, EvidenceArtifactVersion, new_id
from app.object_storage import delete_object, object_key_for, upload_object


def _parse_iso_datetime(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def _project_captured(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        EvidenceArtifactVersion(
            id=event.aggregate_id,
            artifact_id=payload["artifact_id"],
            version_number=payload["version_number"],
            object_key=payload["object_key"],
            original_filename=payload["original_filename"],
            media_type=payload["media_type"],
            byte_size=payload["byte_size"],
            sha256=payload["sha256"],
            source_type=payload["source_type"],
            source_connection_id=payload.get("source_connection_id"),
            source_object_id=payload.get("source_object_id"),
            source_revision_id=payload.get("source_revision_id"),
            source_modified_at=_parse_iso_datetime(payload.get("source_modified_at")),
            linked_evidence_snapshot_id=payload.get("linked_evidence_snapshot_id"),
            captured_at=event.occurred_at,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
        )
    )
    # Explicit flush — same reasoning as ControlOccurrence's
    # _project_materialized: a later event for this aggregate (there
    # are none today, but the contract should hold regardless) could
    # need session.get() to see this row before anything else triggers
    # an autoflush.
    session.flush()


EVIDENCE_ARTIFACT_VERSION_PROJECTORS: dict[str, ProjectorFn] = {
    "EvidenceArtifactVersionCaptured": _project_captured,
}


def _reset_evidence_artifact_versions(session: Session) -> None:
    # Child-before-parent: ControlOccurrenceEvidenceArtifact FK-references
    # EvidenceArtifactVersion, so it must be cleared first or the second
    # delete violates the foreign key. It is NOT re-populated by this
    # rebuild (it's owned by the "control_occurrence" aggregate's own
    # projector, app/control_occurrences.py) — a full recovery from a
    # backup/corruption must also call rebuild_control_occurrence_projection
    # afterward to restore any evidence-artifact links. Documented here
    # deliberately, not silently assumed.
    session.execute(delete(ControlOccurrenceEvidenceArtifact))
    session.execute(delete(EvidenceArtifactVersion))


def rebuild_evidence_artifact_version_projection(session: Session) -> None:
    """Wipe and deterministically rebuild the EvidenceArtifactVersion
    projection from DomainEvent history. See _reset_evidence_artifact_versions
    for why ControlOccurrenceEvidenceArtifact must be separately rebuilt
    afterward via rebuild_control_occurrence_projection."""
    rebuild_projection(
        session,
        EVIDENCE_ARTIFACT_VERSION_PROJECTORS,
        aggregate_type="evidence_artifact_version",
        reset=_reset_evidence_artifact_versions,
    )


def capture_evidence_version(
    session: Session,
    artifact: EvidenceArtifact,
    *,
    client,
    bucket: str,
    tmp_path: str,
    sha256: str,
    byte_size: int,
    media_type: str,
    original_filename: str,
    extension: str,
    source_type: str = "manual",
    source_connection_id: str | None = None,
    source_object_id: str | None = None,
    source_revision_id: str | None = None,
    source_modified_at: datetime.datetime | None = None,
    linked_evidence_snapshot_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
    occurred_at: datetime.datetime | None = None,
) -> tuple[EvidenceArtifactVersion, bool]:
    """Capture one version of an evidence artifact. Idempotent by
    content: if `artifact` already has a version with this exact
    `sha256`, the object is never uploaded (the caller's `tmp_path` is
    removed) and the existing version is returned unchanged — `(version,
    False)`. Otherwise the bytes are uploaded to S3 *before* any
    DomainEvent/DB row is created (so a failed upload leaves nothing
    behind), and `(new_version, True)` is returned.

    If the upload succeeds but the subsequent event append fails for
    any reason, the just-uploaded object is deleted (best-effort) before
    the exception propagates — no S3 object is left orphaned by a
    failure this function caused (an orphan from a genuinely concurrent/
    partial failure elsewhere is the acceptable, non-authoritative
    residue app/object_storage.py::delete_object's docstring describes).
    This function does not roll back the session itself (same
    convention as every other domain function in this codebase — that
    is the caller's responsibility, e.g. app/deps.py::get_db for a real
    request); the DomainEvent row it appended remains pending in the
    session until the caller's own rollback/commit resolves it. See
    tests/test_sqlite_savepoint_rollback.py for a latent (not currently
    reachable by any real request/job path) gap in that guarantee when
    the caller's session already had an earlier, same-session commit
    before this call.

    Caller is responsible for ensuring `tmp_path` (from
    app/object_storage.py::capture_upload_bytes/capture_raw_bytes) still
    exists and matches `sha256`/`byte_size`.
    """
    existing = next((v for v in artifact.versions if v.sha256 == sha256), None)
    if existing is not None:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return existing, False

    version_id = new_id()
    object_key = object_key_for(artifact.id, version_id, extension)
    try:
        upload_object(client, bucket=bucket, object_key=object_key, tmp_path=tmp_path, media_type=media_type)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    next_version_number = (artifact.latest_version.version_number + 1) if artifact.latest_version else 1
    try:
        event = append_and_project(
            session,
            EVIDENCE_ARTIFACT_VERSION_PROJECTORS,
            aggregate_type="evidence_artifact_version",
            aggregate_id=version_id,
            event_type="EvidenceArtifactVersionCaptured",
            payload={
                "artifact_id": artifact.id,
                "version_number": next_version_number,
                "object_key": object_key,
                "original_filename": original_filename,
                "media_type": media_type,
                "byte_size": byte_size,
                "sha256": sha256,
                "source_type": source_type,
                "source_connection_id": source_connection_id,
                "source_object_id": source_object_id,
                "source_revision_id": source_revision_id,
                "source_modified_at": source_modified_at.isoformat() if source_modified_at else None,
                "linked_evidence_snapshot_id": linked_evidence_snapshot_id,
            },
            occurred_at=occurred_at,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    except Exception:
        delete_object(client, bucket=bucket, object_key=object_key)
        raise

    version = session.get(EvidenceArtifactVersion, event.aggregate_id)
    return version, True
