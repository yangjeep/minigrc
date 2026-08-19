"""Generic OIDC login routes — /auth/oidc/login and /auth/oidc/callback
(issue #17).

Disabled (404) unless a usable configuration exists (Admin > Authentication
> Generic OIDC, or the legacy GRC_OIDC_* env vars — see
app/oidc_config.py). Local email/password login (app/routers/auth.py) and
the separate Google-specific flow (app/routers/google_oidc.py) both remain
available as break-glass/alternate paths regardless of whether this is
enabled.

Identity linking is deterministic by (issuer, subject) via
`ExternalIdentity` — deliberately stricter than the existing Google flow:
a first-time (issuer, subject) whose asserted email matches an existing
local user is rejected, never silently linked (see design doc §2.2).
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
from app.models import ExternalIdentity, User
from app.oidc import (
    OidcError,
    OidcIdentity,
    build_authorization_url,
    discover_provider_metadata,
    exchange_code_for_id_token,
    new_nonce,
    new_state,
    verify_identity,
)
from app.oidc_config import ResolvedOidcConfig, resolve_oidc_config
from app.oidc_role_mapping import apply_role_mapping
from app.routers.auth import start_user_session
from app.security import normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oidc", tags=["auth"])

STATE_COOKIE_NAME = "oidc_state"
NONCE_COOKIE_NAME = "oidc_nonce"
OAUTH_COOKIE_MAX_AGE_SECONDS = 600

DISABLED_MESSAGE = "Your account is no longer active. Contact an administrator."
PENDING_MESSAGE = "Your account is awaiting administrator approval."
UNAPPROVED_MESSAGE = "Your account was created but needs administrator approval before you can sign in."
COLLISION_MESSAGE = (
    "This email is already associated with a different account. Contact an administrator to link "
    "your single sign-on identity."
)


def _require_enabled(request: Request, db: Session) -> ResolvedOidcConfig:
    settings = request.app.state.settings
    resolved = resolve_oidc_config(db, settings)
    if not resolved.usable:
        raise HTTPException(status_code=404, detail="Generic OIDC sign-in is not configured")
    return resolved


@router.get("/login")
def oidc_login(request: Request, db: Session = Depends(get_db)):
    resolved = _require_enabled(request, db)
    settings = request.app.state.settings

    try:
        metadata = discover_provider_metadata(resolved.issuer)
    except OidcError as exc:
        logger.warning("oidc discovery failed for issuer=%s: %s", resolved.issuer, exc)
        return redirect_with_flash("/login", "Single sign-on is temporarily unavailable.", kind="error")

    state = new_state()
    nonce = new_nonce()
    authorization_url = build_authorization_url(
        metadata,
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
    db: Session, resolved: ResolvedOidcConfig, identity: OidcIdentity
) -> tuple[User | None, str | None]:
    """Match/create the User for a verified generic-OIDC identity.

    Returns (user, rejection_message). user is None iff rejected.
    """
    existing_identity = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.issuer == identity.issuer, ExternalIdentity.subject == identity.subject
        )
    )
    if existing_identity is not None:
        return db.get(User, existing_identity.user_id), None

    normalized_email = normalize_email(identity.email)
    email_owner = db.scalar(select(User).where(User.email == normalized_email))
    if email_owner is not None:
        logger.info("oidc first-login email collision for %s", normalized_email)
        return None, COLLISION_MESSAGE

    is_first_user = db.scalar(select(func.count()).select_from(User)) == 0
    status = "active" if resolved.auto_provision_enabled else "pending"
    if is_first_user and resolved.auto_provision_enabled:
        # Bootstrap admin: always locally managed, independent of any
        # OIDC group mapping — a fresh deployment must always have at
        # least one admin whose access can't be revoked by an IdP claim.
        role, role_source = "admin", "local"
    else:
        # Owned by the OIDC login flow from the start (issue #18) — a
        # no-op today if no OidcRoleMapping row exists yet, but takes
        # effect on this user's *next* login the moment an admin adds
        # one, with no separate backfill needed.
        role, role_source = "operator", "oidc_mapped"
    user = User(
        email=normalized_email,
        password_hash="",  # SSO-only account; local login stays unusable until a password is set
        role=role,
        role_source=role_source,
        status=status,
    )
    db.add(user)
    db.flush()
    db.add(ExternalIdentity(user_id=user.id, issuer=identity.issuer, subject=identity.subject))
    db.flush()

    if resolved.auto_provision_enabled:
        record_audit_event(
            db,
            entity_type="user",
            entity_id=user.id,
            action="create_via_oidc",
            detail=f"Created user '{normalized_email}' via generic OIDC sign-in ({identity.issuer})",
            actor="system",
        )
        return user, None

    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="create_via_oidc_pending",
        detail=(
            f"Created pending user '{normalized_email}' via generic OIDC sign-in "
            f"({identity.issuer}) — awaiting admin approval"
        ),
        actor="system",
    )
    return None, UNAPPROVED_MESSAGE


@router.get("/callback")
def oidc_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    resolved = _require_enabled(request, db)
    settings = request.app.state.settings

    if error:
        logger.info("oidc callback returned error=%s", error)
        return redirect_with_flash("/login", "Single sign-on was cancelled or failed.", kind="error")

    cookie_state = request.cookies.get(STATE_COOKIE_NAME)
    cookie_nonce = request.cookies.get(NONCE_COOKIE_NAME)
    if not cookie_state or not state or not secrets.compare_digest(cookie_state, state):
        return redirect_with_flash("/login", "Invalid sign-in session (state mismatch).", kind="error")
    if not code:
        return redirect_with_flash(
            "/login", "The OIDC provider did not return an authorization code.", kind="error"
        )

    try:
        metadata = discover_provider_metadata(resolved.issuer)
        raw_id_token = exchange_code_for_id_token(
            metadata,
            code=code,
            client_id=resolved.client_id,
            client_secret=resolved.client_secret,
            redirect_uri=resolved.redirect_uri,
        )
        identity = verify_identity(
            raw_id_token,
            metadata=metadata,
            client_id=resolved.client_id,
            expected_nonce=cookie_nonce or "",
            allowed_domains=resolved.allowed_domains,
        )
    except OidcError as exc:
        logger.info("oidc sign-in rejected: %s", exc)
        return redirect_with_flash("/login", str(exc), kind="error")

    user, rejection = _resolve_user(db, resolved, identity)
    if user is None:
        return redirect_with_flash("/login", rejection or "Sign-in was rejected.", kind="error")

    if user.status == "disabled":
        return redirect_with_flash("/login", DISABLED_MESSAGE, kind="error")
    if user.status == "pending":
        return redirect_with_flash("/login", PENDING_MESSAGE, kind="error")

    apply_role_mapping(db, user, identity.claims, role_claim_name=resolved.role_claim_name, actor="system")

    record_audit_event(
        db,
        entity_type="user",
        entity_id=user.id,
        action="login_oidc",
        detail=f"User signed in via generic OIDC ({identity.issuer})",
        actor=user.email,
    )

    response = start_user_session(db, user, settings)
    response.delete_cookie(STATE_COOKIE_NAME)
    response.delete_cookie(NONCE_COOKIE_NAME)
    return response
