"""Digital Compliance TPM console (issue #15): Observe/Prioritize display,
explicit Nag/Observe actions, and pre-fill draft viewing.

Router-level `require_login` (any role may view), per-mutation
`require_write_access` + `verify_csrf` — the same #37 RBAC convention as
every other router in this codebase. No route here ever mutates a
domain (non-AI) model — see app/digital_tpm.py's module docstring and
tests/test_digital_tpm_boundary.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_login, require_write_access, verify_csrf
from app.digital_tpm import generate_due_reminders, generate_observe_narrative, observe_program_summary
from app.flash import redirect_with_flash
from app.models import AiReminderState, AiTaskExecution
from app.readiness import compute_readiness_queue

router = APIRouter(prefix="/ai/tpm", tags=["digital-tpm"], dependencies=[Depends(require_login)])


@router.get("")
def tpm_console(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    summary = observe_program_summary(db, settings)
    queue = compute_readiness_queue(db)
    recent_executions = db.scalars(
        select(AiTaskExecution).order_by(AiTaskExecution.created_at.desc()).limit(20)
    ).all()
    reminder_states = db.scalars(
        select(AiReminderState).order_by(AiReminderState.updated_at.desc()).limit(20)
    ).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "ai_tpm/console.html",
        {
            "summary": summary,
            "queue": queue[:10],
            "recent_executions": recent_executions,
            "reminder_states": reminder_states,
        },
    )


@router.post("/observe")
def run_observe(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    settings = request.app.state.settings
    execution = generate_observe_narrative(db, settings, actor=user.email)
    return redirect_with_flash(f"/ai/tpm/drafts/{execution.id}", "Observe summary generated.")


@router.post("/nag-scan")
def run_nag_scan(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    settings = request.app.state.settings
    created = generate_due_reminders(db, settings, actor=user.email)
    return redirect_with_flash("/ai/tpm", f"Nag scan complete: {len(created)} reminder(s) recorded.")


@router.get("/drafts/{execution_id}")
def view_draft(execution_id: str, request: Request, db: Session = Depends(get_db)):
    execution = db.get(AiTaskExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "ai_tpm/draft.html", {"execution": execution})
