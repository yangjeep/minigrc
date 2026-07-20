"""Public, unauthenticated Trust Center route (Feature 12).

Deliberately separate from app/routers/trust_center.py (the admin
router) and from the authenticated Bootstrap shell — this renders a
standalone template (trust_center/public.html), not base.html, so no
internal nav/sidebar ever reaches an unauthenticated visitor.

Only ever reads: `TrustCenterSettings.enabled`, sections where
`visibility == "public"` and `status == "published"` (their
`published_body_markdown` snapshot, never `draft_body_markdown`), and
policy downloads gated on both the policy being `status == "approved"`
and currently linked from at least one public+published section —
this is the boundary that keeps `Policy`/`PolicyVersion` ids from
becoming an arbitrary-access surface just by being guessable UUIDs.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db
from app.markdown_render import render_markdown_safe
from app.models import Policy, TrustCenterSection
from app.storage import policy_version_path
from app.trust_center import get_or_create_settings

router = APIRouter(tags=["trust-center-public"])


def _public_published_sections(db: Session) -> list[TrustCenterSection]:
    return list(
        db.scalars(
            select(TrustCenterSection)
            .where(TrustCenterSection.visibility == "public")
            .where(TrustCenterSection.status == "published")
            .order_by(TrustCenterSection.display_order)
        ).all()
    )


@router.get("/trust-center")
def public_trust_center(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Not found")

    sections = _public_published_sections(db)
    rendered_sections = [
        {
            "id": section.id,
            "title": section.title,
            "html": render_markdown_safe(section.published_body_markdown),
            "review_date": section.review_date,
            "expiry_date": section.expiry_date,
            "linked_policy": (
                section.linked_policy
                if section.linked_policy is not None and section.linked_policy.status == "approved"
                else None
            ),
        }
        for section in sections
    ]

    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "trust_center/public.html",
        {
            "settings": settings,
            "intro_html": render_markdown_safe(settings.intro_markdown),
            "sections": rendered_sections,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/trust-center/policies/{policy_id}/download")
def public_policy_download(policy_id: str, request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Not found")

    policy = db.get(Policy, policy_id)
    if policy is None or policy.status != "approved":
        raise HTTPException(status_code=404, detail="Not found")

    linked = any(section.linked_policy_id == policy_id for section in _public_published_sections(db))
    if not linked:
        raise HTTPException(status_code=404, detail="Not found")

    version = policy.latest_version
    if version is None:
        raise HTTPException(status_code=404, detail="Not found")

    app_settings = request.app.state.settings
    path = policy_version_path(
        app_settings.data_dir, policy_id, version.version_number, version.stored_filename
    )
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(
        path,
        media_type=version.media_type,
        filename=version.original_filename,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )
