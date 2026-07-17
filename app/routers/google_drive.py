"""Google Drive org-level connection: admin-only connect/disconnect.

Distinct OAuth flow from Google OIDC login (app/routers/google_oidc.py),
even when pointed at the same Google Cloud project's client credentials —
see docs/decisions/architectural-decisions.md.
"""

from __future__ import annotations

import datetime
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.crypto import DecryptionError, EncryptionNotConfiguredError, decrypt, encrypt
from app.deps import get_db, require_admin, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.google_drive import (
    GoogleDriveError,
    build_authorization_url,
    exchange_code_for_tokens,
    get_access_token,
    revoke_token,
)
from app.models import GoogleDriveConnection

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/connectors/google-drive", tags=["connectors"], dependencies=[Depends(require_login)]
)

STATE_COOKIE_NAME = "google_drive_oauth_state"
OAUTH_COOKIE_MAX_AGE_SECONDS = 600


def _require_configured(request: Request):
    settings = request.app.state.settings
    if not settings.google_drive_configured:
        raise HTTPException(status_code=404, detail="Google Drive connector is not configured")
    return settings


def active_connection(db: Session) -> GoogleDriveConnection | None:
    return db.scalar(
        select(GoogleDriveConnection)
        .where(GoogleDriveConnection.revoked_at.is_(None))
        .order_by(GoogleDriveConnection.connected_at.desc())
        .limit(1)
    )


def get_access_token_for_active_connection(db: Session, settings) -> tuple[GoogleDriveConnection, str]:
    """Shared by policy Drive-link/capture routes: resolve the active
    connection and a fresh access token, or raise GoogleDriveError."""
    connection = active_connection(db)
    if connection is None:
        raise GoogleDriveError("Connect Google Drive first (Connectors > Google Drive).")
    try:
        refresh_token = decrypt(connection.encrypted_refresh_token, key=settings.encryption_key)
    except (DecryptionError, EncryptionNotConfiguredError) as exc:
        raise GoogleDriveError(str(exc)) from exc
    access_token = get_access_token(
        refresh_token=refresh_token,
        client_id=settings.google_drive_client_id,
        client_secret=settings.google_drive_client_secret,
    )
    return connection, access_token


@router.get("")
def view_connection(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    connection = active_connection(db) if settings.google_drive_configured else None
    history = list(
        db.scalars(select(GoogleDriveConnection).order_by(GoogleDriveConnection.connected_at.desc())).all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "connectors/google_drive.html",
        {"connection": connection, "configured": settings.google_drive_configured, "history": history},
    )


@router.get("/connect")
def connect(request: Request, _admin=Depends(require_admin)):
    settings = _require_configured(request)
    state = secrets.token_urlsafe(24)
    authorization_url = build_authorization_url(
        client_id=settings.google_drive_client_id,
        redirect_uri=settings.google_drive_redirect_uri,
        state=state,
    )
    response = RedirectResponse(url=authorization_url, status_code=303)
    response.set_cookie(
        STATE_COOKIE_NAME,
        state,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=OAUTH_COOKIE_MAX_AGE_SECONDS,
    )
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    settings = _require_configured(request)

    if error:
        logger.info("google drive oauth callback returned error=%s", error)
        return redirect_with_flash(
            "/connectors/google-drive", "Google Drive connection was cancelled or failed.", kind="error"
        )

    cookie_state = request.cookies.get(STATE_COOKIE_NAME)
    if not cookie_state or not state or not secrets.compare_digest(cookie_state, state):
        return redirect_with_flash(
            "/connectors/google-drive", "Invalid connection session (state mismatch).", kind="error"
        )
    if not code:
        return redirect_with_flash(
            "/connectors/google-drive", "Google did not return an authorization code.", kind="error"
        )

    try:
        tokens = exchange_code_for_tokens(
            code=code,
            client_id=settings.google_drive_client_id,
            client_secret=settings.google_drive_client_secret,
            redirect_uri=settings.google_drive_redirect_uri,
        )
        encrypted_refresh_token = encrypt(tokens["refresh_token"], key=settings.encryption_key)
    except (GoogleDriveError, EncryptionNotConfiguredError) as exc:
        logger.info("google drive connection failed: %s", exc)
        return redirect_with_flash("/connectors/google-drive", str(exc), kind="error")

    connection = GoogleDriveConnection(
        connected_by_user_id=admin.id,
        granted_scopes=tokens.get("scope", ""),
        encrypted_refresh_token=encrypted_refresh_token,
    )
    db.add(connection)
    db.flush()
    record_audit_event(
        db,
        entity_type="google_drive_connection",
        entity_id=connection.id,
        action="connect",
        detail=f"Connected Google Drive (scopes: {connection.granted_scopes})",
        actor=admin.email,
    )

    response = redirect_with_flash("/connectors/google-drive", "Google Drive connected.")
    response.delete_cookie(STATE_COOKIE_NAME)
    return response


@router.post("/disconnect")
def disconnect(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    connection = active_connection(db)
    if connection is None:
        return redirect_with_flash(
            "/connectors/google-drive", "No active Google Drive connection.", kind="error"
        )

    settings = request.app.state.settings
    try:
        plaintext_token = decrypt(connection.encrypted_refresh_token, key=settings.encryption_key)
        revoke_token(plaintext_token)
    except (DecryptionError, EncryptionNotConfiguredError):
        logger.warning(
            "could not decrypt stored token to revoke at Google; clearing local credential regardless"
        )

    # Erase the credential and stamp who/when disconnected — never delete
    # the row, so the connection's history stays visible. Historical
    # policy snapshots/evidence captured while connected are untouched.
    connection.encrypted_refresh_token = ""
    connection.revoked_at = datetime.datetime.now(datetime.UTC)
    connection.revoked_by_user_id = admin.id
    record_audit_event(
        db,
        entity_type="google_drive_connection",
        entity_id=connection.id,
        action="disconnect",
        detail="Disconnected Google Drive",
        actor=admin.email,
    )
    return redirect_with_flash("/connectors/google-drive", "Google Drive disconnected.")
