from __future__ import annotations

import datetime
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_admin, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.google_drive import (
    GoogleDriveError,
    captured_filename,
    download_file_content,
    get_file_metadata,
    list_revisions,
    parse_drive_file_id,
)
from app.models import POLICY_STATUSES, Policy, PolicyVersion
from app.routers.google_drive import get_access_token_for_active_connection
from app.storage import (
    UploadValidationError,
    policy_version_path,
    save_policy_version_from_bytes,
    save_policy_version_upload,
)

router = APIRouter(prefix="/policies", tags=["policies"], dependencies=[Depends(require_login)])


def _parse_date(value: str) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


@router.get("")
def list_policies(request: Request, status: str = "", owner: str = "", db: Session = Depends(get_db)):
    query = select(Policy).where(Policy.archived.is_(False))
    if status:
        query = query.where(Policy.status == status)
    if owner:
        query = query.where(Policy.owner == owner)
    policies = db.scalars(query.order_by(Policy.title)).all()

    owners = sorted({p.owner for p in db.scalars(select(Policy)).all() if p.owner})
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "policies/list.html",
        {
            "policies": policies,
            "statuses": POLICY_STATUSES,
            "owners": owners,
            "selected_status": status,
            "selected_owner": owner,
        },
    )


@router.get("/new")
def new_policy_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "policies/new.html", {"statuses": POLICY_STATUSES})


@router.post("")
def create_policy(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    owner: str = Form(""),
    status: str = Form("draft"),
    effective_date: str = Form(""),
    next_review_date: str = Form(""),
    file: UploadFile | None = None,
    change_note: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    title = title.strip()
    if not title:
        return redirect_with_flash("/policies/new", "Title is required.", kind="error")
    if status not in POLICY_STATUSES:
        status = "draft"

    policy = Policy(
        title=title,
        description=description,
        owner=owner,
        status=status,
        effective_date=_parse_date(effective_date),
        next_review_date=_parse_date(next_review_date),
    )
    db.add(policy)
    db.flush()
    record_audit_event(
        db,
        entity_type="policy",
        entity_id=policy.id,
        action="create",
        detail=f"Created policy '{policy.title}'",
        actor=request.state.user.email,
    )

    if file is not None and file.filename:
        settings = request.app.state.settings
        try:
            stored = save_policy_version_upload(
                file,
                data_dir=settings.data_dir,
                policy_id=policy.id,
                version_number=1,
                max_bytes=settings.max_upload_bytes,
            )
        except UploadValidationError as exc:
            db.rollback()
            return redirect_with_flash("/policies/new", str(exc), kind="error")

        version = PolicyVersion(
            policy_id=policy.id,
            version_number=1,
            original_filename=stored.original_filename,
            stored_filename=stored.stored_filename,
            media_type=stored.media_type,
            byte_size=stored.byte_size,
            sha256=stored.sha256,
            uploader=request.state.user.email,
            change_note=change_note,
        )
        db.add(version)
        record_audit_event(
            db,
            entity_type="policy",
            entity_id=policy.id,
            action="upload_version",
            detail=f"Uploaded version 1 ({stored.original_filename})",
            actor=request.state.user.email,
        )

    return redirect_with_flash(f"/policies/{policy.id}", f"Policy '{policy.title}' created.")


@router.get("/{policy_id}")
def view_policy(policy_id: str, request: Request, db: Session = Depends(get_db)):
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "policies/detail.html", {"policy": policy, "statuses": POLICY_STATUSES}
    )


