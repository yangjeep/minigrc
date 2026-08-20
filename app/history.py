"""Auditor/operator-facing history & point-in-time reconstruction (issue
#45) — a read-only exploration surface over `app/events.py`'s authoritative
event store, distinct from #33/#14's current-state readiness dashboard and
#34's static audit-package export.

Two capabilities, proven against two representative event-backed domains
per the issue's own "smallest coherent slice" instruction:

- `get_event_history` — a generic, per-aggregate chronological event
  listing (works for any aggregate_type: policy_version,
  control_occurrence, ...).
- `reconstruct_policy_as_of` — "what was true about this policy's
  versions as of date X," replayed from the policy_version event history
  bounded by an `as_of` cutoff.

`reconstruct_policy_as_of` deliberately does NOT reuse
`app/events.py::rebuild_projection` or run inside a nested
transaction/SAVEPOINT that gets rolled back afterward. This repo already
has a known, real pysqlite/SAVEPOINT interaction hazard (issue #65) — a
history/point-in-time view is exactly the kind of read path that must
never risk touching the real, live PolicyVersion projection table, even
transiently. Instead, this module replays events into a **pure, in-memory
dataclass snapshot** that mirrors app/policy_lifecycle.py's projector
transitions exactly, but never issues a single write to the database.
Some duplication of that transition logic is the deliberate cost of that
safety property, not an oversight.
"""

from __future__ import annotations

import dataclasses
import datetime
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import DomainEvent
from app.models import Policy, PolicyVersion


def get_event_history(session: Session, *, aggregate_type: str, aggregate_id: str) -> list[DomainEvent]:
    """Every DomainEvent for one aggregate, in real chronological order —
    the generic "what happened to this object, when, and by whom" view."""
    return list(
        session.scalars(
            select(DomainEvent)
            .where(DomainEvent.aggregate_type == aggregate_type, DomainEvent.aggregate_id == aggregate_id)
            .order_by(DomainEvent.recorded_at, DomainEvent.aggregate_sequence)
        ).all()
    )


@dataclasses.dataclass(frozen=True)
class PolicyVersionSnapshot:
    """An in-memory reconstruction of one PolicyVersion's lifecycle
    columns as of a past cutoff — same fields as the live projection
    (app/models.py::PolicyVersion), never written to any table."""

    version_id: str
    version_number: int
    lifecycle_status: str
    submitted_by: str | None = None
    submitted_at: datetime.datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime.datetime | None = None
    review_decision: str | None = None
    review_comment: str = ""
    effective_at: datetime.datetime | None = None
    superseded_at: datetime.datetime | None = None
    superseded_by_version_id: str | None = None
    withdrawn_at: datetime.datetime | None = None
    withdrawn_reason: str = ""


def _apply_submitted_for_review(snapshot: PolicyVersionSnapshot, event: DomainEvent) -> PolicyVersionSnapshot:
    return dataclasses.replace(
        snapshot, lifecycle_status="in_review", submitted_by=event.actor_id, submitted_at=event.occurred_at
    )


def _apply_reviewed(snapshot: PolicyVersionSnapshot, event: DomainEvent) -> PolicyVersionSnapshot:
    payload = json.loads(event.payload_json)
    return dataclasses.replace(
        snapshot,
        reviewed_by=event.actor_id,
        reviewed_at=event.occurred_at,
        review_decision=payload["decision"],
        review_comment=payload.get("comment", ""),
        lifecycle_status="approved" if payload["decision"] == "approved" else "draft",
    )


def _apply_made_effective(snapshot: PolicyVersionSnapshot, event: DomainEvent) -> PolicyVersionSnapshot:
    return dataclasses.replace(snapshot, lifecycle_status="effective", effective_at=event.occurred_at)


def _apply_superseded(snapshot: PolicyVersionSnapshot, event: DomainEvent) -> PolicyVersionSnapshot:
    payload = json.loads(event.payload_json)
    return dataclasses.replace(
        snapshot,
        lifecycle_status="superseded",
        superseded_at=event.occurred_at,
        superseded_by_version_id=payload["superseded_by_version_id"],
    )


def _apply_withdrawn(snapshot: PolicyVersionSnapshot, event: DomainEvent) -> PolicyVersionSnapshot:
    payload = json.loads(event.payload_json)
    return dataclasses.replace(
        snapshot,
        lifecycle_status="withdrawn",
        withdrawn_at=event.occurred_at,
        withdrawn_reason=payload.get("reason", ""),
    )


# Mirrors app/policy_lifecycle.py::POLICY_VERSION_PROJECTORS exactly, one
# applier per event_type, in-memory only — see this module's docstring.
_SNAPSHOT_APPLIERS = {
    "PolicyVersionSubmittedForReview": _apply_submitted_for_review,
    "PolicyVersionReviewed": _apply_reviewed,
    "PolicyVersionMadeEffective": _apply_made_effective,
    "PolicyVersionSuperseded": _apply_superseded,
    "PolicyVersionWithdrawn": _apply_withdrawn,
}


def reconstruct_policy_as_of(
    session: Session, policy: Policy, *, as_of: datetime.datetime
) -> list[PolicyVersionSnapshot]:
    """Every version of `policy`, reconstructed to its lifecycle state as
    of `as_of` — replaying only events whose `occurred_at` is on or before
    the cutoff. A version created after `as_of` is simply omitted (it did
    not exist yet as of that date, matching PolicyVersion's own file-
    capture columns, which are a plain immutable-by-convention fact, not
    part of the replayed lifecycle state).

    Queries `PolicyVersion` explicitly by `policy_id` rather than reading
    `policy.versions` — that relationship, once lazily loaded on a
    long-lived session, would return a collection cached from the first
    access and miss any version added to the policy afterward in the
    same session.
    """
    versions = session.scalars(select(PolicyVersion).where(PolicyVersion.policy_id == policy.id)).all()
    version_ids_and_numbers = {v.id: v.version_number for v in versions if v.created_at <= as_of}
    if not version_ids_and_numbers:
        return []

    events = session.scalars(
        select(DomainEvent)
        .where(
            DomainEvent.aggregate_type == "policy_version",
            DomainEvent.aggregate_id.in_(version_ids_and_numbers.keys()),
            DomainEvent.occurred_at <= as_of,
        )
        .order_by(DomainEvent.recorded_at, DomainEvent.aggregate_sequence)
    ).all()

    snapshots = {
        version_id: PolicyVersionSnapshot(
            version_id=version_id, version_number=version_number, lifecycle_status="draft"
        )
        for version_id, version_number in version_ids_and_numbers.items()
    }
    for event in events:
        applier = _SNAPSHOT_APPLIERS[event.event_type]
        snapshots[event.aggregate_id] = applier(snapshots[event.aggregate_id], event)

    return sorted(snapshots.values(), key=lambda snapshot: snapshot.version_number)
