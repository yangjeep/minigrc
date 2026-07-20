# Feature 7: worker + persisted job foundation

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Seventh phase of the platform pivot (umbrella issue #5, PR #6), finishes
"checkpoint 2" (external connections + worker). Adds a database-backed
job queue (no Redis, per ADR #24) and moves Feature 6's connection test
onto it as the first real job type.

## Files Changed

- `app/models.py` — `Job` model.
- `app/jobs.py` — `enqueue_job`, `claim_job`, `claim_specific_job`,
  `run_job`, `process_next_job`, `register_handler`.
- `migrations/versions/3061d73bef5e_add_jobs_table.py`.
- `app/worker.py` — `python -m app.worker` entrypoint, graceful
  SIGTERM/SIGINT shutdown.
- `app/connections.py` — `_connection_test_job_handler`, registered as
  `job_type="connection_test"`.
- `app/routers/connections.py` — `test_connection_route` now enqueues +
  claims + runs a job instead of calling the connection-test function
  directly.
- `compose.yaml` — new `worker` service.
- `tests/test_jobs.py` (7), plus one new test in
  `tests/test_connections_router.py` exercising the route end-to-end
  through the job system.

## Verification

- [x] Tests pass (`pytest` — 267 passed, 1 skipped)
- [x] Lint/format clean
- [x] Ran the actual `python -m app.worker` binary against a scratch
      database with one real queued job (a deliberately unregistered job
      type, to observe the failure/retry path): confirmed the worker
      claimed it (`claimed_by` populated with a real worker id),
      executed it, recorded the expected error, rescheduled it as a
      retry (attempts=1, status back to `pending` with backoff), then
      shut down cleanly on `SIGTERM`.
- [x] `test_connection_test_route_runs_through_job_system` proves the
      admin UI's "Test connection" button still works end to end and
      that exactly one `Job` row is created with `job_type=connection_test`.
      No new browser pass needed — the UI/UX is unchanged from Feature 6
      (same synchronous flash-redirect flow), already screenshotted then.

## Decisions & Alternatives Rejected

- **Job succeeds even when the tested connection fails.** The job's
  responsibility is "run the test and record the outcome" — a bounded,
  reachable-but-refused connection is a normal, successful job. Only an
  infrastructure-level problem (unknown connection id, no handler
  registered) triggers a job retry/failure. Getting this wrong would have
  meant every unreachable customer database silently retried 3 times
  with backoff for no reason.
- **`claim_specific_job` added alongside `claim_job`** so a synchronous
  caller (the connection-test route) can claim exactly the job it just
  enqueued, not whatever's oldest-pending — `claim_job`'s FIFO semantics
  are for the actual worker loop, not a caller that wants its own result
  back immediately.
- **Stale-running reclaim (120s)** rather than a heartbeat mechanism —
  simplest thing that recovers from a crashed worker without adding a
  second periodic write per in-flight job.
- Found and fixed a real bug while wiring the connection-test route: the
  web process never imported `app.connections`, so the `connection_test`
  handler was never registered there — the inline claim+run would have
  failed with "no handler registered" on every real request.

## Known Gaps / Follow-ups

- No job-cancellation endpoint (spec allowed skipping this "if reasonably
  achievable" — a `pending`/`running` job simply runs to completion or
  final failure).
- No admin UI to browse job history yet — `audit_events` records every
  enqueue/run via the existing audit log, which covers auditability, but
  there's no dedicated jobs dashboard. Deferred until a feature actually
  needs one (Feature 8's imports will be the second job-type caller).
