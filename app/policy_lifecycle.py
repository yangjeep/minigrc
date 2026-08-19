"""Policy-version approval lifecycle and event projectors (issue #31).

A plain module — matching the shape of app/control_occurrences.py. Builds
on app/events.py (issue #22): the lifecycle columns on `PolicyVersion`
(app/models.py) are a canonical projection over the "policy_version"
DomainEvent aggregate, never written to directly outside the projector
functions here. The row's own existence and its file-capture columns
(sha256, byte_size, uploader, ...) are a separate, plain, immutable-by-
convention fact — never event-sourced; see PolicyVersion's docstring.

See docs/superpowers/specs/2026-08-19-issue31-policy-lifecycle-design.md
for the full design.
"""

from __future__ import annotations

import datetime
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import PolicyVersion


def _project_submitted_for_review(session: Session, event: DomainEvent) -> None:
    version = session.get(PolicyVersion, event.aggregate_id)
    version.lifecycle_status = "in_review"
    version.submitted_by = event.actor_id
    version.submitted_at = event.occurred_at


def _project_reviewed(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    version = session.get(PolicyVersion, event.aggregate_id)
    version.reviewed_by = event.actor_id
    version.reviewed_at = event.occurred_at
    version.review_decision = payload["decision"]
    version.review_comment = payload.get("comment", "")
    version.lifecycle_status = "approved" if payload["decision"] == "approved" else "draft"


def _project_made_effective(session: Session, event: DomainEvent) -> None:
    version = session.get(PolicyVersion, event.aggregate_id)
    version.lifecycle_status = "effective"
    version.effective_at = event.occurred_at
    # No flush here, deliberately — see make_version_effective's
    # docstring for why: flushing mid-projector would check
    # uq_policy_version_one_effective_per_policy against a momentarily
    # inconsistent state during rebuild replay (see
    # rebuild_policy_version_lifecycle_projection), where cross-aggregate
    # event order is only deterministic, not guaranteed to match the
    # live command's own append order for two causally-linked events on
    # different aggregates (app/events.py::rebuild_projection's own
    # docstring calls this out for tied recorded_at values).


def _project_superseded(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    version = session.get(PolicyVersion, event.aggregate_id)
    version.lifecycle_status = "superseded"
    version.superseded_at = event.occurred_at
    version.superseded_by_version_id = payload["superseded_by_version_id"]


def _project_withdrawn(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    version = session.get(PolicyVersion, event.aggregate_id)
    version.lifecycle_status = "withdrawn"
    version.withdrawn_at = event.occurred_at
    version.withdrawn_reason = payload.get("reason", "")


POLICY_VERSION_PROJECTORS: dict[str, ProjectorFn] = {
    "PolicyVersionSubmittedForReview": _project_submitted_for_review,
    "PolicyVersionReviewed": _project_reviewed,
    "PolicyVersionMadeEffective": _project_made_effective,
    "PolicyVersionSuperseded": _project_superseded,
    "PolicyVersionWithdrawn": _project_withdrawn,
}

_LIFECYCLE_DEFAULTS = {
    "lifecycle_status": "draft",
    "submitted_by": None,
    "submitted_at": None,
    "reviewed_by": None,
    "reviewed_at": None,
    "review_decision": None,
    "review_comment": "",
    "effective_at": None,
    "superseded_at": None,
    "superseded_by_version_id": None,
    "withdrawn_at": None,
    "withdrawn_reason": "",
}


def _reset_policy_version_lifecycle(session: Session) -> None:
    # Unlike app/control_occurrences.py's reset (which deletes rows —
    # ControlOccurrence rows only exist via events), PolicyVersion rows
    # exist independent of lifecycle events: only these columns reset.
    for version in session.scalars(select(PolicyVersion)).all():
        for column, default in _LIFECYCLE_DEFAULTS.items():
            setattr(version, column, default)
    session.flush()


def rebuild_policy_version_lifecycle_projection(session: Session) -> None:
    """Wipe and deterministically rebuild every PolicyVersion's lifecycle
    columns from DomainEvent history. Admin/ops recovery path — proves
    app/events.py's rebuild contract holds for this domain too."""
    rebuild_projection(
        session,
        POLICY_VERSION_PROJECTORS,
        aggregate_type="policy_version",
        reset=_reset_policy_version_lifecycle,
    )


def submit_for_review(
    session: Session, version: PolicyVersion, *, actor_type: str = "user", actor_id: str | None = None
) -> PolicyVersion:
    if version.lifecycle_status != "draft":
        raise ValueError(
            f"Cannot submit for review from lifecycle_status={version.lifecycle_status!r} (must be 'draft')"
        )
    append_and_project(
        session,
        POLICY_VERSION_PROJECTORS,
        aggregate_type="policy_version",
        aggregate_id=version.id,
        event_type="PolicyVersionSubmittedForReview",
        payload={},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return version


def review_version(
    session: Session,
    version: PolicyVersion,
    *,
    decision: str,
    comment: str = "",
    actor_type: str = "user",
    actor_id: str | None = None,
) -> PolicyVersion:
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
        POLICY_VERSION_PROJECTORS,
        aggregate_type="policy_version",
        aggregate_id=version.id,
        event_type="PolicyVersionReviewed",
        payload={"decision": decision, "comment": comment},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return version


def make_version_effective(
    session: Session, version: PolicyVersion, *, actor_type: str = "user", actor_id: str | None = None
) -> PolicyVersion:
    """Make `version` the policy's effective version. If a different
    version of the same policy is currently effective, it is
    superseded in the same transaction (one command, two aggregate
    events — same "one command touches two aggregates" shape as
    app/routers/occurrences.py::perform).

    The `previous_effective` lookup below is a best-effort convenience
    for the common single-actor case, not the correctness guarantee —
    two concurrent requests each making a different already-approved
    version effective could both pass this check (neither sees the
    other's uncommitted write). `uq_policy_version_one_effective_per_policy`
    (models.py) is the real guard: the explicit flush below (after both
    events, not per-projector — see _project_made_effective's docstring)
    raises IntegrityError against the *final* state if it's ever
    inconsistent, which the caller (app/routers/policies.py) must catch
    and turn into a clean rejection, not a raw 500."""
    if version.lifecycle_status != "approved":
        raise ValueError(
            f"Cannot make effective from lifecycle_status={version.lifecycle_status!r} (must be 'approved')"
        )

    previous_effective = next(
        (v for v in version.policy.versions if v.lifecycle_status == "effective" and v.id != version.id),
        None,
    )

    # Supersede the previous effective version FIRST, before making the
    # new one effective — reversing this order would momentarily need
    # two "effective" rows for the same policy to coexist within this
    # same call, which uq_policy_version_one_effective_per_policy
    # (correctly) rejects even for a single, non-concurrent caller.
    if previous_effective is not None:
        append_and_project(
            session,
            POLICY_VERSION_PROJECTORS,
            aggregate_type="policy_version",
            aggregate_id=previous_effective.id,
            event_type="PolicyVersionSuperseded",
            payload={"superseded_by_version_id": version.id},
            actor_type=actor_type,
            actor_id=actor_id,
        )
    append_and_project(
        session,
        POLICY_VERSION_PROJECTORS,
        aggregate_type="policy_version",
        aggregate_id=version.id,
        event_type="PolicyVersionMadeEffective",
        payload={},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    # Flush once, after both events — checks
    # uq_policy_version_one_effective_per_policy against the final,
    # self-consistent state (never a momentary two-effective one).
    session.flush()
    return version


def withdraw_version(
    session: Session,
    version: PolicyVersion,
    *,
    reason: str = "",
    actor_type: str = "user",
    actor_id: str | None = None,
    occurred_at: datetime.datetime | None = None,
) -> PolicyVersion:
    """Withdraw a version — usable from any non-terminal state
    (draft/in_review/approved/effective). Covers both "this draft was
    abandoned before approval" and "this was the effective version and
    the policy is being retired with no replacement" — the same
    terminal action from this app's perspective (see design doc §4)."""
    if version.lifecycle_status not in ("draft", "in_review", "approved", "effective"):
        raise ValueError(f"Cannot withdraw a version that is already {version.lifecycle_status!r}")

    append_and_project(
        session,
        POLICY_VERSION_PROJECTORS,
        aggregate_type="policy_version",
        aggregate_id=version.id,
        event_type="PolicyVersionWithdrawn",
        payload={"reason": reason},
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return version
