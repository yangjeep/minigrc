"""Google OIDC login routes — /auth/google/login and /auth/google/callback.

Disabled (404) unless a usable configuration exists (Admin > Authentication
> Google OAuth, or the legacy GRC_GOOGLE_OIDC_* env vars — see
app/google_oidc_config.py). Local email/password login
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
from app.google_oidc_config import ResolvedGoogleOidcConfig, resolve_google_oidc_config
from app.models import Person, User
from app.routers.auth import start_user_session
from app.security import normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google", tags=["auth"])

STATE_COOKIE_NAME = "google_oidc_state"
NONCE_COOKIE_NAME = "google_oidc_nonce"
OAUTH_COOKIE_MAX_AGE_SECONDS = 600

DISABLED_MESSAGE = "Your account is no longer active. Contact an administrator."
PENDING_MESSAGE = "Your account is awaiting administrator approval."
UNAPPROVED_MESSAGE = "Your account was created but needs administrator approval before you can sign in."
COLLISION_MESSAGE = "This email is already linked to a different Google account. Contact an administrator."


def _require_enabled(request: Request, db: Session) -> ResolvedGoogleOidcConfig:
    settings = request.app.state.settings
    resolved = resolve_google_oidc_config(db, settings)
    if not resolved.usable:
        raise HTTPException(status_code=404, detail="Google sign-in is not configured")
    return resolved


@router.get("/login")
def google_login(request: Request, db: Session = Depends(get_db)):
    resolved = _require_enabled(request, db)
    settings = request.app.state.settings

    state = new_state()
    nonce = new_nonce()
    authorization_url = build_authorization_url(
        client_id=resolved.client_id,
        redirect_uri=resolved.redirect_uri,
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


def _resolve_user(
    db: Session, resolved: ResolvedGoogleOidcConfig, identity
) -> tuple[User | None, str | None]:
    """Match/create the User for a verified Google identity.

    Returns (user, rejection_message). user is None iff rejected.
    """
    normalized_email = normalize_email(identity.email)

    user = db.scalar(select(User).where(User.google_subject == identity.subject))
    if user is not None:
        if user.email != normalized_email:
            email_owner = db.scalar(select(User).where(User.email == normalized_email))
            if email_owner is not None and email_owner.id != user.id:
                logger.info("google oidc email-change collision for %s", normalized_email)
                return None, COLLISION_MESSAGE
            old_email = user.email
            user.email = normalized_email
            db.flush()
            record_audit_event(
                db,
                entity_type="user",
                entity_id=user.id,
                action="google_identity_email_changed",
                detail=f"Email changed from '{old_email}' to '{normalized_email}' via Google sign-in",
                actor=normalized_email,
            )
        return user, None

    existing_by_email = db.scalar(select(User).where(User.email == normalized_email))
    if existing_by_email is not None:
        if existing_by_email.google_subject and existing_by_email.google_subject != identity.subject:
            logger.info("google oidc identity collision for %s", normalized_email)
            return None, COLLISION_MESSAGE
        existing_by_email.google_subject = identity.subject
        db.flush()
        record_audit_event(
            db,
            entity_type="user",
            entity_id=existing_by_email.id,
            action="link_google_identity",
            detail=f"Linked Google identity to existing user '{normalized_email}'",
            actor=normalized_email,
        )
        return existing_by_email, None

    person = db.scalar(select(Person).where(Person.email == normalized_email))
    is_first_user = db.scalar(select(func.count()).select_from(User)) == 0
    status = "active" if resolved.auto_provision_enabled else "pending"
    role = "admin" if (is_first_user and resolved.auto_provision_enabled) else "user"
    user = User(
        email=normalized_email,
        password_hash="",  # Google-only account; local login stays unusable until a password is set
        role=role,
        status=status,
        google_subject=identity.subject,
        person_id=person.id if person is not None else None,
    )
    db.add(user)
    db.flush()
    if resolved.auto_provision_enabled:
        record_audit_event(
            db,
            entity_type="user",
            entity_id=user.id,
            action="create_via_google_oidc",
            detail=f"Created user '{normalized_email}' via Google sign-in",
            actor="system",
        )
        return user, None

    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="create_via_google_oidc_pending",
        detail=f"Created pending user '{normalized_email}' via Google sign-in — awaiting admin approval",
        actor="system",
    )
    return None, UNAPPROVED_MESSAGE


@router.get("/callback")
def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    resolved = _require_enabled(request, db)
    settings = request.app.state.settings

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
            client_id=resolved.client_id,
            client_secret=resolved.client_secret,
            redirect_uri=resolved.redirect_uri,
        )
        identity = verify_identity(
            raw_id_token,
            client_id=resolved.client_id,
            expected_nonce=cookie_nonce or "",
            allowed_domains=resolved.allowed_domains,
        )
    except GoogleOidcError as exc:
        logger.info("google oidc sign-in rejected: %s", exc)
        return redirect_with_flash("/login", str(exc), kind="error")

    user, rejection = _resolve_user(db, resolved, identity)
    if user is None:
        return redirect_with_flash("/login", rejection or "Sign-in was rejected.", kind="error")

    if user.status == "disabled":
        return redirect_with_flash("/login", DISABLED_MESSAGE, kind="error")
    if user.status == "pending":
        return redirect_with_flash("/login", PENDING_MESSAGE, kind="error")

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
