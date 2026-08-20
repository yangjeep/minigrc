"""Admin > AI > BYOK Provider settings (issue #15).

Same masked-secret, blank-submission-keeps-existing-value shape as
app/routers/admin_authentication.py's Google/OIDC settings pages. The API
key is never redisplayed after save; `validate_provider_base_url` runs
server-side before persisting so an invalid/SSRF-risky endpoint is
rejected rather than stored.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_provider import InvalidProviderUrlError, resolve_ai_provider_config, validate_provider_base_url
from app.audit import record_audit_event
from app.deps import get_db, require_admin, verify_csrf
from app.flash import redirect_with_flash
from app.models import AiProviderSettings, User, new_id
from app.secrets import create_encrypted_secret

router = APIRouter(prefix="/admin/ai", tags=["admin"], dependencies=[Depends(require_admin)])


def _current_row(db: Session) -> AiProviderSettings | None:
    return db.scalar(select(AiProviderSettings).order_by(AiProviderSettings.updated_at.desc()).limit(1))


@router.get("/settings")
def ai_settings_form(request: Request, db: Session = Depends(get_db)):
    row = _current_row(db)
    settings = request.app.state.settings
    resolved = resolve_ai_provider_config(db, encryption_key=settings.encryption_key)
    has_secret = bool(row and row.secret_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin/ai/settings.html",
        {"row": row, "resolved": resolved, "has_secret": has_secret},
    )


@router.post("/settings")
def update_ai_settings(
    request: Request,
    enabled: bool = Form(False),
    display_name: str = Form(""),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    timeout_seconds: int = Form(30),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    base_url = base_url.strip()
    if enabled and base_url:
        try:
            validate_provider_base_url(base_url)
        except InvalidProviderUrlError as exc:
            return redirect_with_flash("/admin/ai/settings", str(exc), kind="error")

    row = _current_row(db)
    secret_id = row.secret_id if row is not None else None
    if api_key.strip():
        key = request.app.state.settings.encryption_key
        secret = create_encrypted_secret(
            db,
            name=f"ai_provider_api_key:{new_id()}",
            plaintext=api_key.strip(),
            actor=admin.email,
            key=key,
        )
        secret_id = secret.id

    if row is None:
        row = AiProviderSettings(updated_by=admin.email)
        db.add(row)

    row.enabled = enabled
    row.display_name = display_name.strip()
    row.base_url = base_url
    row.model = model.strip()
    row.secret_id = secret_id
    row.timeout_seconds = max(1, timeout_seconds)
    row.updated_by = admin.email
    db.flush()
    record_audit_event(
        db,
        entity_type="ai_provider_settings",
        entity_id=row.id,
        action="update",
        detail=f"Set enabled={row.enabled} display_name='{row.display_name}' model='{row.model}'",
        actor=admin.email,
    )
    return redirect_with_flash("/admin/ai/settings", "AI provider settings saved.")
