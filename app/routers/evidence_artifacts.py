"""Evidence artifact repository routes (issue #32).

Mirrors app/routers/policies.py's shape and #37's RBAC convention
(router-level require_login, per-mutation require_write_access). See
docs/superpowers/specs/2026-08-19-issue32-evidence-repository-design.md
§6.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.control_occurrences import link_evidence_artifact_version
from app.deps import get_db, require_login, require_write_access, verify_csrf
from app.digital_tpm import draft_prefill
from app.evidence_repository import capture_evidence_version
from app.flash import redirect_with_flash
from app.models import ControlOccurrence, EvidenceArtifact, EvidenceArtifactVersion
from app.object_storage import (
    ALLOWED_EVIDENCE_MEDIA_TYPES,
    ObjectStorageError,
    ObjectStorageNotConfiguredError,
    build_s3_client,
    capture_upload_bytes,
    download_object,
    extension_of,
    validate_evidence_content,
)
from app.storage import sanitize_original_filename

router = APIRouter(
    prefix="/evidence-artifacts", tags=["evidence-artifacts"], dependencies=[Depends(require_login)]
)


def _require_configured(request: Request) -> None:
    if not request.app.state.settings.evidence_repository_configured:
        raise HTTPException(status_code=503, detail="The evidence artifact repository is not configured.")


@router.get("")
def list_evidence_artifacts(request: Request, db: Session = Depends(get_db)):
    artifacts = db.scalars(select(EvidenceArtifact).order_by(EvidenceArtifact.title)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "evidence_artifacts/list.html",
        {
            "artifacts": artifacts,
            "configured": request.app.state.settings.evidence_repository_configured,
        },
    )


@router.get("/new")
def new_evidence_artifact_form(request: Request):
    _require_configured(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "evidence_artifacts/new.html", {})


def _capture_and_store_version(
    request: Request,
    db: Session,
    artifact: EvidenceArtifact,
    file: UploadFile,
    user,
) -> None:
    settings = request.app.state.settings
    original_filename = sanitize_original_filename(file.filename or "")
    extension = extension_of(original_filename)
    if extension not in ALLOWED_EVIDENCE_MEDIA_TYPES:
        allowed = ", ".join(sorted(ALLOWED_EVIDENCE_MEDIA_TYPES))
        raise ObjectStorageError(f"Unsupported file type '.{extension}'. Allowed: {allowed}.")

    captured = capture_upload_bytes(file, max_bytes=settings.max_upload_bytes)
    try:
        validate_evidence_content(captured.tmp_path, extension)
        client = build_s3_client(settings)
    except Exception:
        if os.path.exists(captured.tmp_path):
            os.remove(captured.tmp_path)
        raise

    _version, created = capture_evidence_version(
        db,
        artifact,
        client=client,
        bucket=settings.s3_bucket,
        tmp_path=captured.tmp_path,
        sha256=captured.sha256,
        byte_size=captured.byte_size,
        media_type=ALLOWED_EVIDENCE_MEDIA_TYPES[extension],
        original_filename=original_filename,
        extension=extension,
        source_type="manual",
        actor_type="user",
        actor_id=user.id,
    )
    record_audit_event(
        db,
        entity_type="evidence_artifact",
        entity_id=artifact.id,
        action="capture_version",
        detail=(
            f"Captured version of '{artifact.title}' ({original_filename})"
            if created
            else f"Duplicate capture for '{artifact.title}' ({original_filename}) — no new version"
        ),
        actor=request.state.user.email,
    )


@router.post("")
def create_evidence_artifact(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    file: UploadFile | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    _require_configured(request)
    title = title.strip()
    if not title:
        return redirect_with_flash("/evidence-artifacts/new", "Title is required.", kind="error")
    if file is None or not file.filename:
        return redirect_with_flash("/evidence-artifacts/new", "A file is required.", kind="error")

    artifact = EvidenceArtifact(title=title, description=description)
    db.add(artifact)
    db.flush()
    record_audit_event(
        db,
        entity_type="evidence_artifact",
        entity_id=artifact.id,
        action="create",
        detail=f"Created evidence artifact '{artifact.title}'",
        actor=request.state.user.email,
    )

    try:
        _capture_and_store_version(request, db, artifact, file, user)
    except (ObjectStorageError, ObjectStorageNotConfiguredError) as exc:
        db.rollback()
        return redirect_with_flash("/evidence-artifacts/new", str(exc), kind="error")

    return redirect_with_flash(f"/evidence-artifacts/{artifact.id}", "Evidence artifact captured.")


@router.get("/{artifact_id}")
def view_evidence_artifact(artifact_id: str, request: Request, db: Session = Depends(get_db)):
    artifact = db.get(EvidenceArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Evidence artifact not found")

    occurrences = db.scalars(select(ControlOccurrence).order_by(ControlOccurrence.due_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "evidence_artifacts/detail.html",
        {
            "artifact": artifact,
            "occurrences": occurrences,
            "configured": request.app.state.settings.evidence_repository_configured,
        },
    )


@router.post("/{artifact_id}/ai-draft-description")
def draft_evidence_description(
    artifact_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    artifact = db.get(EvidenceArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Evidence artifact not found")
    execution = draft_prefill(
        db, request.app.state.settings, task_type="evidence_description", entity=artifact, actor=user.email
    )
    return redirect_with_flash(f"/ai/tpm/drafts/{execution.id}", "Draft description generated.")


@router.post("/{artifact_id}/versions")
def upload_evidence_artifact_version(
    artifact_id: str,
    request: Request,
    file: UploadFile,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    _require_configured(request)
    artifact = db.get(EvidenceArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Evidence artifact not found")

    try:
        _capture_and_store_version(request, db, artifact, file, user)
    except (ObjectStorageError, ObjectStorageNotConfiguredError) as exc:
        db.rollback()
        return redirect_with_flash(f"/evidence-artifacts/{artifact_id}", str(exc), kind="error")

    return redirect_with_flash(f"/evidence-artifacts/{artifact_id}", "Version captured.")


@router.get("/{artifact_id}/versions/{version_id}/download")
def download_evidence_artifact_version(
    artifact_id: str, version_id: str, request: Request, db: Session = Depends(get_db)
):
    _require_configured(request)
    version = db.get(EvidenceArtifactVersion, version_id)
    if version is None or version.artifact_id != artifact_id:
        raise HTTPException(status_code=404, detail="Evidence artifact version not found")

    settings = request.app.state.settings
    try:
        client = build_s3_client(settings)
        content = download_object(client, bucket=settings.s3_bucket, object_key=version.object_key)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=content,
        media_type=version.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{version.original_filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{artifact_id}/versions/{version_id}/link-occurrence")
def link_evidence_artifact_version_route(
    artifact_id: str,
    version_id: str,
    request: Request,
    occurrence_id: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    version = db.get(EvidenceArtifactVersion, version_id)
    if version is None or version.artifact_id != artifact_id:
        raise HTTPException(status_code=404, detail="Evidence artifact version not found")
    occurrence = db.get(ControlOccurrence, occurrence_id)
    if occurrence is None:
        raise HTTPException(status_code=404, detail="Control occurrence not found")

    detail_url = f"/evidence-artifacts/{artifact_id}"
    try:
        with db.begin_nested():
            link_evidence_artifact_version(db, occurrence, version.id, actor_type="user", actor_id=user.id)
            db.flush()
    except IntegrityError:
        return redirect_with_flash(
            detail_url, "That evidence version is already linked to this occurrence.", kind="error"
        )

    record_audit_event(
        db,
        entity_type="control_occurrence",
        entity_id=occurrence.id,
        action="link_evidence_artifact",
        detail=f"Linked evidence artifact version {version.version_number} to occurrence",
        actor=request.state.user.email,
    )
    return redirect_with_flash(detail_url, "Evidence linked to occurrence.")
