"""Finding lifecycle (open -> remediating -> retesting -> closed) and event
projectors (issue #13).

A plain module — matching the shape of app/policy_lifecycle.py. Builds on
app/events.py: `Finding` (app/models.py) is a canonical projection over the
"finding" DomainEvent aggregate, never written to directly outside the
projector functions here. Deliberately un-prefixed (not "ControlFinding")
so it stays reusable by non-control-test origins (e.g. ISO internal audit)
per the issue's explicit reuse instruction.

See docs/superpowers/specs/2026-08-19-issue13-assurance-testing-findings-design.md
for the full design.
"""

from __future__ import annotations

import datetime
import json

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import (
    FINDING_CLOSURE_DECISIONS,
    FINDING_SEVERITIES,
    ControlTest,
    Finding,
    FindingRemediationUpdate,
    FindingRetest,
    new_id,
)


def _project_opened(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        Finding(
            id=event.aggregate_id,
            title=payload["title"],
            description=payload.get("description", ""),
            severity=payload["severity"],
            status="open",
            control_id=payload.get("control_id"),
            source_test_id=payload.get("source_test_id"),
            owner_person_id=payload.get("owner_person_id"),
            due_date=payload.get("due_date"),
            created_at=event.recorded_at,
            updated_at=event.recorded_at,
        )
    )
    # Explicit flush (session-wide autoflush=False, app/db.py) — a later
    # event for this same aggregate in the same append_and_project call (or
    # a rebuild replay pass) needs session.get() to see this row.
    session.flush()


