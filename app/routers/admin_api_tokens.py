"""Admin > API Tokens: issuance/revocation UI for issue #36's read-only
MCP surface.

The one-time plaintext reveal after creation is rendered directly on the
POST response — never via `app.flash.redirect_with_flash`, which carries
its message in the redirect query string (browser history, `Referer`
header, reverse-proxy access logs). A token must never travel through
that path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api_tokens import create_api_token, revoke_api_token
from app.deps import get_db, require_admin, verify_csrf
from app.flash import redirect_with_flash
from app.models import ApiToken, User

router = APIRouter(prefix="/admin/api-tokens", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("")
def list_api_tokens(request: Request, db: Session = Depends(get_db)):
    tokens = db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/api_tokens/list.html", {"tokens": tokens})


@router.get("/new")
def new_api_token_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/api_tokens/new.html", {})


@router.post("")
def create_api_token_route(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    if not name.strip():
        return redirect_with_flash("/admin/api-tokens/new", "Name is required.", kind="error")

    created = create_api_token(db, name=name.strip(), actor=admin)
    db.flush()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin/api_tokens/created.html",
        {"token": created.token, "plaintext": created.plaintext},
    )


@router.post("/{token_id}/revoke")
def revoke_api_token_route(
    token_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="API token not found")

    was_revoked = revoke_api_token(db, token, actor=admin)
    db.flush()
    message = f"Revoked '{token.name}'." if was_revoked else f"'{token.name}' was already revoked."
    return redirect_with_flash("/admin/api-tokens", message)
