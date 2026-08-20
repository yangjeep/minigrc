"""Google Drive as a real, working `file.capture` connector (issue #28).

Wires the existing, already-tested provider client (`app.google_drive`)
through the generic connector platform's execution/audit/idempotency
boundary (#25's `ConnectorInstance`/`ConnectorExecution`) and
file-capture ingestion path (#24's `ingest_file_capture` ->
`app.evidence_repository.capture_evidence_version`, #32).

Distinct from `app/routers/policies.py`'s pre-existing Drive-to-
`PolicyVersion` capture path: that captures a Drive file as the next
immutable version of a specific policy *document* (a different domain
concept, compliance-document lifecycle, from audit evidence). Both
reuse the same underlying OAuth connection and provider client;
neither depends on the other. See design doc §1.

The real OAuth credential lives in `GoogleDriveConnection`, not this
connector's `ConnectorInstance.secret_ref_json` (see design doc §2) —
this module never resolves that connection itself; the caller passes
an already-resolved `access_token`/`connection`, keeping this module
free of a service-layer-importing-a-router dependency.
"""

from __future__ import annotations

import datetime
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connector_lifecycle import ConnectorLifecycleError, install_connector_instance
from app.connectors.ingestion import ingest_file_capture
from app.connectors.manifest import ConnectorContractError, ConnectorManifest
from app.connectors.result import ConnectorProvenance
from app.google_drive import (
    captured_filename,
    download_file_content,
    get_file_metadata,
    list_revisions,
    parse_drive_file_id,
)
from app.models import ConnectorExecution, ConnectorInstance, EvidenceArtifact, GoogleDriveConnection, User
from app.object_storage import (
    ALLOWED_EVIDENCE_MEDIA_TYPES,
    ObjectStorageError,
    build_s3_client,
    capture_raw_bytes,
    extension_of,
    validate_evidence_content,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _resolve_source_modified_at(
    revisions: list[dict], current_revision_id: str | None
) -> datetime.datetime | None:
    """Best-effort, matching `app.routers.policies.capture_drive_version`'s
    own existing revision-matching logic — Drive's revision listing may
    be incomplete/purged (see app.google_drive's module docstring), so
    a miss here is expected, not an error."""
    for revision in revisions:
        if revision.get("id") == current_revision_id and revision.get("modifiedTime"):
            return datetime.datetime.fromisoformat(revision["modifiedTime"].replace("Z", "+00:00"))
    return None


def get_or_create_google_drive_instance(
    session: Session, manifest: ConnectorManifest, *, actor: User, encryption_key: str
) -> ConnectorInstance:
    """Idempotent: returns the existing `google_drive` `ConnectorInstance`
    if one exists, else installs exactly one. Drive is a singleton
    connection (`GoogleDriveConnection`) — unlike AWS, duplicate
    instances would be ambiguous about which execution ledger a capture
    belongs to, so this never installs a second one."""
    existing = session.scalar(
        select(ConnectorInstance).where(ConnectorInstance.connector_id == manifest.connector_id)
    )
    if existing is not None:
        return existing
    return install_connector_instance(
        session,
        manifest,
        display_name=manifest.display_name,
        config={},
        secret_values={},
        actor=actor,
        encryption_key=encryption_key,
    )


def run_google_drive_capture(
    session: Session,
    instance: ConnectorInstance,
    manifest: ConnectorManifest,
    *,
    drive_file_id: str,
    access_token: str,
    connection: GoogleDriveConnection,
    artifact: EvidenceArtifact,
    settings,
    idempotency_key: str,
    actor_id: str,
    triggered_by: str = "manual",
) -> ConnectorExecution:
    """The `file.capture` execution/ingestion boundary for Google
    Drive — the same shape `run_connector_instance` provides for
    JSON-fact capabilities, specialized for file bytes (which
    `run_connector_instance`'s generic `connector_callable`/
    `_INGESTION_DISPATCH` path deliberately excludes; see
    `app.connector_lifecycle`'s own comment on `_INGESTION_DISPATCH`).

    Disabled instance and a repeated `idempotency_key` are handled
    exactly like `run_connector_instance`. On any failure, rolls back
    and records a failure/error `ConnectorExecution` row — no partial
    evidence write survives a downstream failure, mirroring
    `run_connector_instance`'s own atomicity fix from #25.
    """
    if not instance.enabled:
        raise ConnectorLifecycleError(f"connector instance '{instance.display_name}' is disabled")

    existing = session.scalar(
        select(ConnectorExecution).where(
            ConnectorExecution.connector_instance_id == instance.id,
            ConnectorExecution.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    started_at = _now()
    tmp_path: str | None = None
    try:
        # parse_drive_file_id never fetches the given value as a URL —
        # it only extracts/validates an ID, the same SSRF-safe guard
        # app.routers.policies's Drive routes already apply before
        # get_file_metadata ever sees a caller-supplied value. Applied
        # here too since this issue doesn't add the HTTP route that
        # would otherwise be the sole enforcement point for it.
        drive_file_id = parse_drive_file_id(drive_file_id)
        metadata = get_file_metadata(drive_file_id, access_token=access_token)
        extension = extension_of(captured_filename(metadata))
        if extension not in ALLOWED_EVIDENCE_MEDIA_TYPES:
            allowed = ", ".join(sorted(ALLOWED_EVIDENCE_MEDIA_TYPES))
            raise ObjectStorageError(f"Unsupported Drive file type '.{extension}'. Allowed: {allowed}.")

        revisions = list_revisions(drive_file_id, access_token=access_token)
        content = download_file_content(
            metadata, access_token=access_token, max_bytes=settings.max_upload_bytes
        )

        captured = capture_raw_bytes(content, max_bytes=settings.max_upload_bytes)
        tmp_path = captured.tmp_path
        validate_evidence_content(tmp_path, extension)
        client = build_s3_client(settings)

        # This app's session factory uses expire_on_commit=False, so a
        # long-lived `artifact` object's `.versions` relationship (which
        # capture_evidence_version reads to compute the next version
        # number) can go stale across repeated captures against the
        # same artifact within one session — e.g. a batch job iterating
        # several Drive files, or simply calling this twice in a row in
        # a test. tests/test_evidence_repository.py's own tests already
        # work around this by refreshing between calls; doing it here
        # makes this function robust to that call pattern instead of
        # pushing the burden onto every future caller.
        session.refresh(artifact)

        provenance = ConnectorProvenance(
            source_connection_id=connection.id,
            collected_at=started_at,
            source_object_id=metadata.file_id,
            source_revision_id=metadata.current_revision_id,
            source_modified_at=_resolve_source_modified_at(revisions, metadata.current_revision_id),
        )
        version, created = ingest_file_capture(
            session,
            artifact,
            provenance=provenance,
            manifest=manifest,
            client=client,
            bucket=settings.s3_bucket,
            tmp_path=tmp_path,
            sha256=captured.sha256,
            byte_size=captured.byte_size,
            media_type=ALLOWED_EVIDENCE_MEDIA_TYPES[extension],
            original_filename=captured_filename(metadata),
            extension=extension,
            actor_type="connector",
            actor_id=actor_id,
        )
    except Exception as exc:  # noqa: BLE001 - any Drive/contract/storage failure lands here
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)
        session.rollback()
        execution = ConnectorExecution(
            connector_instance_id=instance.id,
            idempotency_key=idempotency_key,
            # "error" = a contract/validation violation this app itself
            # raised (matches run_connector_instance's own distinction);
            # "failure" = Drive/storage genuinely failed or rejected the
            # request (GoogleDriveError, ObjectStorageError's real S3
            # failures) — kept distinct for triage, not for security.
            status=(
                "error" if isinstance(exc, (ConnectorLifecycleError, ConnectorContractError)) else "failure"
            ),
            error_summary=str(exc)[:2000],
            triggered_by=triggered_by,
            actor_id=actor_id,
            started_at=started_at,
            finished_at=_now(),
        )
        session.add(execution)
        instance.last_failure_at = execution.finished_at
        instance.last_error_summary = execution.error_summary
        session.flush()
        return execution

    execution = ConnectorExecution(
        connector_instance_id=instance.id,
        idempotency_key=idempotency_key,
        status="success",
        result_summary=(
            f"file.capture: {version.original_filename} "
            f"(version {version.version_number}, {'new' if created else 'unchanged'})"
        )[:2000],
        triggered_by=triggered_by,
        actor_id=actor_id,
        started_at=started_at,
        finished_at=_now(),
    )
    session.add(execution)
    instance.last_success_at = execution.finished_at
    session.flush()
    return execution
