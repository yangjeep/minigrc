"""Organization-level framework/catalog version pinning (issue #43).

`FrameworkAdoption` (app/models.py) is a canonical projection over the
"framework_adoption" DomainEvent aggregate, never written to directly
outside the projector functions here — same discipline as
app/compliance_scope.py. One row per `Framework.catalog_family`: which
version of that catalog family the organization currently treats as
adopted.

This module deliberately never touches `ControlRequirementMapping`,
`FrameworkRequirement`, or `ControlPeriod` rows. Those already stay
permanently pinned to whichever `Framework` row they were created
against — `app/framework_catalog.py::reconcile_system_catalogs` never
mutates or removes an existing row, so an existing mapping's foreign key
never repoints itself to a newer catalog version. Upgrading an adoption
only moves which `Framework.id` is treated as "current" going forward;
it is not a migration/diffing engine and does not attempt to re-map or
copy anything (the issue's own explicit non-goal). See
docs/superpowers/specs/2026-08-20-issue43-catalog-version-pinning-design.md.
"""

from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.events import DomainEvent, ProjectorFn, append_and_project, rebuild_projection
from app.models import Framework, FrameworkAdoption, new_id


class FrameworkAdoptionError(ValueError):
    """Raised when caller-supplied adoption/upgrade input is invalid."""


def _project_adopted(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    session.add(
        FrameworkAdoption(
            id=event.aggregate_id,
            catalog_family=payload["catalog_family"],
            framework_id=payload["framework_id"],
            previous_framework_id=None,
            adopted_at=event.recorded_at,
            upgraded_at=None,
        )
    )
    # Explicit flush (session-wide autoflush=False) — a later
    # FrameworkAdoptionUpgraded projector on the same aggregate, in the
    # same call or a rebuild replay, needs session.get() to see this row.
    session.flush()


def _project_upgraded(session: Session, event: DomainEvent) -> None:
    payload = json.loads(event.payload_json)
    adoption = session.get(FrameworkAdoption, event.aggregate_id)
    adoption.previous_framework_id = payload["from_framework_id"]
    adoption.framework_id = payload["to_framework_id"]
    adoption.upgraded_at = event.recorded_at


FRAMEWORK_ADOPTION_PROJECTORS: dict[str, ProjectorFn] = {
    "FrameworkAdoptionAdopted": _project_adopted,
    "FrameworkAdoptionUpgraded": _project_upgraded,
}


def _reset_framework_adoptions(session: Session) -> None:
    session.execute(delete(FrameworkAdoption))


def rebuild_framework_adoption_projection(session: Session) -> None:
    """Wipe and deterministically rebuild the FrameworkAdoption projection
    from DomainEvent history — same shape as
    app/compliance_scope.py::rebuild_compliance_scope_projection."""
    rebuild_projection(
        session,
        FRAMEWORK_ADOPTION_PROJECTORS,
        aggregate_type="framework_adoption",
        reset=_reset_framework_adoptions,
    )


def get_adoption(session: Session, catalog_family: str) -> FrameworkAdoption | None:
    return session.scalar(select(FrameworkAdoption).where(FrameworkAdoption.catalog_family == catalog_family))


def list_adoptions(session: Session) -> list[FrameworkAdoption]:
    return list(session.scalars(select(FrameworkAdoption).order_by(FrameworkAdoption.catalog_family)).all())


def adopt_framework(
    session: Session,
    framework: Framework,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
) -> FrameworkAdoption:
    """First-time adoption of `framework`'s catalog family. Idempotent if
    the family is already adopted to this exact framework (a safe no-op —
    e.g. re-running startup reconciliation). Raises if the family is
    already adopted to a *different* framework: moving to a different
    version of an already-adopted family is a deliberate upgrade
    (`upgrade_framework_adoption`), not a second "first adoption."
    """
    if framework.catalog_family is None:
        raise FrameworkAdoptionError(
            f"Cannot adopt framework {framework.id!r}: it has no catalog_family "
            "(not part of a versioned system catalog)."
        )

    existing = get_adoption(session, framework.catalog_family)
    if existing is not None:
        if existing.framework_id == framework.id:
            return existing
        raise FrameworkAdoptionError(
            f"catalog_family {framework.catalog_family!r} is already adopted "
            f"(framework_id={existing.framework_id!r}) — use upgrade_framework_adoption "
            "to move to a different version."
        )

    stored_event = append_and_project(
        session,
        FRAMEWORK_ADOPTION_PROJECTORS,
        aggregate_type="framework_adoption",
        aggregate_id=new_id(),
        event_type="FrameworkAdoptionAdopted",
        payload={"catalog_family": framework.catalog_family, "framework_id": framework.id},
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return session.get(FrameworkAdoption, stored_event.aggregate_id)


def upgrade_framework_adoption(
    session: Session,
    adoption: FrameworkAdoption,
    to_framework: Framework,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> FrameworkAdoption:
    """The explicit, deliberate upgrade action the issue asks for: move
    `adoption` from its current framework to `to_framework`, a *different*
    version of the *same* catalog family. Never touches any
    ControlRequirementMapping/FrameworkRequirement/ControlPeriod row —
    see this module's docstring."""
    if to_framework.catalog_family != adoption.catalog_family:
        raise FrameworkAdoptionError(
            f"Cannot upgrade catalog_family {adoption.catalog_family!r} to framework "
            f"{to_framework.id!r}, whose catalog_family is {to_framework.catalog_family!r} — "
            "an upgrade must stay within the same catalog family."
        )
    if to_framework.id == adoption.framework_id:
        raise FrameworkAdoptionError(
            f"Framework {to_framework.id!r} is already the adopted version for catalog_family "
            f"{adoption.catalog_family!r} — nothing to upgrade."
        )
    if not to_framework.is_active:
        # Validated here, not only in list_upgrade_candidates' filtered
        # dropdown — a crafted request must not be able to upgrade to an
        # inactive/deactivated framework just because the UI's own
        # candidate list would never have offered it.
        raise FrameworkAdoptionError(
            f"Framework {to_framework.id!r} is inactive — cannot upgrade to an inactive framework."
        )

    stored_event = append_and_project(
        session,
        FRAMEWORK_ADOPTION_PROJECTORS,
        aggregate_type="framework_adoption",
        aggregate_id=adoption.id,
        event_type="FrameworkAdoptionUpgraded",
        payload={
            "catalog_family": adoption.catalog_family,
            "from_framework_id": adoption.framework_id,
            "to_framework_id": to_framework.id,
        },
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return session.get(FrameworkAdoption, stored_event.aggregate_id)


def list_upgrade_candidates(session: Session, adoption: FrameworkAdoption) -> list[Framework]:
    """Other active frameworks in the same catalog family that `adoption`
    could upgrade to — everything except the currently-adopted one."""
    return list(
        session.scalars(
            select(Framework)
            .where(
                Framework.catalog_family == adoption.catalog_family,
                Framework.id != adoption.framework_id,
                Framework.is_active.is_(True),
            )
            .order_by(Framework.version)
        ).all()
    )
