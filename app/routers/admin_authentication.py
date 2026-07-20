"""Admin > Authentication > Google OAuth: DB-backed login configuration.

The client secret is write-only after save (masked placeholder shown for
an already-configured secret; a blank submission keeps the existing
value — never blanks it out). The single most recently updated
GoogleOidcSettings row is the active configuration; see
app/google_oidc_config.py for how it's resolved against the legacy
env-var fallback.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_admin, verify_csrf
from app.flash import redirect_with_flash
from app.google_oidc_config import resolve_google_oidc_config
from app.models import GoogleOidcSettings, User
from app.secrets import create_encrypted_secret

router = APIRouter(prefix="/admin/authentication", tags=["admin"], dependencies=[Depends(require_admin)])


def _current_row(db: Session) -> GoogleOidcSettings | None:
    return db.scalar(select(GoogleOidcSettings).order_by(GoogleOidcSettings.updated_at.desc()).limit(1))


@router.get("/google")
def google_settings_form(request: Request, db: Session = Depends(get_db)):
    row = _current_row(db)
    resolved = resolve_google_oidc_config(db, request.app.state.settings)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin/authentication/google.html",
        {"row": row, "resolved": resolved, "has_secret": bool(row and row.secret_id)},
    )


@router.post("/google")
def update_google_settings(
    request: Request,
    enabled: bool = Form(False),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    allowed_domains: str = Form(""),
    auto_provision_enabled: bool = Form(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    row = _current_row(db)
    secret_id = row.secret_id if row is not None else None
    if client_secret.strip():
        key = request.app.state.settings.encryption_key
        secret = create_encrypted_secret(
            db, name="google_oidc_client_secret", plaintext=client_secret.strip(), actor=admin.email, key=key
        )
        secret_id = secret.id

    if row is None:
        row = GoogleOidcSettings(updated_by=admin.email)
        db.add(row)

    row.enabled = enabled
    row.client_id = client_id.strip()
    row.secret_id = secret_id
    row.allowed_domains = allowed_domains.strip()
    row.auto_provision_enabled = auto_provision_enabled
    row.updated_by = admin.email
    db.flush()
    record_audit_event(
        db,
        entity_type="google_oidc_settings",
        entity_id=row.id,
        action="update",
        detail=(
            f"Set enabled={row.enabled} auto_provision_enabled={row.auto_provision_enabled} "
            f"allowed_domains='{row.allowed_domains}'"
        ),
        actor=admin.email,
    )
    return redirect_with_flash("/admin/authentication/google", "Google OAuth settings saved.")
