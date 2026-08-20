"""Control-definition version lifecycle and event projectors (issue
#42). A plain module — matching the shape of app/policy_lifecycle.py,
which this deliberately mirrors rather than reinventing a second
versioning mechanism.

Unlike PolicyVersion (#31), InternalControlVersion has no pre-existing
plain-insert use to preserve: it is a brand-new table, fully
event-sourced from creation (same reasoning EvidenceArtifactVersion's
docstring gives for its own table, #32) — the *first* event for a
version, InternalControlVersionDrafted, is what creates the row, not a
separate plain insert elsewhere.

InternalControl's own plain columns (name/description/status/
review_frequency) are never synced from this module — see
InternalControlVersion's docstring.

See docs/superpowers/specs/2026-08-20-issue42-control-definition-version-lifecycle-design.md
for the full design.
"""

from __future__ import annotations

import datetime
import json

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import ControlOccurrence, InternalControl, InternalControlVersion, new_id


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _project_drafted(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        InternalControlVersion(
            id=event.aggregate_id,
            control_id=payload["control_id"],
            version_number=payload["version_number"],
            name=payload["name"],
            description=payload["description"],
            status_snapshot=payload["status_snapshot"],
            review_frequency_snapshot=payload["review_frequency_snapshot"],
            mapped_requirement_ids_json=json.dumps(payload["mapped_requirement_ids"]),
            lifecycle_status="draft",
            drafted_at=event.recorded_at,
            drafted_by=event.actor_id,
        )
    )
    # Explicit flush — a later event for this same aggregate (in the
    # same append_and_project call or a later rebuild_projection pass)
    # calls session.get() on this row before anything else would
    # trigger a flush (session-wide autoflush=False, app/db.py). Same
    # discipline as app/control_occurrences.py::_project_materialized.
    session.flush()


def _project_submitted_for_review(session: Session, event: DomainEvent) -> None:
    version = session.get(InternalControlVersion, event.aggregate_id)
    version.lifecycle_status = "in_review"
    version.submitted_by = event.actor_id
    version.submitted_at = event.occurred_at


