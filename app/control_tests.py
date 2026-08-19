"""Control-test population/sample/test recording and event projectors
(issue #13).

Population/sample are plain rows (see app/models.py::ControlTestPopulation
docstring for why); ControlTest is a canonical projection over the
"control_test" DomainEvent aggregate (app/events.py), growing over time
exactly like app/control_occurrences.py's ControlOccurrence — never
written to directly outside the projector functions here.

See docs/superpowers/specs/2026-08-19-issue13-assurance-testing-findings-design.md
for the full design.
"""

from __future__ import annotations

import datetime
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import (
    TEST_RESULTS,
    ControlOccurrence,
    ControlPeriod,
    ControlTest,
    ControlTestEvidenceArtifact,
    ControlTestException,
    ControlTestPopulation,
    ControlTestPopulationItem,
    ControlTestSample,
    ControlTestSampleItem,
    InternalControl,
    new_id,
)


class InvalidControlTestError(ValueError):
    """Raised when caller-supplied population/sample/test fields fail validation."""


def freeze_population(
    session: Session,
    control: InternalControl,
    *,
    control_period: ControlPeriod | None = None,
    description: str = "",
    criteria_note: str = "",
    actor: str,
) -> ControlTestPopulation:
    """Snapshot every current ControlOccurrence for `control` (optionally
    narrowed to `control_period`) into a new, permanently frozen
    population. Copying occurrence ids in now — rather than a live query
    evaluated later — is what keeps a later new occurrence, or a
    correction to an already-frozen occurrence, from silently changing the
    historical sample basis."""
    query = select(ControlOccurrence).where(ControlOccurrence.control_id == control.id)
    if control_period is not None:
        query = query.where(ControlOccurrence.control_period_id == control_period.id)
    occurrences = session.scalars(query).all()

    population = ControlTestPopulation(
        control_id=control.id,
        control_period_id=control_period.id if control_period else None,
        description=description,
        criteria_note=criteria_note,
        created_by=actor,
    )
    session.add(population)
    session.flush()
    for occurrence in occurrences:
        session.add(
            ControlTestPopulationItem(population_id=population.id, control_occurrence_id=occurrence.id)
        )
    session.flush()
    return population


def draw_sample(
    session: Session,
    population: ControlTestPopulation,
    population_item_ids: list[str],
    *,
    selection_method: str,
    selection_rationale: str,
    actor: str,
) -> ControlTestSample:
    if not population_item_ids:
        raise InvalidControlTestError("A sample must select at least one population item.")

    valid_ids = {item.id for item in population.items}
    unknown = set(population_item_ids) - valid_ids
    if unknown:
        raise InvalidControlTestError(
            f"Item id(s) {sorted(unknown)} do not belong to population '{population.id}'."
        )

    sample = ControlTestSample(
        population_id=population.id,
        selection_method=selection_method,
        selection_rationale=selection_rationale,
        created_by=actor,
    )
    session.add(sample)
    session.flush()
    for item_id in population_item_ids:
        session.add(ControlTestSampleItem(sample_id=sample.id, population_item_id=item_id))
    session.flush()
    return sample


