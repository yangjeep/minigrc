"""Generic immutable domain-event store, projector, and rebuild primitives
(issue #22 / epic #21).

This module is intentionally generic infrastructure, not a compliance
domain: it defines no event types, aggregate types, or projection tables
of its own. The first real caller (issue #11, control operations) defines
its own event types and a `projectors` dict; this module only provides the
append/idempotency/sequencing/rebuild mechanics shared by every future
event-backed aggregate.

`payload_json` is a plain JSON-encoded `Text` column (`json.dumps`/
`json.loads` at the call site), matching `Job.payload_json` (app/models.py)
rather than SQLAlchemy's `JSON` type — portable across SQLite/Postgres
without a JSON column type dependency.

See docs/superpowers/specs/2026-08-15-issue22-event-store-design.md for
the full design rationale.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from app.db import Base
from app.models import new_id, utcnow

ProjectorFn = Callable[[Session, "DomainEvent"], None]

_MAX_APPEND_ATTEMPTS = 5


class ConcurrentAppendError(RuntimeError):
    """Raised when an event cannot be appended after retrying past
    concurrent aggregate-sequence contention on the same aggregate."""


class DomainEvent(Base):
    """An immutable fact about a domain aggregate.

    Rows are never updated or deleted by application code — see the
    `before_update`/`before_delete` guards below (per-instance ORM changes)
    and `_reject_domain_event_bulk_mutation` (ORM-level bulk update/delete
    statements), which together reject any ORM-level mutation attempt.
    Current state for any aggregate is derived by replaying its events
    through a projector (see `append_and_project`/`rebuild_projection`),
    never read or written directly on this table by domain code.
    """

    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "aggregate_sequence",
            name="uq_domain_event_aggregate_sequence",
        ),
        UniqueConstraint("idempotency_key", name="uq_domain_event_idempotency_key"),
        CheckConstraint("aggregate_sequence >= 1", name="ck_domain_event_aggregate_sequence_positive"),
        # No separate index on aggregate_type alone: the unique constraint
        # above already provides an equally usable leading-column index for
        # aggregate_type / aggregate_type+aggregate_id lookups, and this
        # table is append-only and write-heavy — a redundant index would be
        # pure write amplification.
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(30), default="system", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)


@event.listens_for(DomainEvent, "before_update")
def _reject_domain_event_update(mapper, connection, target: DomainEvent) -> None:
    raise RuntimeError("DomainEvent rows are immutable and cannot be updated")


@event.listens_for(DomainEvent, "before_delete")
def _reject_domain_event_delete(mapper, connection, target: DomainEvent) -> None:
    raise RuntimeError("DomainEvent rows are immutable and cannot be deleted")


@event.listens_for(Session, "do_orm_execute")
def _reject_domain_event_bulk_mutation(orm_execute_state: ORMExecuteState) -> None:
    # The before_update/before_delete mapper events above only fire for
    # per-instance ORM changes (e.g. `event.field = x; session.flush()`).
    # They do NOT fire for ORM-level bulk statements — `session.execute(
    # update(DomainEvent)...)`, `session.execute(delete(DomainEvent)...)`,
    # or the legacy `session.query(DomainEvent).update()/.delete()` — which
    # are ordinary application code, not a DBA console, and must be blocked
    # too for "normal application code cannot mutate/delete existing domain
    # events" to actually hold. This does not (and cannot) catch raw SQL
    # issued outside the ORM — no automated guard can prevent that, same as
    # every other immutable table in this repo.
    if not (orm_execute_state.is_update or orm_execute_state.is_delete):
        return
    bind_mapper = orm_execute_state.bind_mapper
    if bind_mapper is not None and bind_mapper.class_ is DomainEvent:
        raise RuntimeError("DomainEvent rows are immutable and cannot be bulk-updated or bulk-deleted")


def _next_aggregate_sequence(session: Session, aggregate_type: str, aggregate_id: str) -> int:
    current_max = session.scalar(
        select(func.max(DomainEvent.aggregate_sequence)).where(
            DomainEvent.aggregate_type == aggregate_type,
            DomainEvent.aggregate_id == aggregate_id,
        )
    )
    return (current_max or 0) + 1


def _find_by_idempotency_key(session: Session, idempotency_key: str) -> DomainEvent | None:
    return session.scalars(
        select(DomainEvent).where(DomainEvent.idempotency_key == idempotency_key)
    ).one_or_none()


def _append_event_with_created_flag(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime.datetime | None,
    actor_type: str,
    actor_id: str | None,
    correlation_id: str | None,
    causation_id: str | None,
    idempotency_key: str | None,
    schema_version: int,
) -> tuple[DomainEvent, bool]:
    """Core of `append_event`, additionally reporting whether a new row was
    actually appended (`True`) or an existing idempotent row was returned
    unchanged (`False`) — `append_and_project` needs this to avoid
    re-running a projector on a replayed idempotent call."""
    if idempotency_key is not None:
        existing = _find_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return existing, False

    recorded_at = utcnow()
    resolved_occurred_at = occurred_at if occurred_at is not None else recorded_at
    payload_json = json.dumps(payload)

    last_error: IntegrityError | None = None
    for _ in range(_MAX_APPEND_ATTEMPTS):
        if idempotency_key is not None:
            # Re-check every attempt, not just once before the loop: closes
            # the TOCTOU window where a concurrent append committed the
            # same key since our last check, instead of retrying an insert
            # that's doomed to conflict on idempotency_key again.
            existing = _find_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                return existing, False

        next_sequence = _next_aggregate_sequence(session, aggregate_type, aggregate_id)
        candidate = DomainEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_sequence=next_sequence,
            event_type=event_type,
            schema_version=schema_version,
            payload_json=payload_json,
            occurred_at=resolved_occurred_at,
            recorded_at=recorded_at,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush(objects=[candidate])
            return candidate, True
        except IntegrityError as exc:
            last_error = exc
            # Only worth retrying if this genuinely was a sequence-slot
            # race: confirm another row now actually occupies the exact
            # slot we just tried. Any other IntegrityError (a NOT NULL/
            # CHECK violation, a caller bug) would recur identically no
            # matter what sequence we compute, so surface it immediately
            # instead of masking it as concurrency contention after
            # burning through every retry attempt.
            slot_taken = session.scalar(
                select(DomainEvent.id).where(
                    DomainEvent.aggregate_type == aggregate_type,
                    DomainEvent.aggregate_id == aggregate_id,
                    DomainEvent.aggregate_sequence == next_sequence,
                )
            )
            if slot_taken is None:
                raise
            continue

    raise ConcurrentAppendError(
        f"Could not append event for {aggregate_type}:{aggregate_id} after "
        f"{_MAX_APPEND_ATTEMPTS} attempts — too much concurrent contention on this aggregate."
    ) from last_error


def append_event(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime.datetime | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    idempotency_key: str | None = None,
    schema_version: int = 1,
) -> DomainEvent:
    """Append one immutable domain event, assigning the next aggregate
    sequence number.

    Idempotent when `idempotency_key` is given: a repeat call with the same
    key returns the original event unchanged rather than appending a
    duplicate — namespace the key yourself (e.g.
    f"{aggregate_type}:{aggregate_id}:{command_name}") since this module
    only enforces global uniqueness, not a key format. Use
    `append_and_project` instead of this function directly when a
    projector must run, so an idempotent replay doesn't re-apply it.

    `recorded_at` is always "now" — never caller-supplied. `occurred_at`
    defaults to `recorded_at`; pass an explicit value only for genuine
    backdating (migration/import facts), and pass `actor_type="migration"`
    in that case so a later reader can never mistake it for a real-time
    human action.
    """
    stored_event, _created = _append_event_with_created_flag(
        session,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        schema_version=schema_version,
    )
    return stored_event


def append_and_project(
    session: Session,
    projectors: dict[str, ProjectorFn],
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime.datetime | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    idempotency_key: str | None = None,
    schema_version: int = 1,
) -> DomainEvent:
    """Append an event and immediately apply its projector, in the caller's
    existing transaction — same discipline as app.audit.record_audit_event:
    no commit here, the caller's request-scoped session commits once at the
    end (see app/deps.py::get_db).

    When `idempotency_key` causes this call to return an already-appended
    event (a retried command), the projector is NOT re-run — it already
    ran the first time this event was appended, and projectors are not
    guaranteed idempotent themselves.
    """
    new_event, created = _append_event_with_created_flag(
        session,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        schema_version=schema_version,
    )
    if created:
        projectors[new_event.event_type](session, new_event)
    return new_event


def rebuild_projection(
    session: Session,
    projectors: dict[str, ProjectorFn],
    *,
    aggregate_type: str,
    reset: Callable[[Session], None],
) -> None:
    """Reset an aggregate type's projection and rebuild it deterministically
    by replaying every event for that aggregate type, in real append order.

    Ordered by `(recorded_at, aggregate_type, aggregate_id,
    aggregate_sequence)` — `recorded_at` (real append order) is the
    primary key, NOT `aggregate_id` alone: `aggregate_id` is a random
    UUID4 hex (see app/models.py::new_id), not lexicographically sortable,
    so ordering by it would replay events in an arbitrary order unrelated
    to when they actually happened whenever a projection's state depends
    on interleaving between aggregates (e.g. a cross-aggregate "most
    recently changed" read model), not only on each aggregate's own
    history. `(aggregate_type, aggregate_id, aggregate_sequence)` is kept
    as a deterministic tiebreak for the rare case two events share a
    `recorded_at` value at the storage engine's timestamp resolution.

    `reset` is caller-supplied since this module has no way to know what a
    given aggregate's projection table(s) look like. An event_type missing
    from `projectors` raises KeyError immediately — a projector gap must
    fail loud during rebuild, not silently produce an incomplete
    projection.
    """
    reset(session)
    events = session.scalars(
        select(DomainEvent)
        .where(DomainEvent.aggregate_type == aggregate_type)
        .order_by(
            DomainEvent.recorded_at,
            DomainEvent.aggregate_type,
            DomainEvent.aggregate_id,
            DomainEvent.aggregate_sequence,
        )
    ).all()
    for stored_event in events:
        projectors[stored_event.event_type](session, stored_event)