def _project_reviewed(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    version = session.get(InternalControlVersion, event.aggregate_id)
    version.reviewed_by = event.actor_id
    version.reviewed_at = event.occurred_at
    version.review_decision = payload["decision"]
    version.review_comment = payload.get("comment", "")
    version.lifecycle_status = "approved" if payload["decision"] == "approved" else "draft"


def _project_made_effective(session: Session, event: DomainEvent) -> None:
    version = session.get(InternalControlVersion, event.aggregate_id)
    version.lifecycle_status = "effective"
    version.effective_at = event.occurred_at
    # Flush immediately — found necessary via adversarial self-review
    # (see design doc §7.1): unlike app/policy_lifecycle.py's
    # PolicyVersion (whose row pre-exists every lifecycle event, so
    # *no* projector in that module ever flushes, leaving nothing to
    # interleave with), this module's _project_drafted explicitly
    # flushes to create each new row. During rebuild replay, that
    # unrelated flush can land between this aggregate's own Superseded
    # and a different aggregate's MadeEffective mutation, batching
    # them into one later flush whose internal statement order is not
    # guaranteed to apply "superseded" before "effective" —
    # momentarily violating
    # uq_internal_control_version_one_effective_per_control. Flushing
    # each transition-critical mutation as it is individually applied
    # removes any dependency on flush-batching order across aggregates.
    session.flush()


def _project_superseded(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    version = session.get(InternalControlVersion, event.aggregate_id)
    version.lifecycle_status = "superseded"
    version.superseded_at = event.occurred_at
    version.superseded_by_version_id = payload["superseded_by_version_id"]
    # Flush immediately — see _project_made_effective's comment above.
    session.flush()


def _project_withdrawn(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    version = session.get(InternalControlVersion, event.aggregate_id)
    version.lifecycle_status = "withdrawn"
    version.withdrawn_at = event.occurred_at
    version.withdrawn_reason = payload.get("reason", "")


INTERNAL_CONTROL_VERSION_PROJECTORS: dict[str, ProjectorFn] = {
    "InternalControlVersionDrafted": _project_drafted,
    "InternalControlVersionSubmittedForReview": _project_submitted_for_review,
    "InternalControlVersionReviewed": _project_reviewed,
    "InternalControlVersionMadeEffective": _project_made_effective,
    "InternalControlVersionSuperseded": _project_superseded,
    "InternalControlVersionWithdrawn": _project_withdrawn,
}


def _reset_internal_control_versions(session: Session) -> None:
    # Unlike PolicyVersion's reset (which only resets lifecycle
    # columns — the row exists independent of events there), every
    # InternalControlVersion row only exists via events, so rebuild
    # deletes and replays from scratch, same as
    # app/control_occurrences.py's reset.
    #
    # ControlOccurrence.control_definition_version_id FK-references
    # this table, so it must be cleared first or the delete below
    # violates the foreign key — same "child-before-parent" reasoning
    # app/evidence_repository.py::_reset_evidence_artifact_versions
    # documents for its own analogous case. Nulled, not deleted:
    # ControlOccurrence rows themselves are not owned by this rebuild
    # and must not be destroyed as a side effect of it. Not
    # re-populated by this rebuild either — a full recovery must also
    # call rebuild_control_occurrence_projection afterward, which
    # restores each occurrence's control_definition_version_id from
    # its own original event payload once the referenced version rows
    # exist again.
    session.execute(update(ControlOccurrence).values(control_definition_version_id=None))
    session.execute(delete(InternalControlVersion))


def rebuild_internal_control_version_projection(session: Session) -> None:
    """Wipe and deterministically rebuild every InternalControlVersion
    from DomainEvent history. Admin/ops recovery path — proves
    app/events.py's rebuild contract holds for this domain too."""
    rebuild_projection(
        session,
        INTERNAL_CONTROL_VERSION_PROJECTORS,
        aggregate_type="internal_control_version",
        reset=_reset_internal_control_versions,
    )


def draft_control_definition_version(
    session: Session,
    control: InternalControl,
    *,
    name: str | None = None,
    description: str | None = None,
    status_snapshot: str | None = None,
    review_frequency_snapshot: str | None = None,
    mapped_requirement_ids: list[str] | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> InternalControlVersion:
    """Create a new draft version of `control`'s definition.

    Every snapshot field defaults to the control's own CURRENT value
    when omitted — makes "snapshot exactly what's on the row right
    now" (the migration backfill's and starter-control-generation's
    use case) a one-line call, while still allowing an explicit
    override for a real future revision proposal."""
    # This app's session factory uses expire_on_commit=False, so a
    # long-lived `control` object's `.versions`/`.mappings`
    # relationships (read below for the next version number and the
    # default mapped-requirement snapshot) can go stale across
    # repeated drafts against the same control within one session —
    # the same class of bug found and fixed in
    # app/connectors/google_drive_capture.py::run_google_drive_capture
    # (issue #28). Refreshing here makes this function robust to that
    # call pattern instead of pushing the burden onto every caller.
    session.refresh(control)
    name = control.name if name is None else name
    description = control.description if description is None else description
    status_snapshot = control.status if status_snapshot is None else status_snapshot
    review_frequency_snapshot = (
        control.review_frequency if review_frequency_snapshot is None else review_frequency_snapshot
    )
    mapped_requirement_ids = (
        [mapping.requirement_id for mapping in control.mappings]
        if mapped_requirement_ids is None
        else mapped_requirement_ids
    )

    next_version_number = max((v.version_number for v in control.versions), default=0) + 1
    event = append_and_project(
        session,
        INTERNAL_CONTROL_VERSION_PROJECTORS,
        aggregate_type="internal_control_version",
        aggregate_id=new_id(),
        event_type="InternalControlVersionDrafted",
        payload={
            "control_id": control.id,
            "version_number": next_version_number,
            "name": name,
            "description": description,
            "status_snapshot": status_snapshot,
            "review_frequency_snapshot": review_frequency_snapshot,
            "mapped_requirement_ids": mapped_requirement_ids,
        },
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return session.get(InternalControlVersion, event.aggregate_id)


def submit_for_review(
    session: Session,
    version: InternalControlVersion,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> InternalControlVersion:
    if version.lifecycle_status != "draft":
        raise ValueError(
            f"Cannot submit for review from lifecycle_status={version.lifecycle_status!r} (must be 'draft')"
        )
    append_and_project(
        session,
        INTERNAL_CONTROL_VERSION_PROJECTORS,
        aggregate_type="internal_control_version",
        aggregate_id=version.id,
        event_type="InternalControlVersionSubmittedForReview",
        payload={},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return version


def review_version(
    session: Session,
    version: InternalControlVersion,
    *,
    decision: str,
    comment: str = "",
    actor_type: str = "user",
    actor_id: str | None = None,
) -> InternalControlVersion:
    if version.lifecycle_status != "in_review":
        raise ValueError(
            f"Cannot review from lifecycle_status={version.lifecycle_status!r} (must be 'in_review')"
        )
    if decision not in ("approved", "rejected"):
        raise ValueError(f"Invalid review decision: {decision!r}")
    if decision == "rejected" and not comment.strip():
        raise ValueError("A comment explaining the rejection is required")

    append_and_project(
        session,
        INTERNAL_CONTROL_VERSION_PROJECTORS,
        aggregate_type="internal_control_version",
        aggregate_id=version.id,
        event_type="InternalControlVersionReviewed",
        payload={"decision": decision, "comment": comment},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return version


def make_version_effective(
    session: Session,
    version: InternalControlVersion,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> InternalControlVersion:
    """Make `version` the control's effective definition. If a
    different version of the same control is currently effective, it
    is superseded first (same "one command, two aggregate events,
    supersede-before-effective" shape as
    app/policy_lifecycle.py::make_version_effective — reversing the
    order would momentarily need two "effective" rows for the same
    control to coexist). Each projector
    (_project_superseded/_project_made_effective) flushes its own
    mutation immediately rather than deferring to one flush here at
    the end — unlike PolicyVersion's projectors, this module's
    _project_drafted needs its own flush to create each new row, and
    during rebuild replay that unrelated flush can otherwise batch an
    unrelated aggregate's pending "effective" mutation together with
    this pair's, in an order the partial unique index isn't guaranteed
    to like (see _project_made_effective's comment)."""
    if version.lifecycle_status != "approved":
        raise ValueError(
            f"Cannot make effective from lifecycle_status={version.lifecycle_status!r} (must be 'approved')"
        )

    previous_effective = next(
        (v for v in version.control.versions if v.lifecycle_status == "effective" and v.id != version.id),
        None,
    )

    if previous_effective is not None:
        append_and_project(
            session,
            INTERNAL_CONTROL_VERSION_PROJECTORS,
            aggregate_type="internal_control_version",
            aggregate_id=previous_effective.id,
            event_type="InternalControlVersionSuperseded",
            payload={"superseded_by_version_id": version.id},
            actor_type=actor_type,
            actor_id=actor_id,
        )
    append_and_project(
        session,
        INTERNAL_CONTROL_VERSION_PROJECTORS,
        aggregate_type="internal_control_version",
        aggregate_id=version.id,
        event_type="InternalControlVersionMadeEffective",
        payload={},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return version


def withdraw_version(
    session: Session,
    version: InternalControlVersion,
    *,
    reason: str = "",
    actor_type: str = "user",
    actor_id: str | None = None,
    occurred_at: datetime.datetime | None = None,
) -> InternalControlVersion:
    """Withdraw a version — usable from any non-terminal state
    (draft/in_review/approved/effective). Same terminal action from
    this app's perspective as app/policy_lifecycle.py::withdraw_version."""
    if version.lifecycle_status not in ("draft", "in_review", "approved", "effective"):
        raise ValueError(f"Cannot withdraw a version that is already {version.lifecycle_status!r}")

    append_and_project(
        session,
        INTERNAL_CONTROL_VERSION_PROJECTORS,
        aggregate_type="internal_control_version",
        aggregate_id=version.id,
        event_type="InternalControlVersionWithdrawn",
        payload={"reason": reason},
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return version


def bootstrap_initial_effective_version(
    session: Session, control: InternalControl, *, actor_type: str = "system", actor_id: str | None = None
) -> InternalControlVersion:
    """draft -> submit -> approve -> effective in one call, tagged
    actor_type="system" by default. The sequence
    app/starter_controls.py uses for every newly onboarded control so
    it starts with a real effective version — the migration backfill
    (issue #42) only covers controls that exist at migration time,
    not ones created afterward."""
    version = draft_control_definition_version(session, control, actor_type=actor_type, actor_id=actor_id)
    submit_for_review(session, version, actor_type=actor_type, actor_id=actor_id)
    review_version(session, version, decision="approved", actor_type=actor_type, actor_id=actor_id)
    make_version_effective(session, version, actor_type=actor_type, actor_id=actor_id)
    return version
