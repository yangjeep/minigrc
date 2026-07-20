"""Claim/run/retry logic for the database-backed job queue.

Handlers register by job_type (`register_handler`). `enqueue_job` writes a
pending row (idempotent when an `idempotency_key` is given). `claim_job`
atomically claims one pending — or stale-running — job via a guarded
UPDATE (WHERE status='pending', so a concurrent claim attempt gets
rowcount=0 and returns None instead of double-processing). `run_job`
executes the registered handler, recording a structured result or a
retry/failure outcome with exponential backoff. `process_next_job` is the
claim+run unit both the worker loop and any synchronous inline caller use.

See ADR #24 (docs/decisions/architectural-decisions.md) for why this is a
DB-backed queue rather than Redis.
"""

from __future__ import annotations

import datetime
import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.audit import record_audit_event
from app.models import Job

logger = logging.getLogger(__name__)

JobHandler = Callable[[Session, dict[str, Any]], dict[str, Any]]

_HANDLERS: dict[str, JobHandler] = {}

# A running job whose worker crashed before finishing is reclaimed after
# this long — long enough that a real in-progress job (e.g. a bounded
# connection test) never gets double-run out from under itself.
STALE_RUNNING_THRESHOLD_SECONDS = 120

BACKOFF_BASE_SECONDS = 5


def register_handler(job_type: str, handler: JobHandler) -> None:
    _HANDLERS[job_type] = handler


def enqueue_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
    actor: str,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> Job:
    if idempotency_key is not None:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return existing

    job = Job(
        job_type=job_type,
        payload_json=json.dumps(payload),
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        created_by=actor,
    )
    session.add(job)
    session.flush()
    record_audit_event(
        session,
        entity_type="job",
        entity_id=job.id,
        action="enqueue",
        detail=f"Enqueued {job_type} job",
        actor=actor,
    )
    return job


def claim_job(session: Session, *, worker_id: str) -> Job | None:
    now = datetime.datetime.now(datetime.UTC)
    stale_before = now - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS)
    candidate_id = session.scalar(
        select(Job.id)
        .where(
            ((Job.status == "pending") & (Job.available_at <= now))
            | ((Job.status == "running") & (Job.claimed_at.is_not(None)) & (Job.claimed_at <= stale_before))
        )
        .order_by(Job.created_at)
        .limit(1)
    )
    if candidate_id is None:
        return None

    result = session.execute(
        update(Job)
        .where(
            Job.id == candidate_id,
            (Job.status == "pending") | (Job.status == "running"),
        )
        .values(status="running", claimed_by=worker_id, claimed_at=now)
    )
    if result.rowcount != 1:
        return None
    session.flush()
    return session.get(Job, candidate_id)


def claim_specific_job(session: Session, job_id: str, *, worker_id: str) -> Job | None:
    """Claim a known job by id rather than the oldest pending one — for a
    synchronous caller that just enqueued a job and wants to run exactly
    that job inline (e.g. a bounded connection test), not whatever else
    happens to be pending."""
    now = datetime.datetime.now(datetime.UTC)
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "pending")
        .values(status="running", claimed_by=worker_id, claimed_at=now)
    )
    if result.rowcount != 1:
        return None
    session.flush()
    return session.get(Job, job_id)


def run_job(session: Session, job: Job, *, actor: str) -> None:
    handler = _HANDLERS.get(job.job_type)
    job.attempts += 1
    try:
        if handler is None:
            raise RuntimeError(f"no handler registered for job_type '{job.job_type}'")
        payload = json.loads(job.payload_json)
        result = handler(session, payload)
        job.status = "succeeded"
        job.result_json = json.dumps(result)
        job.error_message = None
        detail = f"Job {job.job_type} succeeded on attempt {job.attempts}"
    except Exception as exc:  # noqa: BLE001 - any handler failure is a retryable job failure
        job.error_message = str(exc)[:2000]
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            detail = f"Job {job.job_type} failed permanently after {job.attempts} attempts: {exc}"
        else:
            job.status = "pending"
            backoff = BACKOFF_BASE_SECONDS * (2 ** (job.attempts - 1))
            job.available_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=backoff)
            detail = f"Job {job.job_type} attempt {job.attempts} failed, retrying in {backoff}s: {exc}"
    record_audit_event(
        session,
        entity_type="job",
        entity_id=job.id,
        action="run",
        detail=detail,
        actor=actor,
    )


def process_next_job(
    session_factory: sessionmaker[Session], *, worker_id: str, actor: str = "worker"
) -> bool:
    with session_factory() as session:
        job = claim_job(session, worker_id=worker_id)
        session.commit()
        if job is None:
            return False
        job = session.get(Job, job.id)
        run_job(session, job, actor=actor)
        session.commit()
        return True
