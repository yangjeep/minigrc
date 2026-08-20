"""Finding lifecycle routes (issue #13).

Router-level `require_login`, per-mutation `require_write_access` +
`verify_csrf` — #37's RBAC convention. See
docs/superpowers/specs/2026-08-19-issue13-assurance-testing-findings-design.md
§5.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_login, require_write_access, verify_csrf
from app.digital_tpm import draft_prefill
from app.finding_lifecycle import (
    close_finding,
    open_finding,
    record_remediation_update,
    record_retest,
    request_retest,
)
from app.flash import redirect_with_flash
from app.models import (
    FINDING_CLOSURE_DECISIONS,
    FINDING_SEVERITIES,
    ControlTest,
    EvidenceArtifactVersion,
    Finding,
    FindingRemediationUpdate,
    FindingRetest,
    InternalControl,
    Person,
)

router = APIRouter(prefix="/findings", tags=["findings"], dependencies=[Depends(require_login)])


@router.get("")
def list_findings(request: Request, db: Session = Depends(get_db)):
    findings = db.scalars(select(Finding).order_by(Finding.created_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "findings/list.html", {"findings": findings})


@router.get("/new")
def new_finding_form(request: Request, source_test_id: str = "", db: Session = Depends(get_db)):
    controls = db.scalars(select(InternalControl).order_by(InternalControl.name)).all()
    people = db.scalars(select(Person).order_by(Person.display_name, Person.email)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "findings/new.html",
        {
            "controls": controls,
            "people": people,
            "severities": FINDING_SEVERITIES,
            "selected_source_test_id": source_test_id,
        },
    )


@router.post("")
def create_finding(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    severity: str = Form(...),
    control_id: str = Form(""),
    source_test_id: str = Form(""),
    owner_person_id: str = Form(""),
    due_date: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    control_id = control_id.strip() or None
    if control_id and db.get(InternalControl, control_id) is None:
        return redirect_with_flash("/findings/new", "Control not found.", kind="error")

    source_test_id = source_test_id.strip() or None
    if source_test_id and db.get(ControlTest, source_test_id) is None:
        return redirect_with_flash("/findings/new", "Source test not found.", kind="error")

    owner_person_id = owner_person_id.strip() or None
    if owner_person_id and db.get(Person, owner_person_id) is None:
        return redirect_with_flash("/findings/new", "Owner person not found.", kind="error")

    due_date_value = None
    due_date = due_date.strip()
    if due_date:
        try:
            due_date_value = datetime.date.fromisoformat(due_date)
        except ValueError:
            return redirect_with_flash("/findings/new", "Due date must be a valid date.", kind="error")

    try:
        finding = open_finding(
            db,
            title=title.strip(),
            description=description.strip(),
            severity=severity,
            control_id=control_id,
            source_test_id=source_test_id,
            owner_person_id=owner_person_id,
            due_date=due_date_value,
            actor_type="user",
            actor_id=user.id,
        )
    except ValueError as exc:
        return redirect_with_flash("/findings/new", str(exc), kind="error")

    return redirect_with_flash(f"/findings/{finding.id}", "Finding opened.")


@router.get("/{finding_id}")
def view_finding(finding_id: str, request: Request, db: Session = Depends(get_db)):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    updates = db.scalars(
        select(FindingRemediationUpdate)
        .where(FindingRemediationUpdate.finding_id == finding.id)
        .order_by(FindingRemediationUpdate.created_at)
    ).all()
    retests = db.scalars(select(FindingRetest).where(FindingRetest.finding_id == finding.id)).all()

    candidate_tests = []
    if finding.control_id is not None:
        used_test_ids = {r.test_id for r in retests}
        query = select(ControlTest).where(ControlTest.control_id == finding.control_id)
        if finding.source_test_id is not None:
            query = query.where(ControlTest.retest_of_test_id == finding.source_test_id)
        candidate_tests = [t for t in db.scalars(query).all() if t.id not in used_test_ids]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "findings/detail.html",
        {
            "finding": finding,
            "updates": updates,
            "retests": retests,
            "candidate_tests": candidate_tests,
            "closure_decisions": FINDING_CLOSURE_DECISIONS,
            "available_evidence": db.scalars(select(EvidenceArtifactVersion)).all(),
        },
    )


@router.post("/{finding_id}/ai-draft-response")
def draft_finding_response(
    finding_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    execution = draft_prefill(
        db, request.app.state.settings, task_type="finding_response", entity=finding, actor=user.email
    )
    return redirect_with_flash(f"/ai/tpm/drafts/{execution.id}", "Draft response generated.")


@router.post("/{finding_id}/remediation-updates")
def create_remediation_update(
    finding_id: str,
    request: Request,
    note: str = Form(...),
    linked_evidence_artifact_version_id: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    linked_evidence_artifact_version_id = linked_evidence_artifact_version_id.strip() or None
    if (
        linked_evidence_artifact_version_id
        and db.get(EvidenceArtifactVersion, linked_evidence_artifact_version_id) is None
    ):
        return redirect_with_flash(f"/findings/{finding_id}", "Evidence version not found.", kind="error")

    try:
        record_remediation_update(
            db,
            finding,
            note=note.strip(),
            linked_evidence_artifact_version_id=linked_evidence_artifact_version_id,
            actor_type="user",
            actor_id=user.id,
        )
    except ValueError as exc:
        return redirect_with_flash(f"/findings/{finding_id}", str(exc), kind="error")

    return redirect_with_flash(f"/findings/{finding_id}", "Remediation update recorded.")


@router.post("/{finding_id}/request-retest")
def request_retest_route(
    finding_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    try:
        request_retest(db, finding, actor_type="user", actor_id=user.id)
    except ValueError as exc:
        return redirect_with_flash(f"/findings/{finding_id}", str(exc), kind="error")

    return redirect_with_flash(f"/findings/{finding_id}", "Retest requested.")


@router.post("/{finding_id}/record-retest")
def record_retest_route(
    finding_id: str,
    request: Request,
    test_id: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    test = db.get(ControlTest, test_id)
    if test is None:
        return redirect_with_flash(f"/findings/{finding_id}", "Test not found.", kind="error")

    try:
        record_retest(db, finding, test=test, actor_type="user", actor_id=user.id)
    except ValueError as exc:
        return redirect_with_flash(f"/findings/{finding_id}", str(exc), kind="error")

    return redirect_with_flash(f"/findings/{finding_id}", "Retest recorded.")


@router.post("/{finding_id}/close")
def close_finding_route(
    finding_id: str,
    request: Request,
    decision: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    try:
        close_finding(db, finding, decision=decision, note=note.strip(), actor_type="user", actor_id=user.id)
    except ValueError as exc:
        return redirect_with_flash(f"/findings/{finding_id}", str(exc), kind="error")

    return redirect_with_flash(f"/findings/{finding_id}", "Finding closed.")
