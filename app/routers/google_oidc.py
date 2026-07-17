"""Google OIDC login routes — /auth/google/login and /auth/google/callback.

Disabled (404) unless GRC_GOOGLE_OIDC_CLIENT_ID/SECRET and
GRC_PUBLIC_BASE_URL are all configured. Local email/password login
(app/routers/auth.py) remains available as a break-glass fallback
regardless of whether this is enabled.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db
from app.flash import redirect_with_flash
from app.google_oidc import (
    GoogleOidcError,
    build_authorization_url,
    exchange_code_for_id_token,
    new_nonce,
    new_state,
    verify_identity,
)
from app.models import Person, User
from app.routers.auth import start_user_session
from app.security import normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google", tags=["auth"])

STATE_COOKIE_NAME = "google_oidc_state"
NONCE_COOKIE_NAME = "google_oidc_nonce"
OAUTH_COOKIE_MAX_AGE_SECONDS = 600


def _require_enabled(request: Request):
    settings = request.app.state.settings
    if not settings.google_oidc_enabled:
        raise HTTPException(status_code=404, detail="Google sign-in is not configured")
    return settings


@router.get("/login")
def google_login(request: Request):
    settings = _require_enabled(request)

    state = new_state()
    nonce = new_nonce()
    authorization_url = build_authorization_url(
        client_id=settings.google_oidc_client_id,
        redirect_uri=settings.google_oidc_redirect_uri,
        state=state,
        nonce=nonce,
    )

    response = RedirectResponse(url=authorization_url, status_code=303)
    for name, value in ((STATE_COOKIE_NAME, state), (NONCE_COOKIE_NAME, nonce)):
        response.set_cookie(
            name,
            value,
            httponly=True,
            samesite="lax",
            secure=settings.session_cookie_secure,
            max_age=OAUTH_COOKIE_MAX_AGE_SECONDS,
        )
    return response


@router.get("/callback")
def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    settings = _require_enabled(request)

    if error:
        logger.info("google oidc callback returned error=%s", error)
        return redirect_with_flash("/login", "Google sign-in was cancelled or failed.", kind="error")

    cookie_state = request.cookies.get(STATE_COOKIE_NAME)
    cookie_nonce = request.cookies.get(NONCE_COOKIE_NAME)
    if not cookie_state or not state or not secrets.compare_digest(cookie_state, state):
        return redirect_with_flash("/login", "Invalid sign-in session (state mismatch).", kind="error")
    if not code:
        return redirect_with_flash("/login", "Google did not return an authorization code.", kind="error")

    try:
        raw_id_token = exchange_code_for_id_token(
            code=code,
            client_id=settings.google_oidc_client_id,
            client_secret=settings.google_oidc_client_secret,
            redirect_uri=settings.google_oidc_redirect_uri,
        )
        identity = verify_identity(
            raw_id_token,
            client_id=settings.google_oidc_client_id,
            expected_nonce=cookie_nonce or "",
            allowed_domains=settings.google_oidc_allowed_domains_set,
        )
    except GoogleOidcError as exc:
        logger.info("google oidc sign-in rejected: %s", exc)
        return redirect_with_flash("/login", str(exc), kind="error")

    normalized_email = normalize_email(identity.email)
    user = db.scalar(select(User).where(User.email == normalized_email))

    if user is None:
        person = db.scalar(select(Person).where(Person.email == normalized_email))
        is_first_user = db.scalar(select(func.count()).select_from(User)) == 0
        user = User(
            email=normalized_email,
            password_hash="",  # Google-only account; local login stays unusable until a password is set
            role="admin" if is_first_user else "user",
            person_id=person.id if person is not None else None,
        )
        db.add(user)
        db.flush()
        record_audit_event(
            db,
            entity_type="user",
            entity_id=user.id,
            action="create_via_google_oidc",
            detail=f"Created user '{normalized_email}' via Google sign-in",
            actor="system",
        )

    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="login_google_oidc",
        detail="User signed in via Google OIDC",
        actor=user.email,
    )

    response = start_user_session(db, user, settings)
    response.delete_cookie(STATE_COOKIE_NAME)
    response.delete_cookie(NONCE_COOKIE_NAME)
    return response
