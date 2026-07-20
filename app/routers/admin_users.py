"""Admin > People > Users: the only user-management UI this app has.

Previously CLI-only (`python -m app.cli create-user`/`promote-admin`).
Role and status changes go through dedicated routes rather than the
generic register-grid PATCH endpoint so this module can enforce
user-specific safety rules (no self-lockout, no zero-admin state) that a
generic register has no concept of. The list view still reuses
register-grid for consistent rendering.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_admin, verify_csrf
from app.flash import redirect_with_flash
from app.models import USER_ROLES, USER_STATUSES, User
from app.registers.config import FieldSpec, RegisterConfig
from app.registers.router import build_register_router
from app.security import normalize_email

router = APIRouter(prefix="/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])

USERS_REGISTER_CONFIG = RegisterConfig(
    name="admin_users",
    model=User,
    entity_type="user",
    order_by=User.email,
    creatable=False,
    deletable=False,
    bulk_enabled=False,
    require_admin_for=frozenset({"list"}),
    fields=(
        FieldSpec(name="email", type="text", read_only=True),
        FieldSpec(name="role", type="text", read_only=True),
        FieldSpec(name="status", type="text", read_only=True),
        FieldSpec(
            name="created_at",
            type="text",
            read_only=True,
            compute=lambda u: u.created_at.isoformat() if u.created_at else None,
        ),
        FieldSpec(
            name="google_linked", type="bool", read_only=True, compute=lambda u: bool(u.google_subject)
        ),
    ),
)

users_register_router = build_register_router(USERS_REGISTER_CONFIG)


def _active_admin_count(db: Session, *, excluding_user_id: str | None = None) -> int:
    query = select(func.count()).select_from(User).where(User.role == "admin", User.status == "active")
    if excluding_user_id is not None:
        query = query.where(User.id != excluding_user_id)
    return db.scalar(query) or 0


@router.get("")
def list_users(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/users/list.html", {})


@router.get("/new")
def new_user_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/users/new.html", {})


@router.post("")
def create_user(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    normalized = normalize_email(email)
    if db.scalar(select(User).where(User.email == normalized)) is not None:
        return redirect_with_flash("/admin/users/new", "A user with that email already exists.", kind="error")

    user = User(email=normalized, password_hash="", role="user", status="active")
    db.add(user)
    db.flush()
    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="create_via_admin",
        detail=f"Added user '{normalized}' (sign in with Google or an admin-set password)",
        actor=admin.email,
    )
    return redirect_with_flash("/admin/users", f"Added {normalized}.")


@router.get("/{user_id}/edit")
def edit_user_form(user_id: str, request: Request, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin/users/edit.html",
        {"user_row": user, "roles": USER_ROLES, "statuses": USER_STATUSES},
    )


@router.post("/{user_id}/edit")
def update_user(
    user_id: str,
    request: Request,
    role: str = Form(...),
    status: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if role not in USER_ROLES or status not in USER_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid role or status")

    edit_url = f"/admin/users/{user_id}/edit"
    is_self = user.id == admin.id
    was_active_admin = user.role == "admin" and user.status == "active"
    remains_active_admin = role == "admin" and status == "active"

    if is_self and status != "active":
        return redirect_with_flash(edit_url, "You cannot disable your own account.", kind="error")
    if (
        was_active_admin
        and not remains_active_admin
        and _active_admin_count(db, excluding_user_id=user.id) == 0
    ):
        return redirect_with_flash(
            edit_url, "At least one active admin must remain — promote another user first.", kind="error"
        )

    user.role = role
    user.status = status
    db.flush()
    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="update_role_status",
        detail=f"Set role={role} status={status} for '{user.email}'",
        actor=admin.email,
    )
    return redirect_with_flash("/admin/users", f"Updated {user.email}.")
