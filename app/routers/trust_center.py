"""Trust Center administration (Feature 11).

Admin-only end to end, matching the Connections precedent — publishing
content externally is a governance-sensitive action even though no
credential is involved. The public-facing, unauthenticated route is a
separate later feature; this router only ever renders behind
`require_admin`, including the Preview action (it approximates what a
visitor would see, it does not serve visitors).

Section list/create/delete/reorder go through the generic register
grid (title/visibility/display_order are simple scalar fields with no
special validation). Body content, review/expiry dates, and internal
record links are edited on a dedicated detail page instead of grid
cells — long-form Markdown and date parsing don't fit the grid's
plain-JSON PATCH contract (see app/registers/router.py's `_validate`,
which has no date-parsing step), and publish/unpublish are dedicated
actions rather than a raw `status` edit so a snapshot is always taken
correctly.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_admin
from app.flash import redirect_with_flash
from app.markdown_render import render_markdown_safe
from app.models import (
    TRUST_CENTER_SECTION_VISIBILITY,
    Framework,
    Policy,
    TrustCenterSection,
    User,
)
from app.registers.config import FieldSpec, RegisterConfig
from app.registers.router import build_register_router
from app.trust_center import get_or_create_settings, is_stale, publish_section, unpublish_section

router = APIRouter(prefix="/trust-center/admin", tags=["trust-center"], dependencies=[Depends(require_admin)])

SECTIONS_REGISTER_CONFIG = RegisterConfig(
    name="trust-center-sections",
    model=TrustCenterSection,
    entity_type="trust_center_section",
    order_by=TrustCenterSection.display_order,
    require_admin_for=frozenset({"list", "create", "edit", "delete"}),
    fields=(
        FieldSpec(name="title", type="text", required=True, max_length=255),
        FieldSpec(name="visibility", type="enum", choices=TRUST_CENTER_SECTION_VISIBILITY),
        FieldSpec(name="display_order", type="number"),
        FieldSpec(name="status", type="text", read_only=True),
        FieldSpec(
            name="linked_framework_name",
            type="text",
            read_only=True,
            compute=lambda s: s.linked_framework.name if s.linked_framework else "",
        ),
        FieldSpec(
            name="linked_policy_title",
            type="text",
            read_only=True,
            compute=lambda s: s.linked_policy.title if s.linked_policy else "",
        ),
        FieldSpec(name="stale", type="bool", read_only=True, compute=lambda s: is_stale(s)),
    ),
)

sections_register_router = build_register_router(SECTIONS_REGISTER_CONFIG)


def _parse_date(value: str) -> datetime.date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


@router.get("")
def admin_home(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "trust_center/admin.html",
        {"settings": settings, "visibility_values": TRUST_CENTER_SECTION_VISIBILITY},
    )


@router.post("/settings")
def update_settings(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
    enabled: bool = Form(False),
    title: str = Form(...),
    intro_markdown: str = Form(""),
    contact_email: str = Form(""),
    support_url: str = Form(""),
):
    settings = get_or_create_settings(db)
    settings.enabled = enabled
    settings.title = title
    settings.intro_markdown = intro_markdown
    settings.contact_email = contact_email
    settings.support_url = support_url
    settings.updated_by_user_id = user.id
    db.flush()
    record_audit_event(
        db,
        entity_type="trust_center_settings",
        entity_id=settings.id,
        action="update",
        detail=f"Updated Trust Center settings (enabled={enabled})",
        actor=user.email,
    )
    return redirect_with_flash("/trust-center/admin", "Trust Center settings updated.")


def _get_section_or_404(db: Session, section_id: str) -> TrustCenterSection:
    section = db.get(TrustCenterSection, section_id)
    if section is None:
        raise HTTPException(status_code=404, detail="Trust Center section not found")
    return section


@router.get("/sections/{section_id}")
def section_detail(section_id: str, request: Request, db: Session = Depends(get_db)):
    section = _get_section_or_404(db, section_id)
    frameworks = db.scalars(select(Framework).order_by(Framework.name)).all()
    policies = db.scalars(select(Policy).order_by(Policy.title)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "trust_center/section_detail.html",
        {
            "section": section,
            "frameworks": frameworks,
            "policies": policies,
            "is_stale": is_stale(section),
        },
    )


@router.post("/sections/{section_id}")
def update_section(
    section_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
    draft_body_markdown: str = Form(""),
    review_date: str = Form(""),
    expiry_date: str = Form(""),
    linked_framework_id: str = Form(""),
    linked_policy_id: str = Form(""),
):
    section = _get_section_or_404(db, section_id)
    section.draft_body_markdown = draft_body_markdown
    section.review_date = _parse_date(review_date)
    section.expiry_date = _parse_date(expiry_date)
    section.linked_framework_id = linked_framework_id or None
    section.linked_policy_id = linked_policy_id or None
    db.flush()
    record_audit_event(
        db,
        entity_type="trust_center_section",
        entity_id=section.id,
        action="update",
        detail=f"Updated draft content for Trust Center section '{section.title}'",
        actor=user.email,
    )
    return redirect_with_flash(f"/trust-center/admin/sections/{section_id}", "Section updated.")


@router.get("/sections/{section_id}/preview")
def preview_section(section_id: str, request: Request, db: Session = Depends(get_db)):
    section = _get_section_or_404(db, section_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "trust_center/section_preview.html",
        {"section": section, "rendered_html": render_markdown_safe(section.draft_body_markdown)},
    )


@router.post("/sections/{section_id}/publish")
def publish_section_route(
    section_id: str, db: Session = Depends(get_db), user: User = Depends(require_admin)
):
    section = _get_section_or_404(db, section_id)
    publish_section(db, section, actor=user)
    return redirect_with_flash(f"/trust-center/admin/sections/{section_id}", "Section published.")


@router.post("/sections/{section_id}/unpublish")
def unpublish_section_route(
    section_id: str, db: Session = Depends(get_db), user: User = Depends(require_admin)
):
    section = _get_section_or_404(db, section_id)
    unpublish_section(db, section, actor=user)
    return redirect_with_flash(f"/trust-center/admin/sections/{section_id}", "Section unpublished.")
