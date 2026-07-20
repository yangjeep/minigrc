from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.config import Settings
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.google_oidc_config import resolve_google_oidc_config
from app.models import User, UserSession
from app.security import (
    SESSION_COOKIE_NAME,
    hash_session_token,
    new_session_token,
    normalize_email,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def start_user_session(db: Session, user: User, settings: Settings) -> RedirectResponse:
    """Create a server-side session row + cookie, and redirect to the dashboard.

    Shared by local email/password login and Google OIDC login — the two
    differ only in how they establish *who* the user is; once a `User` is
    known, session issuance is identical.
    """
    raw_token = new_session_token()
    expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=settings.session_ttl_hours)
    session_row = UserSession(
        user_id=user.id, token_hash=hash_session_token(raw_token), expires_at=expires_at
    )
    db.add(session_row)
    db.flush()

    response = redirect_with_flash("/", f"Signed in as {user.email}.")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
    )
    return response


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    templates = request.app.state.templates
    resolved = resolve_google_oidc_config(db, settings)
    return templates.TemplateResponse(request, "login.html", {"google_oidc_enabled": resolved.usable})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    normalized = normalize_email(email)
    user = db.scalar(select(User).where(User.email == normalized))

    if not verify_password(password, user.password_hash if user else None):
        logger.info("login failed for %s", normalized)
        return redirect_with_flash("/login", "Invalid email or password.", kind="error")

    if user.status != "active":
        logger.info("login rejected for %s: status=%s", normalized, user.status)
        return redirect_with_flash(
            "/login", "Your account is no longer active. Contact an administrator.", kind="error"
        )

    settings = request.app.state.settings
    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="login",
        detail="User signed in",
        actor=user.email,
    )
    return start_user_session(db, user, settings)


@router.post("/logout")
def logout(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    user_session: UserSession = request.state.user_session
    user_session.revoked_at = datetime.datetime.now(datetime.UTC)
    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="logout",
        detail="User signed out",
        actor=user.email,
    )

    response = redirect_with_flash("/login", "Signed out.")
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
