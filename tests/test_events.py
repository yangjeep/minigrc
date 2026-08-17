"""Generic domain-event store: append/idempotency/sequencing/immutability,
plus a representative command -> event -> projection -> rebuild flow proving
the mechanism end to end (issue #22).

The representative aggregate ("widget") is throwaway test-only scaffolding
— a real projection table created via raw SQL scoped to this file — not a
shipped product concept. Issue #22 is infrastructure only; #11 defines the
first real aggregate on top of these primitives.
"""

from __future__ import annotations

import datetime
import json

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app import events as events_module
from app.events import (
    ConcurrentAppendError,
    DomainEvent,
    append_and_project,
    append_event,
    rebuild_projection,
)
from app.models import new_id, utcnow


def _create_widget_projection_table(session):
    session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS test_widget_projection "
            "(aggregate_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
    )


def _reset_widget_projection(session):
    session.execute(text("DELETE FROM test_widget_projection"))


def _project_widget_created(session, event):
    payload = json.loads(event.payload_json)
    session.execute(
        text("INSERT INTO test_widget_projection (aggregate_id, name) VALUES (:aggregate_id, :name)"),
        {"aggregate_id": event.aggregate_id, "name": payload["name"]},
    )


def _project_widget_renamed(session, event):
    payload = json.loads(event.payload_json)
    session.execute(
        text("UPDATE test_widget_projection SET name = :name WHERE aggregate_id = :aggregate_id"),
        {"aggregate_id": event.aggregate_id, "name": payload["name"]},
    )


WIDGET_PROJECTORS = {
    "WidgetCreated": _project_widget_created,
    "WidgetRenamed": _project_widget_renamed,
}


def test_append_event_assigns_monotonic_sequence_per_aggregate(app):
    with app.state.session_factory() as session:
        aggregate_id = new_id()
        first = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        second = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetRenamed",
            payload={"name": "Beta"},
        )
        other_aggregate_id = new_id()
        third = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=other_aggregate_id,
            event_type="WidgetCreated",
            payload={"name": "Gamma"},
        )

        assert first.aggregate_sequence == 1
        assert second.aggregate_sequence == 2
        assert third.aggregate_sequence == 1  # independent per-aggregate sequence


def test_append_event_idempotency_key_short_circuits(app):
    with app.state.session_factory() as session:
        aggregate_id = new_id()
        first = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
            idempotency_key="widget:create:1",
        )
        session.flush()
        second = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha-retried"},
            idempotency_key="widget:create:1",
        )

        assert second.id == first.id
        assert second.aggregate_sequence == 1
        count = session.scalar(
            select(func.count()).select_from(DomainEvent).where(DomainEvent.aggregate_id == aggregate_id)
        )
        assert count == 1


def test_append_and_project_idempotent_replay_does_not_rerun_projector(app):
    """Adversarial review finding: `append_event`'s idempotency short-circuit
    only prevents a duplicate row — `append_and_project` must separately
    avoid re-running the projector on a replayed idempotent call, since a
    real command handler always goes through `append_and_project`, not the
    lower-level `append_event`, and projectors are not assumed idempotent."""
    with app.state.session_factory() as session:
        _create_widget_projection_table(session)
        session.commit()

        widget_id = new_id()
        run_count = {"n": 0}

        def _counting_created_projector(session, event):
            run_count["n"] += 1
            _project_widget_created(session, event)

        projectors = {"WidgetCreated": _counting_created_projector}

        first = append_and_project(
            session,
            projectors,
            aggregate_type="widget",
            aggregate_id=widget_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
            idempotency_key="widget:create:1",
        )
        session.flush()
        second = append_and_project(
            session,
            projectors,
            aggregate_type="widget",
            aggregate_id=widget_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha-retried"},
            idempotency_key="widget:create:1",
        )

        assert second.id == first.id
        assert run_count["n"] == 1


