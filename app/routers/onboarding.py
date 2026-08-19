"""Compliance scoping and guided onboarding routes (issue #30).

Router-level `require_login`, per-mutation `require_write_access` +
`verify_csrf` — #37's RBAC convention, matching
app/routers/evidence_artifacts.py. See
docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-design.md
§6.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.compliance_scope import InvalidComplianceScopeError, define_or_revise_scope, get_compliance_scope
from app.deps import get_db, require_login, require_write_access, verify_csrf
from app.flash import redirect_with_flash
from app.models import TRUST_SERVICE_CATEGORIES
from app.onboarding import compute_onboarding_steps, get_primary_framework
from app.starter_controls import generate_starter_controls_for_framework

router = APIRouter(prefix="/onboarding", tags=["onboarding"], dependencies=[Depends(require_login)])


def _parse_date(value: str) -> datetime.date | None:
    stripped = (value or "").strip()
    if not stripped:
        return None
    try:
        return datetime.date.fromisoformat(stripped)
    except ValueError:
        return None


@router.get("")
def onboarding_checklist(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    steps = compute_onboarding_steps(db, settings)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "onboarding/checklist.html",
        {
            "steps": steps,
            "scope": get_compliance_scope(db),
            "primary_framework": get_primary_framework(db),
        },
    )


@router.get("/scope")
def scope_form(request: Request, db: Session = Depends(get_db)):
    scope = get_compliance_scope(db)
    selected_categories = set(scope.trust_service_categories.split(",")) if scope else {"security"}
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "onboarding/scope_form.html",
        {
            "scope": scope,
            "trust_service_categories": TRUST_SERVICE_CATEGORIES,
            "selected_categories": selected_categories,
        },
    )


@router.post("/scope")
async def submit_scope(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        define_or_revise_scope(
            db,
            service_description=(form.get("service_description") or "").strip(),
            trust_service_categories=form.getlist("trust_service_categories"),
            audit_period_starts_on=_parse_date(form.get("audit_period_starts_on", "")),
            audit_period_ends_on=_parse_date(form.get("audit_period_ends_on", "")),
            data_categories=(form.get("data_categories") or "").strip(),
            locations=(form.get("locations") or "").strip(),
            exclusions=(form.get("exclusions") or "").strip(),
            exclusions_rationale=(form.get("exclusions_rationale") or "").strip(),
            actor_type="user",
            actor_id=user.id,
        )
    except InvalidComplianceScopeError as exc:
        return redirect_with_flash("/onboarding/scope", str(exc), kind="error")

    return redirect_with_flash("/onboarding", "Compliance scope saved.")


@router.post("/generate-starter-controls")
def generate_starter_controls(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    framework = get_primary_framework(db)
    if framework is None:
        return redirect_with_flash("/onboarding", "No primary framework is configured yet.", kind="error")

    created = generate_starter_controls_for_framework(db, framework, actor=user.email)
    if not created:
        return redirect_with_flash("/onboarding", "Every requirement already has a control mapped to it.")
    return redirect_with_flash("/onboarding", f"Generated {len(created)} starter control(s).")
