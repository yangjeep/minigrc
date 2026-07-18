from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_admin, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.google_drive import GoogleDriveError
from app.google_workspace_directory import DirectorySyncError, fetch_directory_users, sync_directory_users
from app.models import EMPLOYMENT_STATUSES, Person
from app.routers.google_drive import get_access_token_for_active_connection
from app.security import normalize_email

router = APIRouter(prefix="/people", tags=["people"], dependencies=[Depends(require_login)])


@router.get("")
def list_people(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    query = select(Person)
    if status in EMPLOYMENT_STATUSES:
        query = query.where(Person.employment_status == status)
    needle = q.strip()
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.where(
            or_(
                Person.email.ilike(pattern, escape="\\"),
                Person.display_name.ilike(pattern, escape="\\"),
            )
        )
    people = db.scalars(query.order_by(Person.display_name, Person.email)).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "people/list.html",
        {
            "people": people,
            "employment_statuses": EMPLOYMENT_STATUSES,
            "q": q,
            "selected_status": status,
            "workspace_directory_enabled": request.app.state.settings.google_workspace_directory_enabled,
        },
    )


@router.get("/new")
def new_person_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "people/new.html", {"employment_statuses": EMPLOYMENT_STATUSES}
    )


@router.post("")
def create_person(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    employment_status: str = Form("unknown"),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    normalized = normalize_email(email)
    if not normalized:
        return redirect_with_flash("/people/new", "Email is required.", kind="error")
    if employment_status not in EMPLOYMENT_STATUSES:
        employment_status = "unknown"

    person = Person(
        email=normalized,
        display_name=display_name.strip(),
        employment_status=employment_status,
        source="manual",
    )
    db.add(person)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect_with_flash(
            "/people/new", f"A person with email '{normalized}' already exists.", kind="error"
        )

    record_audit_event(
        db,
        entity_type="person",
        entity_id=person.id,
        action="create",
        detail=f"Added person '{normalized}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/people/{person.id}", "Person added.")


@router.post("/sync-workspace-directory")
def sync_workspace_directory(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    """Admin-only optional sync — updates Person records from Google
    Workspace Directory. Never deletes; a Person missing from this sync
    is simply not touched. Manual People records remain fully usable
    whether or not this is configured or ever run."""
    settings = request.app.state.settings
    if not settings.google_workspace_directory_enabled:
        return redirect_with_flash("/people", "Workspace Directory sync is not enabled.", kind="error")

    try:
        connection, access_token = get_access_token_for_active_connection(db, settings)
        directory_users = fetch_directory_users(access_token=access_token)
    except (GoogleDriveError, DirectorySyncError) as exc:
        return redirect_with_flash("/people", str(exc), kind="error")

    result = sync_directory_users(db, directory_users)
    record_audit_event(
        db,
        entity_type="google_drive_connection",
        entity_id=connection.id,
        action="sync_workspace_directory",
        detail=f"Workspace Directory sync: {result['created']} created, {result['updated']} updated",
        actor=admin.email,
    )
    return redirect_with_flash(
        "/people",
        f"Synced {result['total']} directory user(s): {result['created']} new, {result['updated']} updated.",
    )


@router.get("/{person_id}")
def view_person(person_id: str, request: Request, db: Session = Depends(get_db)):
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "people/detail.html", {"person": person, "employment_statuses": EMPLOYMENT_STATUSES}
    )


@router.post("/{person_id}/edit")
def update_person(
    person_id: str,
    request: Request,
    display_name: str = Form(""),
    employment_status: str = Form("unknown"),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    if employment_status not in EMPLOYMENT_STATUSES:
        return redirect_with_flash(f"/people/{person_id}", "Invalid employment status.", kind="error")

    before = {"display_name": person.display_name, "employment_status": person.employment_status}
    person.display_name = display_name.strip()
    person.employment_status = employment_status
    after = {"display_name": person.display_name, "employment_status": person.employment_status}

    record_audit_event(
        db,
        entity_type="person",
        entity_id=person.id,
        action="update",
        detail=f"Updated person '{person.email}': before={before} after={after}",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/people/{person_id}", "Person updated.")