def test_duplicate_aggregate_sequence_rejected_at_db_level(app):
    with app.state.session_factory() as session:
        aggregate_id = new_id()
        now = utcnow()
        session.add(
            DomainEvent(
                aggregate_type="widget",
                aggregate_id=aggregate_id,
                aggregate_sequence=1,
                event_type="WidgetCreated",
                payload_json="{}",
                occurred_at=now,
                recorded_at=now,
            )
        )
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="widget",
                aggregate_id=aggregate_id,
                aggregate_sequence=1,
                event_type="WidgetCreated",
                payload_json="{}",
                occurred_at=now,
                recorded_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_append_event_retries_past_a_stale_sequence_conflict(app, monkeypatch):
    with app.state.session_factory() as session:
        aggregate_id = new_id()
        append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        session.flush()

        real_next = events_module._next_aggregate_sequence
        calls = {"n": 0}

        def fake_next(session_arg, aggregate_type, agg_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return 1  # stale — collides with the seeded event above
            return real_next(session_arg, aggregate_type, agg_id)

        monkeypatch.setattr(events_module, "_next_aggregate_sequence", fake_next)

        appended = events_module.append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetRenamed",
            payload={"name": "Beta"},
        )

        assert appended.aggregate_sequence == 2
        assert calls["n"] == 2


def test_append_event_raises_after_max_retries_exhausted(app, monkeypatch):
    with app.state.session_factory() as session:
        aggregate_id = new_id()
        append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        session.flush()

        monkeypatch.setattr(events_module, "_next_aggregate_sequence", lambda s, t, a: 1)

        with pytest.raises(ConcurrentAppendError):
            events_module.append_event(
                session,
                aggregate_type="widget",
                aggregate_id=aggregate_id,
                event_type="WidgetRenamed",
                payload={"name": "Beta"},
            )


def test_append_event_surfaces_non_sequence_integrity_errors_immediately(app):
    """Adversarial review finding: a caller bug that violates a constraint
    OTHER than the aggregate-sequence uniqueness (e.g. a NOT NULL
    violation from passing `aggregate_id=None`) must surface as the real
    error immediately, not get masked as `ConcurrentAppendError` after
    burning through every retry attempt — retrying can't fix a caller bug,
    since the exact same violation recurs regardless of which sequence
    number gets computed."""
    with app.state.session_factory() as session:
        with pytest.raises(IntegrityError):
            append_event(
                session,
                aggregate_type="widget",
                aggregate_id=None,  # type: ignore[arg-type]  # deliberately invalid
                event_type="WidgetCreated",
                payload={"name": "Alpha"},
            )


def test_append_event_idempotency_key_recheck_recovers_mid_loop(app, monkeypatch):
    """Adversarial review finding (TOCTOU): if a concurrent append commits
    the same idempotency_key after our pre-loop check found nothing, the
    fix must re-check the key at the start of every retry iteration (not
    only once before the loop) so the caller recovers the winner's event
    instead of failing. Simulated here with a single session: the fake
    pre-loop check reports "not found" (as if it ran before the winner
    existed), then the real, already-flushed "winner" row is what the
    in-loop recheck actually finds."""
    with app.state.session_factory() as session:
        aggregate_id = new_id()
        key = "widget:create:race"

        winner = DomainEvent(
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            aggregate_sequence=1,
            event_type="WidgetCreated",
            payload_json=json.dumps({"name": "Winner"}),
            occurred_at=utcnow(),
            recorded_at=utcnow(),
            idempotency_key=key,
        )
        session.add(winner)
        session.flush()

        real_find = events_module._find_by_idempotency_key
        calls = {"n": 0}

        def fake_find(session_arg, idempotency_key):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # pre-loop check: simulate "not found yet"
            return real_find(session_arg, idempotency_key)

        monkeypatch.setattr(events_module, "_find_by_idempotency_key", fake_find)

        result = events_module.append_event(
            session,
            aggregate_type="widget",
            aggregate_id=aggregate_id,
            event_type="WidgetCreated",
            payload={"name": "Loser-retry"},
            idempotency_key=key,
        )

        assert result.id == winner.id
        assert calls["n"] == 2


