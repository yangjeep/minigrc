"""Regression test for a foundational SQLite transaction-integrity bug
found while adversarially testing issue #32 (not specific to it):
pysqlite's own legacy implicit BEGIN/COMMIT tracking conflicts with
SQLAlchemy's explicit SAVEPOINT emission (`Session.begin_nested()`,
used by every event-sourced command via
app/events.py::append_and_project's `_append_event_with_created_flag`).

The following sequence silently fails to roll back data that was
already written inside a *released* savepoint: (1) an earlier
`session.commit()`, (2) a `begin_nested()` block that completes and
releases normally, (3) some later, unrelated failure in the same
still-open transaction, (4) `session.rollback()`.

**Reachability, checked directly, not assumed**: no web route in this
app calls `session.commit()` mid-request — `app/deps.py::get_db`
commits exactly once, at the very end of a request, and a mid-request
failure only ever calls `rollback()` (never preceded by a same-session
`commit()`), which this file's other two tests confirm was never
broken. `app/jobs.py`/`app/import_directory.py` (the only other
`session.commit()` call sites in `app/`) never call
`append_and_project`/`append_event` at all. So today, this bug is
**latent, not actively reachable** by any real request or background
job in this codebase — it only matters for a *future* code path that
commits mid-session and later does event-sourced work that might need
to roll back. Still worth fixing properly and worth a regression test
now, precisely so that future code doesn't reintroduce it unnoticed.

**The standard SQLAlchemy-documented fix was tried and reverted**:
disabling pysqlite's own `isolation_level` and emitting an explicit
`BEGIN`/`BEGIN IMMEDIATE` ourselves does fix this correctness bug, but
directly measured (this same session, via the LiveServer+concurrent-
client harness `tests/test_sqlite_read_after_write.py` already
established) to reintroduce the exact class of "database is locked"
availability failure issue #55 fixed — at as few as 2 concurrent
requests, because it holds a transaction-level lock for every
session's full activity (including plain reads), not just the brief
window immediately around a write. That trade was not acceptable to
make unilaterally, so the fix is deferred to a dedicated follow-up
issue for a more surgical approach (see `app/db.py`'s comment at the
former site of the reverted fix, and issue #65).
`test_rollback_after_released_savepoint_undoes_the_event` below is
`xfail(strict=True)`, not skipped — it stays green when it starts
failing (i.e. the moment a real fix lands), which is exactly the
signal that fix should flip it back to a plain passing test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import build_engine, init_db, make_session_factory
from app.events import append_and_project
from app.models import EvidenceArtifact, new_id


def _broken_projector(session, event):
    raise RuntimeError("simulated failure after the savepoint already released")


@pytest.mark.xfail(
    reason="Known, deferred pysqlite/SAVEPOINT rollback bug (issue #65) — see module "
    "docstring. The standard fix reintroduces issue #55's concurrency regression; a "
    "more surgical fix is tracked in #65, not shipped here.",
    strict=True,
)
def test_rollback_after_released_savepoint_undoes_the_event(tmp_path):
    db_path = str(tmp_path / "savepoint_rollback.db")
    engine = build_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        artifact = EvidenceArtifact(title="Committed before the savepoint")
        session.add(artifact)
        session.commit()

        try:
            append_and_project(
                session,
                {"Foo": _broken_projector},
                aggregate_type="test_aggregate",
                aggregate_id=new_id(),
                event_type="Foo",
                payload={},
                actor_id="user-1",
            )
        except RuntimeError:
            pass
        session.rollback()

    # Bypass the ORM identity map entirely — a fresh connection is the
    # only way to be certain this reflects what is actually durable.
    with engine.connect() as conn:
        events = conn.execute(text("SELECT * FROM domain_events")).fetchall()
        assert events == [], "the event must not survive a rollback that happens after it was appended"

        artifacts = conn.execute(text("SELECT title FROM evidence_artifacts")).fetchall()
        assert [row.title for row in artifacts] == ["Committed before the savepoint"]


def test_rollback_without_a_prior_commit_still_works(tmp_path):
    """The bug only manifested with a prior commit in the same session
    (see the module docstring) — confirm the simpler case (no prior
    commit at all) was never broken, so this fix doesn't just shuffle
    the bug to a different scenario."""
    db_path = str(tmp_path / "savepoint_rollback_no_prior_commit.db")
    engine = build_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        artifact = EvidenceArtifact(title="Never committed")
        session.add(artifact)
        session.flush()

        try:
            append_and_project(
                session,
                {"Foo": _broken_projector},
                aggregate_type="test_aggregate",
                aggregate_id=new_id(),
                event_type="Foo",
                payload={},
                actor_id="user-1",
            )
        except RuntimeError:
            pass
        session.rollback()

    with engine.connect() as conn:
        events = conn.execute(text("SELECT * FROM domain_events")).fetchall()
        assert events == []
        artifacts = conn.execute(text("SELECT * FROM evidence_artifacts")).fetchall()
        assert artifacts == []


def test_successful_event_append_still_commits_normally(tmp_path):
    """The fix must not make a *successful* (non-rolled-back) event
    append stop being durable — only the rollback path changes."""
    db_path = str(tmp_path / "savepoint_success.db")
    engine = build_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    def _working_projector(session, event):
        pass

    with session_factory() as session:
        aggregate_id = new_id()
        append_and_project(
            session,
            {"Foo": _working_projector},
            aggregate_type="test_aggregate",
            aggregate_id=aggregate_id,
            event_type="Foo",
            payload={},
            actor_id="user-1",
        )
        session.commit()

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM domain_events WHERE aggregate_id = :id"), {"id": aggregate_id}
        ).scalar()
        assert count == 1