@router.post("/{policy_id}")
def update_policy(
    policy_id: str,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    owner: str = Form(""),
    status: str = Form("draft"),
    effective_date: str = Form(""),
    next_review_date: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    title = title.strip()
    if not title:
        return redirect_with_flash(f"/policies/{policy_id}", "Title is required.", kind="error")

    before = {"title": policy.title, "owner": policy.owner, "status": policy.status}
    policy.title = title
    policy.description = description
    policy.owner = owner
    policy.status = status if status in POLICY_STATUSES else policy.status
    policy.effective_date = _parse_date(effective_date)
    policy.next_review_date = _parse_date(next_review_date)

    record_audit_event(
        db,
        entity_type="policy",
        entity_id=policy.id,
        action="update",
        detail=f"Updated policy metadata: before={before} after="
        f"{{'title': '{policy.title}', 'owner': '{policy.owner}', 'status': '{policy.status}'}}",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/policies/{policy_id}", "Policy updated.")


@router.post("/{policy_id}/versions")
def upload_policy_version(
    policy_id: str,
    request: Request,
    file: UploadFile,
    change_note: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    next_version_number = (policy.latest_version.version_number + 1) if policy.latest_version else 1
    settings = request.app.state.settings

    try:
        stored = save_policy_version_upload(
            file,
            data_dir=settings.data_dir,
            policy_id=policy.id,
            version_number=next_version_number,
            max_bytes=settings.max_upload_bytes,
        )
    except UploadValidationError as exc:
        return redirect_with_flash(f"/policies/{policy_id}", str(exc), kind="error")

    version = PolicyVersion(
        policy_id=policy.id,
        version_number=next_version_number,
        original_filename=stored.original_filename,
        stored_filename=stored.stored_filename,
        media_type=stored.media_type,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        uploader=request.state.user.email,
        change_note=change_note,
    )
    db.add(version)
    record_audit_event(
        db,
        entity_type="policy",
        entity_id=policy.id,
        action="upload_version",
        detail=f"Uploaded version {next_version_number} ({stored.original_filename})",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/policies/{policy_id}", f"Version {next_version_number} uploaded.")


@router.post("/{policy_id}/retire")
def retire_policy(
    policy_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    policy.status = "retired"
    record_audit_event(
        db,
        entity_type="policy",
        entity_id=policy.id,
        action="retire",
        detail=f"Retired policy '{policy.title}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/policies/{policy_id}", "Policy retired.")


@router.get("/{policy_id}/versions/{version_id}/download")
def download_policy_version(policy_id: str, version_id: str, request: Request, db: Session = Depends(get_db)):
    version = db.get(PolicyVersion, version_id)
    if version is None or version.policy_id != policy_id:
        raise HTTPException(status_code=404, detail="Policy version not found")

    settings = request.app.state.settings
    path = policy_version_path(settings.data_dir, policy_id, version.version_number, version.stored_filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Stored file is missing")

    return FileResponse(
        path,
        media_type=version.media_type,
        filename=version.original_filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/{policy_id}/drive-link")
def link_drive_file(
    policy_id: str,
    request: Request,
    drive_url_or_id: str = Form(...),
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    """Associate this policy with a Drive file — metadata only, no content
    captured yet. Never fetches the submitted value as a URL: it is parsed
    into a file ID, then used solely to build our own Google API request."""
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    settings = request.app.state.settings
    try:
        file_id = parse_drive_file_id(drive_url_or_id)
        _connection, access_token = get_access_token_for_active_connection(db, settings)
        metadata = get_file_metadata(file_id, access_token=access_token)
    except GoogleDriveError as exc:
        return redirect_with_flash(f"/policies/{policy_id}", str(exc), kind="error")

    policy.source_type = "drive"
    policy.drive_file_id = metadata.file_id
    policy.drive_web_url = metadata.web_view_link
    policy.drive_mime_type = metadata.mime_type
    policy.drive_last_seen_revision_id = metadata.current_revision_id
    policy.drive_last_synced_at = datetime.datetime.now(datetime.UTC)

    record_audit_event(
        db,
        entity_type="policy",
        entity_id=policy.id,
        action="link_drive_file",
        detail=f"Linked policy '{policy.title}' to Drive file '{metadata.name}' ({metadata.file_id})",
        actor=admin.email,
    )
    return redirect_with_flash(f"/policies/{policy_id}", "Policy linked to Drive file.")


@router.post("/{policy_id}/drive-refresh")
def refresh_drive_metadata(
    policy_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    """Re-check the linked Drive file's current revision without capturing
    content — lets an admin see drift before deciding to capture."""
    policy = db.get(Policy, policy_id)
    if policy is None or not policy.drive_file_id:
        raise HTTPException(status_code=404, detail="Policy is not linked to a Drive file")

    settings = request.app.state.settings
    try:
        _connection, access_token = get_access_token_for_active_connection(db, settings)
        metadata = get_file_metadata(policy.drive_file_id, access_token=access_token)
    except GoogleDriveError as exc:
        return redirect_with_flash(f"/policies/{policy_id}", str(exc), kind="error")

    policy.drive_web_url = metadata.web_view_link
    policy.drive_mime_type = metadata.mime_type
    policy.drive_last_seen_revision_id = metadata.current_revision_id
    policy.drive_last_synced_at = datetime.datetime.now(datetime.UTC)
    return redirect_with_flash(f"/policies/{policy_id}", "Checked Drive for the current revision.")


@router.post("/{policy_id}/drive-capture")
def capture_drive_version(
    policy_id: str,
    request: Request,
    change_note: str = Form(""),
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    """Download/export the linked Drive file's current content and store it
    as the next immutable PolicyVersion — through the same validated
    storage pipeline as a manual upload (content-type check, size bound,
    hashing, atomic move). A failure here creates no partial version."""
    policy = db.get(Policy, policy_id)
    if policy is None or not policy.drive_file_id:
        raise HTTPException(status_code=404, detail="Policy is not linked to a Drive file")

    settings = request.app.state.settings
    try:
        connection, access_token = get_access_token_for_active_connection(db, settings)
        metadata = get_file_metadata(policy.drive_file_id, access_token=access_token)
        revisions = list_revisions(policy.drive_file_id, access_token=access_token)
        content = download_file_content(metadata, access_token=access_token)
    except GoogleDriveError as exc:
        return redirect_with_flash(f"/policies/{policy_id}", str(exc), kind="error")

    next_version_number = (policy.latest_version.version_number + 1) if policy.latest_version else 1
    try:
        stored = save_policy_version_from_bytes(
            content,
            original_filename=captured_filename(metadata),
            data_dir=settings.data_dir,
            policy_id=policy.id,
            version_number=next_version_number,
            max_bytes=settings.max_upload_bytes,
        )
    except UploadValidationError as exc:
        return redirect_with_flash(f"/policies/{policy_id}", str(exc), kind="error")

    source_modified_at = None
    for revision in revisions:
        if revision.get("id") == metadata.current_revision_id and revision.get("modifiedTime"):
            source_modified_at = datetime.datetime.fromisoformat(
                revision["modifiedTime"].replace("Z", "+00:00")
            )
            break

    version = PolicyVersion(
        policy_id=policy.id,
        version_number=next_version_number,
        original_filename=stored.original_filename,
        stored_filename=stored.stored_filename,
        media_type=stored.media_type,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        uploader=admin.email,
        change_note=change_note or "Captured from Google Drive",
        source_type="drive",
        source_file_id=metadata.file_id,
        source_revision_id=metadata.current_revision_id,
        source_modified_at=source_modified_at,
    )
    db.add(version)

    policy.drive_web_url = metadata.web_view_link
    policy.drive_mime_type = metadata.mime_type
    policy.drive_last_seen_revision_id = metadata.current_revision_id
    policy.drive_last_synced_at = datetime.datetime.now(datetime.UTC)
    connection.last_successful_sync_at = datetime.datetime.now(datetime.UTC)

    record_audit_event(
        db,
        entity_type="policy",
        entity_id=policy.id,
        action="capture_drive_version",
        detail=(
            f"Captured version {next_version_number} of '{policy.title}' from Drive file "
            f"'{metadata.name}' (revision {metadata.current_revision_id})"
        ),
        actor=admin.email,
    )
    return redirect_with_flash(
        f"/policies/{policy_id}", f"Captured version {next_version_number} from Drive."
    )