def test_domain_event_cannot_be_updated(app):
    with app.state.session_factory() as session:
        event = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=new_id(),
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        session.flush()
        event.event_type = "Tampered"
        with pytest.raises(RuntimeError, match="immutable"):
            session.flush()


def test_domain_event_cannot_be_deleted(app):
    with app.state.session_factory() as session:
        event = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=new_id(),
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        session.flush()
        session.delete(event)
        with pytest.raises(RuntimeError, match="immutable"):
            session.flush()


def test_domain_event_cannot_be_bulk_updated(app):
    """Adversarial review finding: before_update/before_delete mapper
    events only fire for per-instance ORM changes — they do NOT fire for
    an ORM-level bulk `update()`/`delete()` statement, which is ordinary
    application code, not a DBA console. Covered by a separate
    do_orm_execute guard."""
    with app.state.session_factory() as session:
        event = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=new_id(),
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        session.flush()
        with pytest.raises(RuntimeError, match="immutable"):
            session.execute(
                update(DomainEvent).where(DomainEvent.id == event.id).values(event_type="Tampered")
            )


def test_domain_event_cannot_be_bulk_deleted(app):
    with app.state.session_factory() as session:
        event = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=new_id(),
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        session.flush()
        with pytest.raises(RuntimeError, match="immutable"):
            session.execute(delete(DomainEvent).where(DomainEvent.id == event.id))


def test_domain_event_cannot_be_bulk_updated_via_legacy_query_update(app):
    with app.state.session_factory() as session:
        event = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=new_id(),
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        session.flush()
        with pytest.raises(RuntimeError, match="immutable"):
            session.query(DomainEvent).filter_by(id=event.id).update({"event_type": "Tampered"})


def test_occurred_at_defaults_to_recorded_at(app):
    with app.state.session_factory() as session:
        event = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=new_id(),
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        assert event.occurred_at == event.recorded_at


def test_occurred_at_can_be_explicitly_backdated_for_migration_facts(app):
    with app.state.session_factory() as session:
        backdated = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        event = append_event(
            session,
            aggregate_type="widget",
            aggregate_id=new_id(),
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
            occurred_at=backdated,
            actor_type="migration",
        )
        session.commit()

        # Round-trip through the DB (DateTime here is naive, matching every
        # other timestamp column in this repo — see utcnow() usage
        # throughout app/models.py) rather than only checking the in-memory
        # attribute, which would pass even if persistence silently dropped
        # the value.
        session.expire(event)
        assert event.occurred_at.replace(tzinfo=datetime.UTC) == backdated
        assert event.recorded_at.replace(tzinfo=datetime.UTC) != backdated
        assert event.actor_type == "migration"


