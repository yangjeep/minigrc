"""PII redaction for offboarded people/users, reconciled with immutable
compliance history (issue #47).

The reconciliation this module rests on: no `DomainEvent` payload ever
embeds a raw `Person`/`User` PII value (confirmed by inspection —
every reference across the entire event-sourced domain is a stable
`*_person_id`/`actor_id` foreign key, never a copied email or name).
`Person` and `User` are already plain, ordinary mutable reference
tables (`Person.in_scope` already changes via a normal UPDATE +
`record_audit_event` — the same pattern this module uses, not a new
one). Redacting `Person.email`/`display_name`/`external_id` in place,
with `Person.id` staying stable, automatically redacts what every
historical `*_person_id`-referencing view displays — no event, no
projector change, no `DomainEvent` row is ever touched by this module.

The one real gap this module closes: ten plain `String` columns across
the schema freeze a `User.email` value verbatim at write time
(`AuditEvent.actor`, `PolicyVersion.uploader`, and eight `created_by`/
`last_reviewed_by` columns — see `_ACTOR_STRING_COLUMNS`). None of
these are event-sourced or ORM-immutability-guarded (that guard is
registered specifically against `DomainEvent`, confirmed in
app/events.py) — redacting them is a deliberate, scoped, audited
mutation of already-plain-mutable convenience/log columns.

See docs/superpowers/specs/2026-08-20-issue47-pii-retention-redaction-design.md
for the full design, including why this is not a stop-condition-worthy
architecture conflict.
"""

from __future__ import annotations

import dataclasses
import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import (
    AuditEvent,
    ControlPeriod,
    ControlTestPopulation,
    ControlTestSample,
    ExternalConnection,
    ImportJob,
    Job,
    Person,
    PolicyVersion,
    RequirementAssessment,
    Secret,
    User,
)

# Every plain String column, across the whole schema, confirmed by
# grep to freeze a User.email value verbatim at write time. A
# declarative registry, not ten hand-written UPDATE statements: every
# entry gets the identical treatment. Justified as a genuine
# abstraction, not a speculative one — ten real, already-existing
# columns need the exact same operation today.
_ACTOR_STRING_COLUMNS: tuple[tuple[type, str], ...] = (
    (AuditEvent, "actor"),
    (PolicyVersion, "uploader"),
    (ControlPeriod, "created_by"),
    (ControlTestPopulation, "created_by"),
    (ControlTestSample, "created_by"),
    (RequirementAssessment, "last_reviewed_by"),
    (Secret, "created_by"),
    (ExternalConnection, "created_by"),
    (Job, "created_by"),
    (ImportJob, "created_by"),
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


@dataclasses.dataclass(frozen=True)
class PersonRedactionReport:
    person_id: str
    already_redacted: bool


def redact_person_pii(session: Session, person: Person, *, actor: User) -> PersonRedactionReport:
    """Redact a departed Person's own directory-entry PII.

    Requires `employment_status == "departed"` — refuses to redact
    anyone not already marked departed, so this can never accidentally
    strip a current employee's contact details. Idempotent: a second
    call on an already-redacted person is a safe no-op, not a
    duplicate redaction, distinguished via `pii_redacted_at` rather
    than inferred from the email's own shape.

    Never touches any `*_person_id` foreign key or any `DomainEvent`
    row — `person.id` stays exactly as it was, so every historical
    reference (ControlOccurrence.responsible_person_id,
    InternalControl.owner_person_id, etc.) keeps resolving correctly,
    now to the redacted display values.
    """
    if person.pii_redacted_at is not None:
        return PersonRedactionReport(person_id=person.id, already_redacted=True)
    if person.employment_status != "departed":
        raise ValueError(
            f"Cannot redact PII for person {person.id!r}: employment_status is "
            f"{person.employment_status!r}, must be 'departed'."
        )

    person.email = f"redacted-person-{person.id}@redacted.invalid"
    person.display_name = "Redacted Person"
    person.external_id = None
    person.pii_redacted_at = _now()

    record_audit_event(
        session,
        entity_type="person",
        entity_id=person.id,
        action="redact_pii",
        detail=f"Redacted PII for departed person '{person.id}' (email/display_name/external_id).",
        actor=actor.email,
    )
    return PersonRedactionReport(person_id=person.id, already_redacted=False)


@dataclasses.dataclass(frozen=True)
class UserRedactionReport:
    user_id: str
    already_redacted: bool
    rows_updated_by_table: dict[str, int]


def redact_user_pii(session: Session, user: User, *, actor: User) -> UserRedactionReport:
    """Redact a departed User's login email — on the User row itself,
    and retroactively across every plain-string actor/creator column
    that historically froze it verbatim (see `_ACTOR_STRING_COLUMNS`).

    Requires `status == "disabled"` — refuses to redact an active
    login, which would both break their ability to sign in and rewrite
    audit attribution for someone still working. Idempotent via
    `pii_redacted_at`.

    The replacement pseudonym (`redacted-user-<id>@redacted.invalid`)
    deliberately embeds the real, stable, never-deleted `user.id` — an
    authorized admin can still resolve "this action was taken by
    account <id>, whose PII has since been redacted," matching the
    issue's own "preserve their stable identity reference" requirement,
    without exposing the actual email anywhere these columns are
    displayed. Never touches any `*_id`/`actor_id` column or any
    `DomainEvent` row.
    """
    if user.pii_redacted_at is not None:
        return UserRedactionReport(user_id=user.id, already_redacted=True, rows_updated_by_table={})
    if user.status != "disabled":
        raise ValueError(
            f"Cannot redact PII for user {user.id!r}: status is {user.status!r}, must be 'disabled'."
        )

    old_email = user.email
    new_email = f"redacted-user-{user.id}@redacted.invalid"

    rows_updated_by_table: dict[str, int] = {}
    for model, column in _ACTOR_STRING_COLUMNS:
        result = session.execute(
            update(model).where(getattr(model, column) == old_email).values(**{column: new_email})
        )
        if result.rowcount:
            rows_updated_by_table[model.__tablename__] = result.rowcount

    user.email = new_email
    user.pii_redacted_at = _now()

    record_audit_event(
        session,
        entity_type="user",
        entity_id=user.id,
        action="redact_pii",
        detail=(
            f"Redacted PII for disabled user '{user.id}' (login email and "
            f"{sum(rows_updated_by_table.values())} historical actor-string reference(s) "
            f"across {len(rows_updated_by_table)} table(s))."
        ),
        actor=actor.email,
    )
    return UserRedactionReport(
        user_id=user.id, already_redacted=False, rows_updated_by_table=rows_updated_by_table
    )
