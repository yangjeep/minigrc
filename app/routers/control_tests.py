"""Control-test population/sample/test routes (issue #13).

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

from app.control_tests import (
    InvalidControlTestError,
    draw_sample,
    freeze_population,
    link_test_evidence,
    record_test,
)
from app.deps import get_db, require_login, require_write_access, verify_csrf
from app.flash import redirect_with_flash
from app.models import (
    TEST_RESULTS,
    ControlPeriod,
    ControlTest,
    ControlTestEvidenceArtifact,
    ControlTestException,
    ControlTestPopulation,
    ControlTestSample,
    EvidenceArtifactVersion,
    InternalControl,
    Person,
)

router = APIRouter(prefix="/control-tests", tags=["control-tests"], dependencies=[Depends(require_login)])


@router.get("/populations")
def list_populations(request: Request, db: Session = Depends(get_db)):
    populations = db.scalars(
        select(ControlTestPopulation).order_by(ControlTestPopulation.frozen_at.desc())
    ).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "control_tests/populations_list.html", {"populations": populations}
    )


@router.get("/populations/new")
def new_population_form(request: Request, db: Session = Depends(get_db)):
    controls = db.scalars(select(InternalControl).order_by(InternalControl.name)).all()
    periods = db.scalars(select(ControlPeriod).order_by(ControlPeriod.starts_on.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "control_tests/populations_new.html", {"controls": controls, "periods": periods}
    )


@router.post("/populations")
def create_population(
    request: Request,
    control_id: str = Form(...),
    control_period_id: str = Form(""),
    description: str = Form(""),
    criteria_note: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    control = db.get(InternalControl, control_id)
    if control is None:
        return redirect_with_flash("/control-tests/populations/new", "Control not found.", kind="error")

    control_period = None
    control_period_id = control_period_id.strip() or None
    if control_period_id:
        control_period = db.get(ControlPeriod, control_period_id)
        if control_period is None:
            return redirect_with_flash(
                "/control-tests/populations/new", "Control period not found.", kind="error"
            )

    population = freeze_population(
        db,
        control,
        control_period=control_period,
        description=description.strip(),
        criteria_note=criteria_note.strip(),
        actor=user.email,
    )
    return redirect_with_flash(f"/control-tests/populations/{population.id}", "Population frozen.")


@router.get("/populations/{population_id}")
def view_population(population_id: str, request: Request, db: Session = Depends(get_db)):
    population = db.get(ControlTestPopulation, population_id)
    if population is None:
        raise HTTPException(status_code=404, detail="Population not found")

    samples = db.scalars(
        select(ControlTestSample).where(ControlTestSample.population_id == population.id)
    ).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "control_tests/populations_detail.html", {"population": population, "samples": samples}
    )


@router.post("/populations/{population_id}/samples")
def create_sample(
    population_id: str,
    request: Request,
    population_item_ids: list[str] = Form([]),
    selection_method: str = Form(""),
    selection_rationale: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    population = db.get(ControlTestPopulation, population_id)
    if population is None:
        raise HTTPException(status_code=404, detail="Population not found")

    try:
        draw_sample(
            db,
            population,
            population_item_ids,
            selection_method=selection_method.strip(),
            selection_rationale=selection_rationale.strip(),
            actor=user.email,
        )
    except InvalidControlTestError as exc:
        return redirect_with_flash(f"/control-tests/populations/{population_id}", str(exc), kind="error")

    return redirect_with_flash(f"/control-tests/populations/{population_id}", "Sample drawn.")


@router.get("")
def list_control_tests(request: Request, db: Session = Depends(get_db)):
    tests = db.scalars(select(ControlTest).order_by(ControlTest.performed_at.desc())).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "control_tests/list.html", {"tests": tests})


@router.get("/new")
def new_control_test_form(request: Request, sample_id: str = "", db: Session = Depends(get_db)):
    controls = db.scalars(select(InternalControl).order_by(InternalControl.name)).all()
    periods = db.scalars(select(ControlPeriod).order_by(ControlPeriod.starts_on.desc())).all()
    samples = db.scalars(select(ControlTestSample)).all()
    people = db.scalars(select(Person).order_by(Person.display_name, Person.email)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "control_tests/new.html",
        {
            "controls": controls,
            "periods": periods,
            "samples": samples,
            "people": people,
            "results": TEST_RESULTS,
            "selected_sample_id": sample_id,
        },
    )


@router.post("")
def create_control_test(
    request: Request,
    control_id: str = Form(...),
    control_period_id: str = Form(""),
    sample_id: str = Form(""),
    procedure_description: str = Form(...),
    tester_person_id: str = Form(...),
    performed_on: str = Form(...),
    result: str = Form(...),
    notes: str = Form(""),
    exception_lines: str = Form(""),
    retest_of_test_id: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    control = db.get(InternalControl, control_id)
    if control is None:
        return redirect_with_flash("/control-tests/new", "Control not found.", kind="error")

    tester = db.get(Person, tester_person_id)
    if tester is None:
        return redirect_with_flash("/control-tests/new", "Tester not found.", kind="error")

    try:
        performed_date = datetime.date.fromisoformat(performed_on)
    except ValueError:
        return redirect_with_flash("/control-tests/new", "Performed date must be a valid date.", kind="error")
    performed_at = datetime.datetime.combine(performed_date, datetime.time.max, tzinfo=datetime.UTC)

    control_period = None
    control_period_id = control_period_id.strip() or None
    if control_period_id:
        control_period = db.get(ControlPeriod, control_period_id)
        if control_period is None:
            return redirect_with_flash("/control-tests/new", "Control period not found.", kind="error")

    sample = None
    sample_id = sample_id.strip() or None
    if sample_id:
        sample = db.get(ControlTestSample, sample_id)
        if sample is None:
            return redirect_with_flash("/control-tests/new", "Sample not found.", kind="error")

    retest_of = None
    retest_of_test_id = retest_of_test_id.strip() or None
    if retest_of_test_id:
        retest_of = db.get(ControlTest, retest_of_test_id)
        if retest_of is None:
            return redirect_with_flash("/control-tests/new", "Retest-of test not found.", kind="error")

    exceptions = [
        {"sample_item_id": None, "description": line.strip()}
        for line in exception_lines.splitlines()
        if line.strip()
    ]

    try:
        test = record_test(
            db,
            control,
            sample=sample,
            control_period=control_period,
            procedure_description=procedure_description.strip(),
            tester_person_id=tester.id,
            performed_at=performed_at,
            result=result,
            notes=notes.strip(),
            exceptions=exceptions,
            retest_of=retest_of,
            actor_type="user",
            actor_id=user.id,
        )
    except InvalidControlTestError as exc:
        return redirect_with_flash("/control-tests/new", str(exc), kind="error")

    return redirect_with_flash(f"/control-tests/{test.id}", "Test recorded.")


@router.get("/{test_id}")
def view_control_test(test_id: str, request: Request, db: Session = Depends(get_db)):
    test = db.get(ControlTest, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Control test not found")

    linked_evidence_rows = db.scalars(
        select(ControlTestEvidenceArtifact).where(ControlTestEvidenceArtifact.test_id == test.id)
    ).all()
    linked_evidence_ids = {row.evidence_artifact_version_id for row in linked_evidence_rows}
    available_evidence = db.scalars(
        select(EvidenceArtifactVersion).where(EvidenceArtifactVersion.id.not_in(linked_evidence_ids or [""]))
    ).all()
    exceptions = db.scalars(select(ControlTestException).where(ControlTestException.test_id == test.id)).all()
    retests = db.scalars(select(ControlTest).where(ControlTest.retest_of_test_id == test.id)).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "control_tests/detail.html",
        {
            "test": test,
            "available_evidence": available_evidence,
            "linked_evidence": linked_evidence_rows,
            "exceptions": exceptions,
            "retests": retests,
        },
    )


@router.post("/{test_id}/link-evidence")
def link_evidence_route(
    test_id: str,
    request: Request,
    evidence_artifact_version_id: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_write_access),
    _csrf: None = Depends(verify_csrf),
):
    test = db.get(ControlTest, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Control test not found")

    version = db.get(EvidenceArtifactVersion, evidence_artifact_version_id)
    if version is None:
        return redirect_with_flash(f"/control-tests/{test_id}", "Evidence version not found.", kind="error")

    link_test_evidence(db, test, version.id, actor_type="user", actor_id=user.id)
    return redirect_with_flash(f"/control-tests/{test_id}", "Evidence linked.")