def _parse_iso_datetime(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def _project_recorded(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        ControlTest(
            id=event.aggregate_id,
            control_id=payload["control_id"],
            control_period_id=payload.get("control_period_id"),
            sample_id=payload.get("sample_id"),
            procedure_description=payload.get("procedure_description", ""),
            tester_person_id=payload["tester_person_id"],
            performed_at=_parse_iso_datetime(payload["performed_at"]),
            result=payload["result"],
            notes=payload.get("notes", ""),
            retest_of_test_id=payload.get("retest_of_test_id"),
            created_at=event.recorded_at,
            updated_at=event.recorded_at,
        )
    )
    for exception in payload.get("exceptions", []):
        session.add(
            ControlTestException(
                test_id=event.aggregate_id,
                sample_item_id=exception.get("sample_item_id"),
                description=exception.get("description", ""),
            )
        )
    # Explicit flush (session-wide autoflush=False, app/db.py) — matches
    # app/control_occurrences.py::_project_materialized's comment: a later
    # ControlTestEvidenceLinked event for this same aggregate needs
    # session.get() to see this row.
    session.flush()


def _project_evidence_linked(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        ControlTestEvidenceArtifact(
            test_id=event.aggregate_id, evidence_artifact_version_id=payload["evidence_artifact_version_id"]
        )
    )


CONTROL_TEST_PROJECTORS: dict[str, ProjectorFn] = {
    "ControlTestRecorded": _project_recorded,
    "ControlTestEvidenceLinked": _project_evidence_linked,
}


def _reset_control_tests(session: Session) -> None:
    session.execute(delete(ControlTestEvidenceArtifact))
    session.execute(delete(ControlTestException))
    session.execute(delete(ControlTest))


def rebuild_control_test_projection(session: Session) -> None:
    """Wipe and deterministically rebuild the ControlTest/ControlTestException/
    ControlTestEvidenceArtifact projections from DomainEvent history —
    mirrors app/control_occurrences.py::rebuild_control_occurrence_projection."""
    rebuild_projection(
        session, CONTROL_TEST_PROJECTORS, aggregate_type="control_test", reset=_reset_control_tests
    )


def record_test(
    session: Session,
    control: InternalControl,
    *,
    sample: ControlTestSample | None = None,
    control_period: ControlPeriod | None = None,
    procedure_description: str,
    tester_person_id: str,
    performed_at: datetime.datetime,
    result: str,
    notes: str = "",
    exceptions: list[dict] | None = None,
    retest_of: ControlTest | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> ControlTest:
    if result not in TEST_RESULTS:
        raise InvalidControlTestError(f"result must be one of {TEST_RESULTS}, got {result!r}")

    exceptions = exceptions or []
    if exceptions and result != "exceptions_noted":
        raise InvalidControlTestError("exceptions were given but result is not 'exceptions_noted'.")

    if sample is not None:
        population = session.get(ControlTestPopulation, sample.population_id)
        if population.control_id != control.id:
            raise InvalidControlTestError(
                f"sample '{sample.id}' was drawn from a population of control "
                f"'{population.control_id}', not '{control.id}'."
            )
        valid_sample_item_ids = {item.id for item in sample.items}
        for exception in exceptions:
            sample_item_id = exception.get("sample_item_id")
            if sample_item_id is not None and sample_item_id not in valid_sample_item_ids:
                raise InvalidControlTestError(
                    f"sample_item_id '{sample_item_id}' does not belong to sample '{sample.id}'."
                )

    if retest_of is not None and retest_of.control_id != control.id:
        raise InvalidControlTestError("retest_of must be a prior test of the same control.")

    payload = {
        "control_id": control.id,
        "control_period_id": control_period.id if control_period else None,
        "sample_id": sample.id if sample else None,
        "procedure_description": procedure_description,
        "tester_person_id": tester_person_id,
        "performed_at": performed_at.isoformat(),
        "result": result,
        "notes": notes,
        "exceptions": exceptions,
        "retest_of_test_id": retest_of.id if retest_of else None,
    }
    event = append_and_project(
        session,
        CONTROL_TEST_PROJECTORS,
        aggregate_type="control_test",
        aggregate_id=new_id(),
        event_type="ControlTestRecorded",
        payload=payload,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return session.get(ControlTest, event.aggregate_id)


def link_test_evidence(
    session: Session,
    test: ControlTest,
    evidence_artifact_version_id: str,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> None:
    append_and_project(
        session,
        CONTROL_TEST_PROJECTORS,
        aggregate_type="control_test",
        aggregate_id=test.id,
        event_type="ControlTestEvidenceLinked",
        payload={"evidence_artifact_version_id": evidence_artifact_version_id},
        actor_type=actor_type,
        actor_id=actor_id,
    )
