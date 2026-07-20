from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.csv_import import CsvTooLargeError, read_csv_upload
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.imports import enqueue_and_run_import
from app.models import Risk

router = APIRouter(prefix="/risks", tags=["risks"], dependencies=[Depends(require_login)])


@router.get("")
def list_risks(request: Request, db: Session = Depends(get_db)):
    risks = db.scalars(select(Risk).order_by(Risk.created_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "risks/list.html", {"risks": risks})


@router.post("")
def create_risk(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form("general"),
    likelihood: int = Form(1),
    impact: int = Form(1),
    owner: str = Form(""),
    treatment_plan: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    title = title.strip()
    if not title:
        return redirect_with_flash("/risks", "Title is required.", kind="error")
    if not (1 <= likelihood <= 5) or not (1 <= impact <= 5):
        return redirect_with_flash("/risks", "Likelihood and impact must be between 1 and 5.", kind="error")

    risk = Risk(
        title=title,
        description=description,
        category=category,
        likelihood=likelihood,
        impact=impact,
        owner=owner,
        treatment_plan=treatment_plan,
    )
    db.add(risk)
    db.flush()
    record_audit_event(
        db,
        entity_type="risk",
        entity_id=risk.id,
        action="create",
        detail=f"Created risk '{risk.title}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash("/risks", f"Risk '{risk.title}' created.")


@router.post("/import")
def import_risks(
    request: Request,
    file: UploadFile,
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    settings = request.app.state.settings
    try:
        raw_bytes = read_csv_upload(file, max_bytes=settings.max_upload_bytes)
    except CsvTooLargeError as exc:
        return redirect_with_flash("/risks", str(exc), kind="error")

    import_job = enqueue_and_run_import(
        db,
        importer_name="risk_register_csv",
        raw_bytes=raw_bytes,
        filename=file.filename or "upload.csv",
        target={},
        actor=request.state.user.email,
        source="web",
    )

    if import_job.status != "completed":
        errors = json.loads(import_job.validation_errors_json or "[]")
        preview = "; ".join(errors[:5])
        return redirect_with_flash("/risks", f"Import failed: {preview}", kind="error")

    return redirect_with_flash("/risks", f"Imported {import_job.records_created} risk(s).")