def test_representative_command_event_projection_and_rebuild_flow(app):
    """Proves the generic append/project/rebuild mechanism end to end with a
    throwaway test-only aggregate — the smallest concrete demonstration of
    the flow #11 (and later domains) will build real aggregates on top of,
    without this infrastructure issue inventing a shipped product concept.
    """
    with app.state.session_factory() as session:
        _create_widget_projection_table(session)
        session.commit()

        widget_id = new_id()
        append_and_project(
            session,
            WIDGET_PROJECTORS,
            aggregate_type="widget",
            aggregate_id=widget_id,
            event_type="WidgetCreated",
            payload={"name": "Alpha"},
        )
        append_and_project(
            session,
            WIDGET_PROJECTORS,
            aggregate_type="widget",
            aggregate_id=widget_id,
            event_type="WidgetRenamed",
            payload={"name": "Beta"},
        )
        session.commit()

        name = session.execute(
            text("SELECT name FROM test_widget_projection WHERE aggregate_id = :id"), {"id": widget_id}
        ).scalar_one()
        assert name == "Beta"

        stored_events = session.scalars(
            select(DomainEvent)
            .where(DomainEvent.aggregate_id == widget_id)
            .order_by(DomainEvent.aggregate_sequence)
        ).all()
        assert [e.aggregate_sequence for e in stored_events] == [1, 2]
        assert [e.event_type for e in stored_events] == ["WidgetCreated", "WidgetRenamed"]

        # Wipe the projection entirely and rebuild it from events alone.
        session.execute(text("DELETE FROM test_widget_projection"))
        session.commit()
        assert session.execute(text("SELECT COUNT(*) FROM test_widget_projection")).scalar_one() == 0

        rebuild_projection(
            session, WIDGET_PROJECTORS, aggregate_type="widget", reset=_reset_widget_projection
        )
        session.commit()

        rebuilt_name = session.execute(
            text("SELECT name FROM test_widget_projection WHERE aggregate_id = :id"), {"id": widget_id}
        ).scalar_one()
        assert rebuilt_name == "Beta"


def test_rebuild_projection_orders_by_real_append_time_across_aggregates(app):
    """Adversarial review finding: `aggregate_id` is a random UUID4 hex
    (app/models.py::new_id) — not lexicographically sortable — so ordering
    replay by aggregate_id would NOT reproduce real append order for a
    projection whose state depends on interleaving between aggregates
    (e.g. "which widget was touched most recently", globally). Rebuild
    must order by recorded_at (real append time) first."""
    with app.state.session_factory() as session:
        session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS test_last_touched (marker INTEGER PRIMARY KEY, aggregate_id TEXT)"
            )
        )
        session.execute(text("DELETE FROM test_last_touched"))
        session.commit()

        def _project_touched(session, event):
            session.execute(text("DELETE FROM test_last_touched"))
            session.execute(
                text("INSERT INTO test_last_touched (marker, aggregate_id) VALUES (1, :aggregate_id)"),
                {"aggregate_id": event.aggregate_id},
            )

        def _reset_touched(session):
            session.execute(text("DELETE FROM test_last_touched"))

        touch_projectors = {"Touched": _project_touched}

        widget_a = new_id()
        widget_b = new_id()
        # Real append order: A, B, A — the final "most recently touched"
        # must be A, not whichever of A/B happens to sort last by id.
        append_and_project(
            session,
            touch_projectors,
            aggregate_type="widget",
            aggregate_id=widget_a,
            event_type="Touched",
            payload={},
        )
        append_and_project(
            session,
            touch_projectors,
            aggregate_type="widget",
            aggregate_id=widget_b,
            event_type="Touched",
            payload={},
        )
        append_and_project(
            session,
            touch_projectors,
            aggregate_type="widget",
            aggregate_id=widget_a,
            event_type="Touched",
            payload={},
        )
        session.commit()

        live_final = session.execute(text("SELECT aggregate_id FROM test_last_touched")).scalar_one()
        assert live_final == widget_a

        session.execute(text("DELETE FROM test_last_touched"))
        session.commit()

        rebuild_projection(session, touch_projectors, aggregate_type="widget", reset=_reset_touched)
        session.commit()

        rebuilt_final = session.execute(text("SELECT aggregate_id FROM test_last_touched")).scalar_one()
        assert rebuilt_final == widget_a


def test_rebuild_projection_unknown_event_type_raises(app):
    with app.state.session_factory() as session:
        _create_widget_projection_table(session)
        session.commit()

        widget_id = new_id()
        append_event(
            session,
            aggregate_type="widget",
            aggregate_id=widget_id,
            event_type="WidgetTeleported",
            payload={},
        )
        session.commit()

        with pytest.raises(KeyError):
            rebuild_projection(
                session, WIDGET_PROJECTORS, aggregate_type="widget", reset=_reset_widget_projection
            )
