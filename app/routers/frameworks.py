from __future__ import annotations

import datetime
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.csv_import import CsvTooLargeError, read_csv_upload
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.imports import enqueue_and_run_import
from app.models import (
    APPLICABILITY_VALUES,
    IMPLEMENTATION_STATES,
    Framework,
    FrameworkRequirement,
    RequirementNote,
)
from app.progress import compute_progress
from app.registers.config import FieldSpec, RegisterConfig
from app.registers.router import build_register_router
from app.requirements import add_requirement

router = APIRouter(prefix="/frameworks", tags=["frameworks"], dependencies=[Depends(require_login)])

REQUIREMENTS_REGISTER_CONFIG = RegisterConfig(
    name="framework-requirements",
    model=FrameworkRequirement,
    entity_type="framework_requirement",
    scope_field="framework_id",
    creatable=False,
    deletable=False,
    order_by=FrameworkRequirement.display_order,
    fields=(
        FieldSpec(name="reference_code", type="text", required=True, max_length=32),
        FieldSpec(name="title", type="text", required=True, max_length=255),
        FieldSpec(name="summary", type="text"),
        FieldSpec(name="display_order", type="number"),
        FieldSpec(
            name="applicable",
            type="text",
            read_only=True,
            compute=lambda r: r.assessment.applicable if r.assessment else None,
        ),
        FieldSpec(
            name="implementation_state",
            type="text",
            read_only=True,
            compute=lambda r: r.assessment.implementation_state if r.assessment else None,
        ),
        FieldSpec(
            name="owner",
            type="text",
            read_only=True,
            compute=lambda r: r.assessment.owner if r.assessment else "",
        ),
    ),
)

requirements_register_router = build_register_router(REQUIREMENTS_REGISTER_CONFIG)


@router.get("")
def list_frameworks(request: Request, db: Session = Depends(get_db)):
    frameworks = db.scalars(select(Framework).order_by(Framework.name)).all()
    progress_by_id = {f.id: compute_progress(f) for f in frameworks}
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "frameworks/list.html", {"frameworks": frameworks, "progress_by_id": progress_by_id}
    )


@router.get("/new")
def new_framework_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "frameworks/new.html", {})


