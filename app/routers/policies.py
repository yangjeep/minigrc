from __future__ import annotations

import datetime
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.models import POLICY_STATUSES, Policy, PolicyVersion
from app.storage import UploadValidationError, policy_version_path, save_policy_version_upload

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
