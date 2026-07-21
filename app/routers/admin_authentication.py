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
from app.models import GoogleOidcSettings, User, new_id
from app.secrets import create_encrypted_secret

router = APIRouter(prefix="/admin/authentication", tags=["admin"], dependencies=[Depends(require_admin)])


def _current_row(db: Session) -> GoogleOidcSettings | None:
    return db.scalar(select(GoogleOidcSettings).order_by(GoogleOidcSettings.updated_at.desc()).limit(1))


def _not_usable_reason(
    row: GoogleOidcSettings | None, resolved, has_secret: bool, public_base_url: str
) -> str | None:
    """Explain why the form's own fields don't already show why: `usable`
    also depends on GRC_PUBLIC_BASE_URL (an env var, deliberately not a
    form field — see app/google_oidc_config.py) and on the stored secret
    actually decrypting, neither of which is otherwise visible here."""
    if resolved.usable or row is None or not row.enabled:
        return None
    if not row.client_id:
        return "Client ID is required."
    if not has_secret:
        return "Client secret is required."
    if not public_base_url:
        return (
            "GRC_PUBLIC_BASE_URL is not set in this deployment's environment — "
            "required to compute the OAuth redirect URI."
        )
    return "Stored client secret could not be decrypted — check GRC_ENCRYPTION_KEY, then re-enter it."


@router.get("/google")
def google_settings_form(request: Request, db: Session = Depends(get_db)):
    row = _current_row(db)
    settings = request.app.state.settings
    resolved = resolve_google_oidc_config(db, settings)
    has_secret = bool(row and row.secret_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin/authentication/google.html",
        {
            "row": row,
            "resolved": resolved,
            "has_secret": has_secret,
            "not_usable_reason": _not_usable_reason(row, resolved, has_secret, settings.public_base_url),
        },
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
            db,
            name=f"google_oidc_client_secret:{new_id()}",
            plaintext=client_secret.strip(),
            actor=admin.email,
            key=key,
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