@router.post("")
def create_framework(
    request: Request,
    name: str = Form(...),
    version: str = Form(...),
    description: str = Form(""),
    is_placeholder_content: bool = Form(False),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    name = name.strip()
    version = version.strip()
    if not name or not version:
        return redirect_with_flash("/frameworks/new", "Name and version are required.", kind="error")

    framework = Framework(
        name=name, version=version, description=description, is_placeholder_content=is_placeholder_content
    )
    db.add(framework)
    db.flush()
    record_audit_event(
        db,
        entity_type="framework",
        entity_id=framework.id,
        action="create",
        detail=f"Created framework '{framework.name}' {framework.version}",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/frameworks/{framework.id}", "Framework created.")


@router.get("/{framework_id}")
def view_framework(framework_id: str, request: Request, db: Session = Depends(get_db)):
    framework = db.get(Framework, framework_id)
    if framework is None:
        raise HTTPException(status_code=404, detail="Framework not found")

    progress = compute_progress(framework)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "frameworks/detail.html",
        {"framework": framework, "progress": progress},
    )


@router.get("/{framework_id}/edit")
def edit_framework_form(framework_id: str, request: Request, db: Session = Depends(get_db)):
    framework = db.get(Framework, framework_id)
    if framework is None:
        raise HTTPException(status_code=404, detail="Framework not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "frameworks/edit.html", {"framework": framework})


@router.post("/{framework_id}/edit")
def update_framework(
    framework_id: str,
    request: Request,
    name: str = Form(...),
    version: str = Form(...),
    description: str = Form(""),
    is_placeholder_content: bool = Form(False),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    framework = db.get(Framework, framework_id)
    if framework is None:
        raise HTTPException(status_code=404, detail="Framework not found")

    name = name.strip()
    version = version.strip()
    if not name or not version:
        return redirect_with_flash(
            f"/frameworks/{framework_id}/edit", "Name and version are required.", kind="error"
        )

    framework.name = name
    framework.version = version
    framework.description = description
    framework.is_placeholder_content = is_placeholder_content
    framework.is_active = is_active
    record_audit_event(
        db,
        entity_type="framework",
        entity_id=framework.id,
        action="update",
        detail=f"Updated framework metadata for '{framework.name}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/frameworks/{framework_id}", "Framework updated.")


@router.get("/{framework_id}/requirements/new")
def new_requirement_form(framework_id: str, request: Request, db: Session = Depends(get_db)):
    framework = db.get(Framework, framework_id)
    if framework is None:
        raise HTTPException(status_code=404, detail="Framework not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "frameworks/requirement_new.html", {"framework": framework})


@router.post("/{framework_id}/requirements")
def create_requirement(
    framework_id: str,
    request: Request,
    reference_code: str = Form(...),
    title: str = Form(...),
    summary: str = Form(""),
    display_order: int = Form(0),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    framework = db.get(Framework, framework_id)
    if framework is None:
        raise HTTPException(status_code=404, detail="Framework not found")

    reference_code = reference_code.strip()
    title = title.strip()
    if not reference_code or not title:
        return redirect_with_flash(
            f"/frameworks/{framework_id}/requirements/new",
            "Reference code and title are required.",
            kind="error",
        )

    try:
        requirement = add_requirement(
            db,
            framework,
            reference_code=reference_code,
            title=title,
            summary=summary,
            display_order=display_order,
        )
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect_with_flash(
            f"/frameworks/{framework_id}/requirements/new",
            f"Reference code '{reference_code}' already exists in this framework.",
            kind="error",
        )

    record_audit_event(
        db,
        entity_type="framework",
        entity_id=framework.id,
        action="add_requirement",
        detail=f"Added requirement '{requirement.reference_code}' to '{framework.name}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/frameworks/{framework_id}", "Requirement added.")


@router.post("/{framework_id}/import")
def import_requirements(
    framework_id: str,
    request: Request,
    file: UploadFile,
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    framework = db.get(Framework, framework_id)
    if framework is None:
        raise HTTPException(status_code=404, detail="Framework not found")

    settings = request.app.state.settings
    try:
        raw_bytes = read_csv_upload(file, max_bytes=settings.max_upload_bytes)
    except CsvTooLargeError as exc:
        return redirect_with_flash(f"/frameworks/{framework_id}", str(exc), kind="error")

    import_job = enqueue_and_run_import(
        db,
        importer_name="framework_requirements_csv",
        raw_bytes=raw_bytes,
        filename=file.filename or "upload.csv",
        target={"framework_id": framework_id},
        actor=request.state.user.email,
        source="web",
    )

    if import_job.status != "completed":
        errors = json.loads(import_job.validation_errors_json or "[]")
        preview = "; ".join(errors[:5])
        return redirect_with_flash(f"/frameworks/{framework_id}", f"Import failed: {preview}", kind="error")

    return redirect_with_flash(
        f"/frameworks/{framework_id}", f"Imported {import_job.records_created} requirement(s)."
    )


@router.get("/{framework_id}/requirements/{requirement_id}")
def view_requirement(framework_id: str, requirement_id: str, request: Request, db: Session = Depends(get_db)):
    requirement = db.get(FrameworkRequirement, requirement_id)
    if requirement is None or requirement.framework_id != framework_id:
        raise HTTPException(status_code=404, detail="Requirement not found")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "frameworks/requirement_detail.html",
        {
            "framework": requirement.framework,
            "requirement": requirement,
            "assessment": requirement.assessment,
            "notes": list(reversed(requirement.notes)),
            "implementation_states": IMPLEMENTATION_STATES,
        },
    )


@router.post("/{framework_id}/requirements/{requirement_id}/assessment")
def update_assessment(
    framework_id: str,
    requirement_id: str,
    request: Request,
    applicable: str = Form(...),
    implementation_state: str = Form(...),
    owner: str = Form(""),
    note_body: str = Form(""),
    mark_reviewed: bool = Form(False),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    requirement = db.get(FrameworkRequirement, requirement_id)
    if requirement is None or requirement.framework_id != framework_id:
        raise HTTPException(status_code=404, detail="Requirement not found")

    detail_url = f"/frameworks/{framework_id}/requirements/{requirement_id}"

    if applicable not in APPLICABILITY_VALUES:
        return redirect_with_flash(detail_url, "Invalid applicability value.", kind="error")
    if implementation_state not in IMPLEMENTATION_STATES:
        return redirect_with_flash(detail_url, "Invalid implementation state.", kind="error")

    note_body = note_body.strip()
    if applicable == "no" and not note_body:
        return redirect_with_flash(
            detail_url, "A note explaining why this is not applicable is required.", kind="error"
        )

    assessment = requirement.assessment
    before = {
        "applicable": assessment.applicable,
        "implementation_state": assessment.implementation_state,
        "owner": assessment.owner,
    }
    assessment.applicable = applicable
    assessment.implementation_state = implementation_state
    assessment.owner = owner
    if mark_reviewed:
        assessment.last_reviewed_at = datetime.datetime.now(datetime.UTC)
        assessment.last_reviewed_by = request.state.user.email

    if note_body:
        db.add(
            RequirementNote(requirement_id=requirement.id, author=request.state.user.email, body=note_body)
        )
        record_audit_event(
            db,
            entity_type="requirement_note",
            entity_id=requirement.id,
            action="create",
            detail=f"Note added to requirement '{requirement.reference_code}'",
            actor=request.state.user.email,
        )

    after = {
        "applicable": assessment.applicable,
        "implementation_state": assessment.implementation_state,
        "owner": assessment.owner,
    }
    record_audit_event(
        db,
        entity_type="requirement_assessment",
        entity_id=assessment.id,
        action="update",
        detail=f"Assessment for '{requirement.reference_code}' changed: before={before} after={after}",
        actor=request.state.user.email,
    )
    return redirect_with_flash(detail_url, "Assessment updated.")


@router.post("/{framework_id}/requirements/{requirement_id}/notes")
def add_note(
    framework_id: str,
    requirement_id: str,
    request: Request,
    body: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    requirement = db.get(FrameworkRequirement, requirement_id)
    if requirement is None or requirement.framework_id != framework_id:
        raise HTTPException(status_code=404, detail="Requirement not found")

    detail_url = f"/frameworks/{framework_id}/requirements/{requirement_id}"
    body = body.strip()
    if not body:
        return redirect_with_flash(detail_url, "Note cannot be empty.", kind="error")

    db.add(RequirementNote(requirement_id=requirement.id, author=request.state.user.email, body=body))
    record_audit_event(
        db,
        entity_type="requirement_note",
        entity_id=requirement.id,
        action="create",
        detail=f"Note added to requirement '{requirement.reference_code}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(detail_url, "Note added.")
