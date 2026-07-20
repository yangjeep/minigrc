"""Tests for the database-backed job/worker foundation (Feature 7).

See the architecture checkpoint on umbrella issue #5 and ADR #24.
"""

from __future__ import annotations

import datetime

import pytest

from app.jobs import (
    STALE_RUNNING_THRESHOLD_SECONDS,
    _try_claim,
    claim_job,
    enqueue_job,
    process_next_job,
    register_handler,
    run_job,
)
from app.models import Job

TEST_JOB_TYPE = "test_echo"


@pytest.fixture(autouse=True)
def _register_echo_handler():
    calls = []

    def handler(session, payload):
        if payload.get("fail"):
            raise RuntimeError("deliberate failure")
        calls.append(payload)
        return {"echoed": payload}

    register_handler(TEST_JOB_TYPE, handler)
    yield calls


def test_enqueue_job_creates_pending_row(app):
    with app.state.session_factory() as session:
        job = enqueue_job(session, job_type=TEST_JOB_TYPE, payload={"x": 1}, actor="admin@example.com")
        session.commit()
        assert job.status == "pending"
        assert job.attempts == 0


def test_enqueue_job_is_idempotent(app):
    with app.state.session_factory() as session:
        first = enqueue_job(
            session,
            job_type=TEST_JOB_TYPE,
            payload={"x": 1},
            actor="admin@example.com",
            idempotency_key="dup-key",
        )
        session.commit()
        first_id = first.id

    with app.state.session_factory() as session:
        second = enqueue_job(
            session,
            job_type=TEST_JOB_TYPE,
            payload={"x": 2},
            actor="admin@example.com",
            idempotency_key="dup-key",
        )
        session.commit()
        assert second.id == first_id


def test_claim_job_only_claims_one_of_two_concurrent_attempts(app):
    with app.state.session_factory() as session:
        enqueue_job(session, job_type=TEST_JOB_TYPE, payload={}, actor="admin@example.com")
        session.commit()

    with app.state.session_factory() as session_a, app.state.session_factory() as session_b:
        claimed_a = claim_job(session_a, worker_id="worker-a")
        session_a.commit()
        claimed_b = claim_job(session_b, worker_id="worker-b")
        session_b.commit()
        assert claimed_a is not None
        assert claimed_b is None  # already claimed by worker-a


def test_run_job_success_marks_succeeded_with_result(app):
    with app.state.session_factory() as session:
        job = enqueue_job(session, job_type=TEST_JOB_TYPE, payload={"x": 42}, actor="admin@example.com")
        session.commit()
        job_id = job.id

    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        claim_job(session, worker_id="w1")  # claims this same job (only one pending)
        session.commit()

    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        run_job(session, job, actor="admin@example.com")
        session.commit()
        assert job.status == "succeeded"
        assert job.result_json is not None
        assert "42" in job.result_json


def test_try_claim_rejects_row_already_freshly_reclaimed_by_another_worker(app):
    """Regression test for a TOCTOU race in the stale-running reclaim path.

    Simulates two workers whose SELECT both saw job J as a stale-running
    candidate before either claimed it: worker A's guarded UPDATE runs
    first and refreshes `claimed_at` to "now"; worker B's guarded UPDATE
    must then fail, because J is no longer stale by the time B's WHERE
    clause re-evaluates against the current row. If the UPDATE's WHERE
    clause doesn't repeat the staleness check (only `status='running'`),
    B would incorrectly re-claim J and both workers would run its handler.
    """
    with app.state.session_factory() as session:
        job = enqueue_job(session, job_type=TEST_JOB_TYPE, payload={}, actor="admin@example.com")
        session.commit()
        job_id = job.id

    now = datetime.datetime.now(datetime.UTC)
    stale_before = now - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS)

    with app.state.session_factory() as session:
        # Worker A already reclaimed this stale job moments ago.
        row = session.get(Job, job_id)
        row.status = "running"
        row.claimed_by = "worker-a"
        row.claimed_at = now
        session.commit()

    with app.state.session_factory() as session:
        result = _try_claim(session, job_id, worker_id="worker-b", now=now, stale_before=stale_before)
        session.commit()
        assert result is None


def test_run_job_failure_retries_until_max_attempts(app):
    with app.state.session_factory() as session:
        job = enqueue_job(
            session, job_type=TEST_JOB_TYPE, payload={"fail": True}, actor="admin@example.com", max_attempts=2
        )
        session.commit()
        job_id = job.id

    for _ in range(2):
        with app.state.session_factory() as session:
            job = session.get(Job, job_id)
            job.status = "pending"
            job.available_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
            session.commit()
        with app.state.session_factory() as session:
            job = session.get(Job, job_id)
            run_job(session, job, actor="admin@example.com")
            session.commit()

    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        assert job.attempts == 2
        assert job.status == "failed"
        assert "deliberate failure" in job.error_message


def test_process_next_job_end_to_end(app, _register_echo_handler):
    with app.state.session_factory() as session:
        enqueue_job(session, job_type=TEST_JOB_TYPE, payload={"y": 7}, actor="admin@example.com")
        session.commit()

    processed = process_next_job(app.state.session_factory, worker_id="w1")
    assert processed is True
    assert _register_echo_handler == [{"y": 7}]

    processed_again = process_next_job(app.state.session_factory, worker_id="w1")
    assert processed_again is False  # nothing left pending


def test_unknown_job_type_fails_gracefully(app):
    with app.state.session_factory() as session:
        job = enqueue_job(session, job_type="no_such_handler", payload={}, actor="admin@example.com")
        session.commit()
        job_id = job.id

    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        claim_job(session, worker_id="w1")
        session.commit()

    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        run_job(session, job, actor="admin@example.com")
        session.commit()
        assert job.status in ("failed", "pending")  # never crashes the caller
        assert job.error_message