def _project_remediation_update_recorded(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    finding = session.get(Finding, event.aggregate_id)
    session.add(
        FindingRemediationUpdate(
            finding_id=finding.id,
            note=payload["note"],
            actor=event.actor_id or event.actor_type,
            linked_evidence_artifact_version_id=payload.get("linked_evidence_artifact_version_id"),
            created_at=event.recorded_at,
        )
    )
    if finding.status == "open":
        finding.status = "remediating"
    finding.updated_at = event.recorded_at


def _project_retest_requested(session: Session, event: DomainEvent) -> None:
    finding = session.get(Finding, event.aggregate_id)
    finding.status = "retesting"
    finding.updated_at = event.recorded_at


def _project_retest_recorded(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    finding = session.get(Finding, event.aggregate_id)
    session.add(FindingRetest(finding_id=finding.id, test_id=payload["test_id"]))
    finding.status = "remediating" if payload["test_result"] == "exceptions_noted" else "retesting"
    finding.updated_at = event.recorded_at


def _project_closed(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    finding = session.get(Finding, event.aggregate_id)
    finding.status = "closed"
    finding.closure_decision = payload["decision"]
    finding.closure_note = payload.get("note", "")
    finding.updated_at = event.recorded_at


FINDING_PROJECTORS: dict[str, ProjectorFn] = {
    "FindingOpened": _project_opened,
    "FindingRemediationUpdateRecorded": _project_remediation_update_recorded,
    "FindingRetestRequested": _project_retest_requested,
    "FindingRetestRecorded": _project_retest_recorded,
    "FindingClosed": _project_closed,
}


def _reset_findings(session: Session) -> None:
    session.execute(delete(FindingRetest))
    session.execute(delete(FindingRemediationUpdate))
    session.execute(delete(Finding))


def rebuild_finding_projection(session: Session) -> None:
    """Wipe and deterministically rebuild the Finding/FindingRemediationUpdate/
    FindingRetest projections from DomainEvent history — mirrors
    app/control_occurrences.py::rebuild_control_occurrence_projection."""
    rebuild_projection(session, FINDING_PROJECTORS, aggregate_type="finding", reset=_reset_findings)


def open_finding(
    session: Session,
    *,
    title: str,
    description: str = "",
    severity: str,
    control_id: str | None = None,
    source_test_id: str | None = None,
    owner_person_id: str | None = None,
    due_date: datetime.date | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> Finding:
    if severity not in FINDING_SEVERITIES:
        raise ValueError(f"severity must be one of {FINDING_SEVERITIES}, got {severity!r}")
    if not title.strip():
        raise ValueError("title is required")

    event = append_and_project(
        session,
        FINDING_PROJECTORS,
        aggregate_type="finding",
        aggregate_id=new_id(),
        event_type="FindingOpened",
        payload={
            "title": title,
            "description": description,
            "severity": severity,
            "control_id": control_id,
            "source_test_id": source_test_id,
            "owner_person_id": owner_person_id,
            "due_date": due_date.isoformat() if due_date else None,
        },
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return session.get(Finding, event.aggregate_id)


def record_remediation_update(
    session: Session,
    finding: Finding,
    *,
    note: str,
    linked_evidence_artifact_version_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> Finding:
    if finding.status == "closed":
        raise ValueError("Cannot record a remediation update on a closed finding")
    if not note.strip():
        raise ValueError("note is required")

    append_and_project(
        session,
        FINDING_PROJECTORS,
        aggregate_type="finding",
        aggregate_id=finding.id,
        event_type="FindingRemediationUpdateRecorded",
        payload={"note": note, "linked_evidence_artifact_version_id": linked_evidence_artifact_version_id},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return finding


def request_retest(
    session: Session, finding: Finding, *, actor_type: str = "user", actor_id: str | None = None
) -> Finding:
    if finding.status != "remediating":
        raise ValueError(f"Cannot request a retest from status={finding.status!r} (must be 'remediating')")

    append_and_project(
        session,
        FINDING_PROJECTORS,
        aggregate_type="finding",
        aggregate_id=finding.id,
        event_type="FindingRetestRequested",
        payload={},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return finding


def record_retest(
    session: Session,
    finding: Finding,
    *,
    test: ControlTest,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> Finding:
    """Link `test` as this finding's retest. When this finding has a
    `source_test_id` (it arose from a formal ControlTest), `test` must
    itself have been recorded with `retest_of` pointing at that same test
    (see app/control_tests.py::record_test) — enforced below, not just
    documented, so the lineage can't silently drift. A finding opened
    without a `source_test_id` but with a `control_id` (e.g. a manual
    observation against a known control) still requires `test.control_id`
    to match — a retest from an unrelated control is never accepted. Only
    a finding with neither (control-less, source-test-less) accepts any
    test, since there is nothing to validate lineage against. This
    function does not create the test, only records the finding-level
    consequence of it. Never auto-closes: a passing retest leaves the
    finding in "retesting", awaiting an explicit, separate close_finding
    call by an authorized human (issue #13's "external auditor
    conclusions must not be fabricated... unless explicitly entered by an
    authorized human")."""
    if finding.status != "retesting":
        raise ValueError(f"Cannot record a retest from status={finding.status!r} (must be 'retesting')")
    if finding.source_test_id is not None and test.retest_of_test_id != finding.source_test_id:
        raise ValueError(
            "test.retest_of_test_id must reference this finding's source_test_id "
            f"({finding.source_test_id!r}), got {test.retest_of_test_id!r}"
        )
    no_source_test = finding.source_test_id is None
    control_mismatch = finding.control_id is not None and test.control_id != finding.control_id
    if no_source_test and control_mismatch:
        raise ValueError(
            f"test.control_id ({test.control_id!r}) does not match this finding's control_id "
            f"({finding.control_id!r})"
        )

    append_and_project(
        session,
        FINDING_PROJECTORS,
        aggregate_type="finding",
        aggregate_id=finding.id,
        event_type="FindingRetestRecorded",
        payload={"test_id": test.id, "test_result": test.result},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return finding


def close_finding(
    session: Session,
    finding: Finding,
    *,
    decision: str,
    note: str = "",
    actor_type: str = "user",
    actor_id: str | None = None,
) -> Finding:
    if finding.status == "closed":
        raise ValueError("Finding is already closed")
    if decision not in FINDING_CLOSURE_DECISIONS:
        raise ValueError(f"decision must be one of {FINDING_CLOSURE_DECISIONS}, got {decision!r}")

    append_and_project(
        session,
        FINDING_PROJECTORS,
        aggregate_type="finding",
        aggregate_id=finding.id,
        event_type="FindingClosed",
        payload={"decision": decision, "note": note},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return finding
