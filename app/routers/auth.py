from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.config import get_settings
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
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


@router.get("/login")
def login_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {})


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

    settings = get_settings()
    raw_token = new_session_token()
    expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=settings.session_ttl_hours)
    session_row = UserSession(
        user_id=user.id, token_hash=hash_session_token(raw_token), expires_at=expires_at
    )
    db.add(session_row)
    db.flush()
    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="login",
        detail="User signed in",
        actor=user.email,
    )

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
