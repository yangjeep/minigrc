"""Schema/constraint tests for issue #11's control-operations tables
(ControlPeriod, ControlOccurrence, ControlOccurrenceEvidence) plus
InternalControl's new cadence/owner columns — proves the schema before any
generation/route behavior is built on top of it. See
docs/superpowers/specs/2026-08-15-issue11-control-operations-lifecycle-design.md
§11 slice 1 and §12.4 for the design this schema implements.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.control_occurrences import (
    generate_occurrences,
    link_evidence,
    perform_occurrence,
    rebuild_control_occurrence_projection,
    record_occurrence_manually,
)
from app.events import DomainEvent
from app.models import (
    ControlOccurrence,
    ControlOccurrenceEvidence,
    ControlPeriod,
    EvidenceSnapshot,
    InternalControl,
    Person,
)


def _make_control(session, **kwargs):
    defaults = {"name": "Test control", "owner": "owner@example.com"}
    defaults.update(kwargs)
    control = InternalControl(**defaults)
    session.add(control)
    session.flush()
    return control


def _make_period(session, **kwargs):
    defaults = {
        "name": "Test period",
        "starts_on": datetime.date(2026, 1, 1),
        "ends_on": datetime.date(2026, 6, 30),
        "created_by": "admin@example.com",
    }
    defaults.update(kwargs)
    period = ControlPeriod(**defaults)
    session.add(period)
    session.flush()
    return period


def _make_occurrence(session, control, **kwargs):
    from app.models import new_id, utcnow

    defaults = {
        "id": new_id(),
        "control_id": control.id,
        "origin": "manual",
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    defaults.update(kwargs)
    occurrence = ControlOccurrence(**defaults)
    session.add(occurrence)
    session.flush()
    return occurrence


def test_control_period_rejects_ends_on_before_starts_on(app):
    with app.state.session_factory() as session:
        session.add(
            ControlPeriod(
                name="Bad period",
                starts_on=datetime.date(2026, 6, 30),
                ends_on=datetime.date(2026, 1, 1),
                created_by="admin@example.com",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_control_period_rejects_invalid_status(app):
    with app.state.session_factory() as session:
        session.add(
            ControlPeriod(
                name="Bad status",
                starts_on=datetime.date(2026, 1, 1),
                ends_on=datetime.date(2026, 6, 30),
                status="not-a-real-status",
                created_by="admin@example.com",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_control_period_accepts_valid_statuses(app):
    with app.state.session_factory() as session:
        for status in ("planned", "active", "closed"):
            period = _make_period(session, name=f"Period {status}", status=status)
            assert period.status == status


def test_internal_control_cadence_type_allows_null(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        assert control.cadence_type is None


def test_internal_control_cadence_type_rejects_invalid_value(app):
    with app.state.session_factory() as session:
        session.add(InternalControl(name="Bad cadence", cadence_type="weekly"))
        with pytest.raises(IntegrityError):
            session.flush()


def test_internal_control_cadence_type_accepts_valid_values(app):
    with app.state.session_factory() as session:
        for cadence in ("calendar", "event_driven"):
            control = _make_control(session, name=f"Control {cadence}", cadence_type=cadence)
            assert control.cadence_type == cadence


def test_internal_control_cadence_interval_allows_null(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        assert control.cadence_interval_months is None


def test_internal_control_cadence_interval_accepts_positive_value(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="calendar", cadence_interval_months=3)
        assert control.cadence_interval_months == 3


def test_internal_control_cadence_interval_rejects_zero_and_negative(app):
    """Regression guard: a non-positive cadence_interval_months would send
    app/control_occurrences.py's due-date generation loop backward forever
    (adversarial review finding, issue #11)."""
    for bad_value in (0, -1):
        with app.state.session_factory() as session:
            session.add(
                InternalControl(
                    name=f"Bad cadence {bad_value}",
                    cadence_type="calendar",
                    cadence_interval_months=bad_value,
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()


def test_internal_control_owner_person_id_references_person(app):
    with app.state.session_factory() as session:
        person = Person(email="owner@example.com", display_name="Owner")
        session.add(person)
        session.flush()

        control = _make_control(session, owner_person_id=person.id)
        assert control.owner_person_id == person.id


def test_control_occurrence_rejects_invalid_origin(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        from app.models import new_id, utcnow

        session.add(
            ControlOccurrence(
                id=new_id(),
                control_id=control.id,
                origin="not-a-real-origin",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_control_occurrence_unique_control_period_due_at(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        period = _make_period(session)
        due_at = datetime.datetime(2026, 3, 31, tzinfo=datetime.UTC)

        _make_occurrence(session, control, control_period_id=period.id, origin="generated", due_at=due_at)
        session.commit()

        from app.models import new_id, utcnow

        session.add(
            ControlOccurrence(
                id=new_id(),
                control_id=control.id,
                control_period_id=period.id,
                origin="generated",
                due_at=due_at,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_control_occurrence_unique_control_due_at_when_period_less(app):
    """Regression guard: UniqueConstraint("control_id", "control_period_id",
    "due_at") alone does NOT stop two control_period_id=NULL rows sharing a
    due_at (NULL != NULL for uniqueness on both SQLite and PostgreSQL) — the
    partial unique index "uq_occurrence_control_due_no_period" closes that
    gap specifically for the period-less case."""
    with app.state.session_factory() as session:
        control = _make_control(session)
        due_at = datetime.datetime(2026, 3, 31, tzinfo=datetime.UTC)

        _make_occurrence(session, control, control_period_id=None, origin="manual", due_at=due_at)
        session.commit()

        from app.models import new_id, utcnow

        session.add(
            ControlOccurrence(
                id=new_id(),
                control_id=control.id,
                control_period_id=None,
                origin="manual",
                due_at=due_at,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_control_occurrence_allows_multiple_null_due_at(app):
    """NULL != NULL for uniqueness in both SQLite and PostgreSQL — multiple
    event-driven occurrences (due_at=NULL) for the same control/period must
    not collide."""
    with app.state.session_factory() as session:
        control = _make_control(session)
        first = _make_occurrence(session, control, origin="manual", due_at=None)
        second = _make_occurrence(session, control, origin="manual", due_at=None)
        session.commit()
        assert first.id != second.id


def test_internal_control_cannot_be_deleted_while_occurrences_exist(app):
    """No cascade from InternalControl to ControlOccurrence — deleting a
    control with occurrence history must fail rather than silently
    destroying that history (PRAGMA foreign_keys=ON on SQLite)."""
    with app.state.session_factory() as session:
        control = _make_control(session)
        _make_occurrence(session, control, origin="manual")
        session.commit()

        session.delete(control)
        with pytest.raises(IntegrityError):
            session.flush()


def test_control_occurrence_evidence_unique_pair(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        occurrence = _make_occurrence(session, control, origin="manual")
        evidence = EvidenceSnapshot(
            source_type="aws_cloudtrail",
            check_key="test-check",
            status="pass",
            title="Test evidence",
            collected_at=datetime.datetime.now(datetime.UTC),
            normalized_payload_json="{}",
            raw_payload_sha256="0" * 64,
        )
        session.add(evidence)
        session.flush()

        session.add(ControlOccurrenceEvidence(occurrence_id=occurrence.id, evidence_snapshot_id=evidence.id))
        session.commit()

        session.add(ControlOccurrenceEvidence(occurrence_id=occurrence.id, evidence_snapshot_id=evidence.id))
        with pytest.raises(IntegrityError):
            session.flush()


# --- Generation, projectors, and rebuild (slice 2) --------------------------


def test_generate_occurrences_period_scoped_produces_expected_due_dates(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="calendar", cadence_interval_months=3)
        period = _make_period(
            session, starts_on=datetime.date(2026, 1, 1), ends_on=datetime.date(2026, 6, 30), status="active"
        )

        occurrences = generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()

        assert len(occurrences) == 2
        assert all(o.origin == "generated" for o in occurrences)
        assert all(o.control_period_id == period.id for o in occurrences)
        due_dates = sorted(o.due_at.date() for o in occurrences)
        assert due_dates == [datetime.date(2026, 1, 1), datetime.date(2026, 4, 1)]


def test_generate_occurrences_is_idempotent(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="calendar", cadence_interval_months=3)
        period = _make_period(session, status="active")

        first = generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()
        second = generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()

        assert [o.id for o in first] == [o.id for o in second]
        all_occurrences = session.scalars(
            select(ControlOccurrence).where(ControlOccurrence.control_id == control.id)
        ).all()
        assert len(all_occurrences) == len(first)


def test_generate_occurrences_regeneration_after_cadence_change_leaves_prior_rows_untouched(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="calendar", cadence_interval_months=3)
        period = _make_period(session, status="active")

        first = generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()
        first_ids = {o.id for o in first}

        control.cadence_interval_months = 1
        session.flush()
        second = generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()

        # Prior rows are untouched (still present, same ids) — cadence
        # changes only affect future generation, never rewrite history.
        second_ids = {o.id for o in second}
        assert first_ids.issubset(second_ids)
        assert len(second_ids) > len(first_ids)


def test_generate_occurrences_noop_for_event_driven_control(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="event_driven")
        period = _make_period(session, status="active")
        assert generate_occurrences(session, control, period=period, actor_type="system") == []


def test_generate_occurrences_noop_for_unconfigured_control(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        period = _make_period(session, status="active")
        assert generate_occurrences(session, control, period=period, actor_type="system") == []


def test_generate_occurrences_noop_for_negative_cadence_interval(app):
    """Domain-level defense in depth (adversarial review finding, issue
    #11): even bypassing the DB CHECK constraint (an in-memory, unflushed
    control), generate_occurrences must not walk _compute_due_dates
    backward forever for a non-positive interval."""
    with app.state.session_factory() as session:
        control = InternalControl(name="Bad interval", cadence_type="calendar", cadence_interval_months=-1)
        period = _make_period(session, status="active")
        assert generate_occurrences(session, control, period=period, actor_type="system") == []


def test_generate_occurrences_rejects_non_active_period(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="calendar", cadence_interval_months=3)
        period = _make_period(session, status="planned")
        with pytest.raises(ValueError, match="not active"):
            generate_occurrences(session, control, period=period, actor_type="system")


def test_generate_occurrences_period_less_rolling_path(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="calendar", cadence_interval_months=1)
        occurrences = generate_occurrences(session, control, horizon_months=3, actor_type="system")
        session.commit()

        assert len(occurrences) >= 1
        assert all(o.control_period_id is None for o in occurrences)
        assert all(o.origin == "generated" for o in occurrences)


def test_record_occurrence_manually_sets_origin_manual(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        occurrence = record_occurrence_manually(
            session, control, scope_note="Ad hoc offboarding check", actor_type="user", actor_id=None
        )
        session.commit()

        assert occurrence.origin == "manual"
        assert occurrence.control_id == control.id
        assert occurrence.scope_note == "Ad hoc offboarding check"
        assert occurrence.due_at is None


def test_record_occurrence_manually_preserves_due_at_when_given(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        due_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        occurrence = record_occurrence_manually(session, control, due_at=due_at, actor_type="user")
        # DateTime (no timezone=True) round-trips as naive, same as every
        # other timestamp column in this repo — compare wall-clock value,
        # tzinfo representation aside.
        assert occurrence.due_at.replace(tzinfo=None) == due_at.replace(tzinfo=None)


def test_perform_occurrence_sets_projection_fields_and_appends_event(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        occurrence = record_occurrence_manually(session, control, actor_type="user")
        session.flush()

        person = Person(email="performer@example.com", display_name="Performer")
        session.add(person)
        session.flush()

        performed_at = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)
        perform_occurrence(
            session,
            occurrence,
            occurred_at=performed_at,
            performed_by_person_id=person.id,
            scope_note="Reviewed access list",
            evidence_note="Attached export",
            actor_type="user",
            actor_id=None,
        )
        session.commit()

        assert occurrence.performed_at == performed_at
        assert occurrence.performed_by_person_id == person.id
        assert occurrence.scope_note == "Reviewed access list"
        assert occurrence.evidence_note == "Attached export"

        events = session.scalars(
            select(DomainEvent)
            .where(DomainEvent.aggregate_id == occurrence.id)
            .order_by(DomainEvent.aggregate_sequence)
        ).all()
        assert [e.event_type for e in events] == [
            "ControlOccurrenceRecordedManually",
            "ControlOccurrencePerformed",
        ]
        assert events[0].actor_type == "user"


def test_perform_occurrence_twice_preserves_full_history_via_events(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        occurrence = record_occurrence_manually(session, control, actor_type="user")
        session.flush()

        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC),
            performed_by_person_id=None,
            scope_note="First submission",
            evidence_note="",
            actor_type="user",
        )
        session.flush()
        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 2, 2, tzinfo=datetime.UTC),
            performed_by_person_id=None,
            scope_note="Corrected submission",
            evidence_note="",
            actor_type="user",
        )
        session.commit()

        # Projection reflects only the latest submission...
        assert occurrence.scope_note == "Corrected submission"

        # ...but both submissions remain independently queryable as events.
        performed_events = session.scalars(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == occurrence.id,
                DomainEvent.event_type == "ControlOccurrencePerformed",
            )
        ).all()
        assert len(performed_events) == 2


def test_link_evidence_creates_join_row(app):
    with app.state.session_factory() as session:
        control = _make_control(session)
        occurrence = record_occurrence_manually(session, control, actor_type="user")
        session.flush()

        evidence = EvidenceSnapshot(
            source_type="aws_cloudtrail",
            check_key="test-check",
            status="pass",
            title="Test evidence",
            collected_at=datetime.datetime.now(datetime.UTC),
            normalized_payload_json="{}",
            raw_payload_sha256="0" * 64,
        )
        session.add(evidence)
        session.flush()

        link_evidence(session, occurrence, evidence.id, actor_type="user")
        session.commit()

        link = session.scalar(
            select(ControlOccurrenceEvidence).where(ControlOccurrenceEvidence.occurrence_id == occurrence.id)
        )
        assert link is not None
        assert link.evidence_snapshot_id == evidence.id


def test_responsible_person_id_snapshotted_not_live_join(app):
    """Historical stability: changing a control's owner after an occurrence
    was generated must not retroactively change that occurrence's
    responsible_person_id, and newly generated occurrences must use the
    new owner."""
    with app.state.session_factory() as session:
        old_owner = Person(email="old-owner@example.com", display_name="Old Owner")
        new_owner = Person(email="new-owner@example.com", display_name="New Owner")
        session.add_all([old_owner, new_owner])
        session.flush()

        control = _make_control(
            session, cadence_type="calendar", cadence_interval_months=3, owner_person_id=old_owner.id
        )
        period = _make_period(session, status="active")

        first_batch = generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()
        assert all(o.responsible_person_id == old_owner.id for o in first_batch)

        control.owner_person_id = new_owner.id
        session.flush()

        second_period = _make_period(
            session,
            name="Second period",
            starts_on=datetime.date(2027, 1, 1),
            ends_on=datetime.date(2027, 6, 30),
            status="active",
        )
        second_batch = generate_occurrences(session, control, period=second_period, actor_type="system")
        session.commit()

        # Old occurrences unchanged; new ones reflect the new owner.
        for occurrence in first_batch:
            session.refresh(occurrence)
            assert occurrence.responsible_person_id == old_owner.id
        assert all(o.responsible_person_id == new_owner.id for o in second_batch)


def test_rebuild_control_occurrence_projection_reproduces_state(app):
    with app.state.session_factory() as session:
        control = _make_control(session, cadence_type="calendar", cadence_interval_months=3)
        period = _make_period(session, status="active")

        generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()

        occurrence = session.scalars(
            select(ControlOccurrence).where(ControlOccurrence.control_id == control.id)
        ).first()
        evidence = EvidenceSnapshot(
            source_type="aws_cloudtrail",
            check_key="test-check",
            status="pass",
            title="Test evidence",
            collected_at=datetime.datetime.now(datetime.UTC),
            normalized_payload_json="{}",
            raw_payload_sha256="0" * 64,
        )
        session.add(evidence)
        session.flush()
        link_evidence(session, occurrence, evidence.id, actor_type="user")
        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC),
            performed_by_person_id=None,
            scope_note="Done",
            evidence_note="",
            actor_type="user",
        )
        session.commit()

        def _naive(value):
            # DateTime (no timezone=True) round-trips as naive, same as
            # every other timestamp column in this repo — normalize before
            # comparing so an in-memory-still-aware value (not yet
            # round-tripped through a fresh query) compares equal to a
            # freshly-fetched naive one on the same wall-clock instant.
            return value.replace(tzinfo=None) if isinstance(value, datetime.datetime) else value

        pre_rebuild = {
            (o.id, _naive(o.due_at), _naive(o.performed_at), o.scope_note, o.origin)
            for o in session.scalars(select(ControlOccurrence)).all()
        }
        pre_rebuild_links = {
            (e.occurrence_id, e.evidence_snapshot_id)
            for e in session.scalars(select(ControlOccurrenceEvidence)).all()
        }

        rebuild_control_occurrence_projection(session)
        session.commit()

        post_rebuild = {
            (o.id, _naive(o.due_at), _naive(o.performed_at), o.scope_note, o.origin)
            for o in session.scalars(select(ControlOccurrence)).all()
        }
        post_rebuild_links = {
            (e.occurrence_id, e.evidence_snapshot_id)
            for e in session.scalars(select(ControlOccurrenceEvidence)).all()
        }

        assert pre_rebuild == post_rebuild
        assert pre_rebuild_links == post_rebuild_links


def test_rebuild_control_occurrence_projection_fails_loud_on_unknown_event_type(app):
    """Guards against a future projector gap silently producing an
    incomplete projection — matches app/events.py's own contract."""
    with app.state.session_factory() as session:
        from app.events import append_event

        control = _make_control(session)
        append_event(
            session,
            aggregate_type="control_occurrence",
            aggregate_id="deadbeefdeadbeefdeadbeefdeadbeef",
            event_type="ControlOccurrenceSomethingUnknown",
            payload={"control_id": control.id},
            actor_type="system",
        )
        session.commit()

        with pytest.raises(KeyError):
            rebuild_control_occurrence_projection(session)
