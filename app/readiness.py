"""Continuous readiness work queue (issue #14).

Every item below is computed live from already-authoritative state —
never stored, never a second manually-maintained task table. Same "compute
and expose, never persist a derived flag" precedent as
app/vendor_flags.py::compute_flags and app/routers/controls.py::view_control's
overdue/upcoming occurrence split, just unified across domains into one
queue instead of scoped to a single vendor/control page.

See docs/superpowers/specs/2026-08-19-issue14-readiness-dashboard-design.md
for the full design, including which "required" categories from the issue
are deliberately NOT implemented because no backing field exists for them
today (most notably "evidence awaiting review").
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import (
    ControlOccurrence,
    ControlOccurrenceEvidence,
    ControlOccurrenceEvidenceArtifact,
    ControlRequirementMapping,
    ControlTest,
    EvidenceSnapshot,
    Finding,
    FrameworkRequirement,
    InternalControl,
    Policy,
)

READINESS_CATEGORIES = {
    "occurrence_overdue": "Missed control occurrence",
    "occurrence_upcoming": "Upcoming control occurrence",
    "occurrence_missing_evidence": "Occurrence missing evidence",
    "evidence_stale": "Stale evidence",
    "control_test_failed_no_finding": "Test exception without a finding",
    "finding_needs_attention": "Finding needs attention",
    "finding_needs_retest": "Finding awaiting retest",
    "control_missing_owner": "Control missing an owner",
    "finding_missing_owner": "Finding missing an owner",
    "policy_review_overdue": "Policy review overdue",
}

_UPCOMING_WINDOW_DAYS = 14


@dataclass(frozen=True)
class ReadinessItem:
    category: str
    reason: str
    link: str
    control_id: str | None
    due_on: datetime.date | None


def _naive_utc_now() -> datetime.datetime:
    # Naive UTC "now" for comparison against DateTime columns that may
    # come back tz-naive after a real DB round-trip (SQLite DateTime
    # without timezone=True) — same handling as
    # app/routers/controls.py::view_control and app/vendor_flags.py.
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _as_naive(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _compute_occurrence_items(session: Session) -> list[ReadinessItem]:
    now = _naive_utc_now()
    upcoming_cutoff = now + datetime.timedelta(days=_UPCOMING_WINDOW_DAYS)
    occurrences = session.scalars(
        select(ControlOccurrence).where(ControlOccurrence.performed_at.is_(None))
    ).all()

    items = []
    for occurrence in occurrences:
        if occurrence.due_at is None:
            continue
        due_at = _as_naive(occurrence.due_at)
        link = f"/occurrences/{occurrence.id}"
        if due_at < now:
            items.append(
                ReadinessItem(
                    category="occurrence_overdue",
                    reason=f"Due {due_at.date()}, not yet performed",
                    link=link,
                    control_id=occurrence.control_id,
                    due_on=due_at.date(),
                )
            )
        elif due_at <= upcoming_cutoff:
            items.append(
                ReadinessItem(
                    category="occurrence_upcoming",
                    reason=f"Due {due_at.date()}",
                    link=link,
                    control_id=occurrence.control_id,
                    due_on=due_at.date(),
                )
            )
    return items


def _compute_occurrence_missing_evidence_items(session: Session) -> list[ReadinessItem]:
    has_snapshot_evidence = exists().where(ControlOccurrenceEvidence.occurrence_id == ControlOccurrence.id)
    has_artifact_evidence = exists().where(
        ControlOccurrenceEvidenceArtifact.occurrence_id == ControlOccurrence.id
    )
    occurrences = session.scalars(
        select(ControlOccurrence).where(
            ControlOccurrence.performed_at.is_not(None),
            ~has_snapshot_evidence,
            ~has_artifact_evidence,
        )
    ).all()
    return [
        ReadinessItem(
            category="occurrence_missing_evidence",
            reason=f"Performed {occurrence.performed_at.date()} with no evidence linked",
            link=f"/occurrences/{occurrence.id}",
            control_id=occurrence.control_id,
            due_on=None,
        )
        for occurrence in occurrences
    ]


def _compute_evidence_stale_items(session: Session) -> list[ReadinessItem]:
    now = _naive_utc_now()
    snapshots = session.scalars(
        select(EvidenceSnapshot).where(
            EvidenceSnapshot.expires_at.is_not(None), EvidenceSnapshot.expires_at < now
        )
    ).all()
    return [
        ReadinessItem(
            category="evidence_stale",
            reason=f"Expired {_as_naive(snapshot.expires_at).date()}",
            link=f"/evidence/{snapshot.id}",
            control_id=None,  # an evidence snapshot may map to several controls/requirements
            due_on=_as_naive(snapshot.expires_at).date(),
        )
        for snapshot in snapshots
    ]


def _compute_control_test_failed_no_finding_items(session: Session) -> list[ReadinessItem]:
    has_finding = exists().where(Finding.source_test_id == ControlTest.id)
    tests = session.scalars(
        select(ControlTest).where(ControlTest.result == "exceptions_noted", ~has_finding)
    ).all()
    return [
        ReadinessItem(
            category="control_test_failed_no_finding",
            reason=f"Tested {test.performed_at.date()} with exceptions noted, no finding opened",
            link=f"/control-tests/{test.id}",
            control_id=test.control_id,
            due_on=None,
        )
        for test in tests
    ]


def _compute_finding_items(session: Session) -> list[ReadinessItem]:
    today = datetime.date.today()
    items = []
    needs_attention = session.scalars(
        select(Finding).where(Finding.status.in_(("open", "remediating")))
    ).all()
    for finding in needs_attention:
        if finding.due_date is None:
            reason = "No due date set"
        elif finding.due_date < today:
            reason = f"Overdue since {finding.due_date}"
        else:
            reason = f"Due {finding.due_date}"
        items.append(
            ReadinessItem(
                category="finding_needs_attention",
                reason=reason,
                link=f"/findings/{finding.id}",
                control_id=finding.control_id,
                due_on=finding.due_date,
            )
        )

    needs_retest = session.scalars(select(Finding).where(Finding.status == "retesting")).all()
    items.extend(
        ReadinessItem(
            category="finding_needs_retest",
            reason="Remediated and awaiting a recorded retest",
            link=f"/findings/{finding.id}",
            control_id=finding.control_id,
            due_on=finding.due_date,
        )
        for finding in needs_retest
    )

    missing_owner = session.scalars(
        select(Finding).where(Finding.status != "closed", Finding.owner_person_id.is_(None))
    ).all()
    items.extend(
        ReadinessItem(
            category="finding_missing_owner",
            reason="No owner assigned",
            link=f"/findings/{finding.id}",
            control_id=finding.control_id,
            due_on=None,
        )
        for finding in missing_owner
    )
    return items


def _compute_control_missing_owner_items(session: Session) -> list[ReadinessItem]:
    controls = session.scalars(
        select(InternalControl).where(InternalControl.owner_person_id.is_(None), InternalControl.owner == "")
    ).all()
    return [
        ReadinessItem(
            category="control_missing_owner",
            reason="No owner assigned",
            link=f"/controls/{control.id}",
            control_id=control.id,
            due_on=None,
        )
        for control in controls
    ]


def _compute_policy_review_overdue_items(session: Session) -> list[ReadinessItem]:
    today = datetime.date.today()
    policies = session.scalars(
        select(Policy).where(
            Policy.archived.is_(False), Policy.next_review_date.is_not(None), Policy.next_review_date < today
        )
    ).all()
    return [
        ReadinessItem(
            category="policy_review_overdue",
            reason=f"Review was due {policy.next_review_date}",
            link=f"/policies/{policy.id}",
            control_id=None,
            due_on=policy.next_review_date,
        )
        for policy in policies
    ]


def _control_ids_by_framework(session: Session) -> dict[str, set[str]]:
    """control_id -> set of framework_ids it maps to, via
    ControlRequirementMapping -> FrameworkRequirement — built once per
    call so framework filtering doesn't run a query per queue item."""
    rows = session.execute(
        select(ControlRequirementMapping.control_id, FrameworkRequirement.framework_id).join(
            FrameworkRequirement, ControlRequirementMapping.requirement_id == FrameworkRequirement.id
        )
    ).all()
    mapping: dict[str, set[str]] = {}
    for control_id, framework_id in rows:
        mapping.setdefault(control_id, set()).add(framework_id)
    return mapping


def compute_readiness_queue(session: Session, *, framework_id: str | None = None) -> list[ReadinessItem]:
    """The unified cross-domain readiness queue. Every item is computed
    exactly once regardless of `framework_id` — the filter only narrows
    which already-computed items are returned (RULES.md-equivalent "no
    duplicate items for one operational fact" from the issue). Items with
    `control_id=None` are framework-agnostic and always included."""
    items = [
        *_compute_occurrence_items(session),
        *_compute_occurrence_missing_evidence_items(session),
        *_compute_evidence_stale_items(session),
        *_compute_control_test_failed_no_finding_items(session),
        *_compute_finding_items(session),
        *_compute_control_missing_owner_items(session),
        *_compute_policy_review_overdue_items(session),
    ]

    if framework_id is None:
        matching = items
    else:
        control_frameworks = _control_ids_by_framework(session)
        matching = [
            item
            for item in items
            if item.control_id is None or framework_id in control_frameworks.get(item.control_id, set())
        ]

    def sort_key(item: ReadinessItem):
        return (item.due_on is None, item.due_on or datetime.date.max, item.category)

    return sorted(matching, key=sort_key)
