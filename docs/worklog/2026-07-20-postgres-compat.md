# Feature 4: PostgreSQL compatibility

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Fourth phase of the platform pivot (umbrella issue #5, PR #6), first of
the "checkpoint 1" group (Postgres + secrets). Adds `DATABASE_URL`
support and a Postgres-aware `build_engine`, with SQLite remaining the
fully-supported default. See the architecture checkpoint on issue #5 and
ADR #24 for the full library/security reasoning.

## Files Changed

- `app/db.py::build_engine` — accepts a bare SQLite path or a full
  SQLAlchemy URL; only attaches the SQLite `PRAGMA` listener for the
  sqlite dialect.
- `app/config.py` — `database_url` (reads unprefixed `DATABASE_URL`),
  `resolved_engine_target` property.
- `app/main.py::create_app` — uses `resolved_engine_target`; explicit
  `database_path`/`data_dir` (always passed by tests) clears
  `database_url` so tests can never accidentally target a real Postgres.
- `app/cli.py` — all four `build_engine` call sites updated; `migrate`'s
  stdout output now uses `engine.url.render_as_string(hide_password=True)`
  instead of the raw settings string (found while implementing — the
  original would have echoed a `DATABASE_URL` credential to stdout/logs).
- `pyproject.toml` — added `psycopg[binary]`.
- `.github/workflows/ci.yml` — new `test-postgres` job (postgres:16
  service container).
- `docs/architecture.md` — SQLite vs. Postgres section.
- `tests/test_postgres_compat.py` — 4 always-run dialect-selection unit
  tests + 1 gated live-migration test (skipped unless `TEST_DATABASE_URL`
  is set — no Postgres/Docker available in this local environment, so
  this test only actually runs in CI).

## Verification

- [x] Tests pass (`pytest` — 236 passed, 1 skipped locally)
- [x] Lint/format clean
- [x] No live Postgres available locally to test end-to-end — the
      dialect-selection logic is unit-tested directly, and the live
      migration round-trip test is gated to run in CI's new
      `test-postgres` job, which was pushed but not yet observed passing
      as of this entry (checked in the next worklog/checkpoint).

## Decisions & Alternatives Rejected

- `DATABASE_URL` unprefixed (not `GRC_DATABASE_URL`) — explicit user
  instruction, also matches the convention most Postgres-hosting
  platforms use.
- Did not parametrize the full 236-test suite across both dialects —
  scoped to proving migration/schema portability (the highest-risk item)
  via a dedicated gated test file rather than rearchitecting every
  existing SQLite-`tmp_path`-based fixture. Documented as a known
  follow-up below.
- Model audit found no SQLite-specific assumptions needing a fix —
  columns already use portable SQLAlchemy types, and existing migrations
  already use `op.batch_alter_table` (required for SQLite, harmless
  no-op-wrapper on Postgres).

## Known Gaps / Follow-ups

- Full test suite is not yet parametrized to run against both SQLite and
  Postgres — only the dedicated compat test file runs against a live
  Postgres in CI. If a future bug surfaces that's dialect-specific and
  not caught by the current tests, that's the trigger to revisit.
- No automated SQLite→Postgres data-migration tool — documented as an
  operator-run ETL step (e.g. `pgloader`) in `docs/architecture.md`.
