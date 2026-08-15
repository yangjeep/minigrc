# Issue #22: event store, projector/rebuild foundation, exclusive SQLite/PostgreSQL backend contract

**Date:** 2026-08-15
**Author:** Claude (agent)
**Type:** feat

## Summary

Adds the generic, immutable domain-event store and projector/rebuild
primitives required by epic #21 before #11 (control operations) and later
compliance domains can become event-backed, plus closes a real gap in the
SQLite/PostgreSQL exclusive-backend contract: mixed `DATABASE_URL` +
`GRC_DATABASE_PATH` configuration is now rejected at startup instead of
silently resolved by precedence. This is infrastructure only — no existing
domain (`InternalControl`, `Policy`, `Risk`, etc.) is migrated onto it, and
no routes/UI were added.

## Files Changed

- `app/events.py` (new) — `DomainEvent` model, `append_event`/
  `append_and_project`/`rebuild_projection`, immutability guards
  (per-instance + ORM-level bulk statements), `ConcurrentAppendError`.
- `app/config.py` — `reject_ambiguous_database_config` (plain function,
  not a pydantic validator — see rationale below).
- `app/main.py` — calls `reject_ambiguous_database_config` right before
  building the engine; logs the resolved backend dialect (never the URL).
- `migrations/env.py` — imports `app.events` so autogenerate sees the new
  table.
- `migrations/versions/045d4fcc315f_add_domain_events_table.py` (new) —
  additive-only; reviewed by hand, downgrade verified.
- `tests/test_events.py` (new), `tests/test_config.py` (new),
  `tests/test_db_init.py`, `tests/test_postgres_compat.py` — see
  Verification.
- `docs/superpowers/specs/2026-08-15-issue22-event-store-design.md` (new)
  — design doc, including a §12 recording adversarial-review fixes.

## Verification

- [x] Tests pass (`pytest`) — 402 baseline + 24 new/changed = 426 tests,
      425 passed / 1 skipped (the Postgres-gated live-migration test — no
      Docker/Postgres available in this sandbox; see below).
- [x] Lint/format clean (`ruff check .`, `ruff format --check .`) for
      every file in this change (an untracked, unrelated `#11` design doc
      left in the working tree by a prior session is excluded from both —
      it is not part of this commit).
- [x] Migration verified by hand: additive-only (`create_table`, no
      existing table touched), `alembic upgrade head` / `downgrade -1` /
      `upgrade head` cycle exercised locally against a throwaway dev
      SQLite DB.
- [x] Independent adversarial code review (`pr-review-toolkit:code-reviewer`,
      fresh context) — see Decisions & Alternatives Rejected for what it
      found and how each finding was addressed.
- [ ] PostgreSQL live-migration test (`tests/test_postgres_compat.py::
      test_migrations_apply_cleanly_against_postgres`) — not run in this
      sandbox (no Docker/local Postgres available; `psycopg` installs and
      imports fine, but there's no server to connect to). This repo's own
      convention already treats this test as CI-only (see
      `docs/worklog/2026-07-20-postgres-compat.md`), so verification for
      this issue relies on the same CI `test-postgres` job once pushed —
      report its actual result rather than assuming green.

## Decisions & Alternatives Rejected

- **Proved the mechanism with a throwaway test-only aggregate** (a
  fictional "widget" with a projection table created via raw SQL scoped
  to `tests/test_events.py`), not a new shipped product concept — issue
  #22 explicitly says not to implement #11 domain objects here, and this
  keeps the diff infrastructure-only.
- **`payload_json` is `Text` + `json.dumps`/`json.loads`, not SQLAlchemy's
  `JSON` type** — matches `Job.payload_json`/`Job.result_json` exactly,
  whose docstring states the reason directly (portable across SQLite/
  Postgres without a JSON column-type dependency). Caught by re-reading
  the actual model/migration directly rather than trusting an earlier
  inspection pass's paraphrase, which had described it as a `JSON` column.
- **An independent adversarial review (fresh subagent, no shared context)
  found and reproduced several real defects in the first pass**, each
  fixed rather than argued away:
  - `append_and_project` re-ran the projector on an idempotent replay,
    contradicting its own intended "no re-projection" behavior — fixed by
    reporting whether `append_event` actually created a new row.
  - The retry loop treated every `IntegrityError` as sequence contention,
    masking real caller bugs (e.g. a NOT NULL violation) as a misleading
    `ConcurrentAppendError` with the original exception discarded — fixed
    by confirming the exact sequence slot is actually occupied before
    retrying, and chaining `raise ... from` otherwise.
  - The idempotency-key TOCTOU race was wider than intended (checked only
    once, before the retry loop) — fixed by re-checking on every attempt.
  - `session.flush()` (whole-session) inside the append retry could be
    derailed by unrelated pending objects elsewhere in the same
    transaction — fixed with `session.flush(objects=[candidate])`.
  - `before_update`/`before_delete` (per-instance) don't cover ORM-level
    bulk `update()`/`delete()` statements, which are ordinary application
    code in this repo's own idiom — fixed with a `Session`-level
    `do_orm_execute` guard.
  - `rebuild_projection` ordered replay by `aggregate_id` (a random UUID4
    hex, not sortable), which doesn't reproduce real historical order for
    a projection that depends on cross-aggregate interleaving — fixed by
    ordering on `recorded_at` first.
  - The backend-selection check was a pydantic `model_validator`, which
    ran at `Settings()` construction time — before `create_app`'s
    test-only `database_path` override (which clears `database_url`) had
    a chance to apply, and its `ValidationError` risked echoing tail
    characters of `encryption_key` into a startup error message. Fixed by
    making it a plain function called once, after settings are fully
    resolved, right before the engine is built.
  - See `docs/superpowers/specs/2026-08-15-issue22-event-store-design.md`
    §12 for the complete list, including two findings reviewed and
    deliberately left unfixed (SQLite not enforcing `VARCHAR` length,
    and `DateTime` dropping tz-awareness) because both match this
    codebase's existing, repo-wide convention rather than being unique to
    this table — fixing them here would be inconsistent scope expansion.
  - The review also surfaced one adjacent, pre-existing bug unrelated to
    this issue: `app/db.py::init_db` builds Alembic's `sqlalchemy.url`
    from `str(engine.url)`, which masks the password — `alembic upgrade
    head` would fail to authenticate against a real password-protected
    Postgres server. Not introduced here, not caught by CI (the Postgres
    service container is passwordless), left for a follow-up issue.
- **No separate `ix_domain_events_aggregate_type` index** — the
  `uq_domain_event_aggregate_sequence` unique constraint already provides
  an equally usable leading-column index for the same lookups; a
  redundant index would be pure write amplification on an append-only,
  write-heavy table.

## Known Gaps / Follow-ups

- PostgreSQL live-migration verification for `domain_events` specifically
  has not been observed passing in this session (no local Postgres) —
  confirm the `test-postgres` CI job actually goes green on the pushed
  branch before treating Postgres support for this table as verified,
  not just "should work by construction."
- The residual idempotency-key race (a concurrent commit landing between
  the in-loop recheck and this call's own insert attempt) is accepted as
  a narrow, documented gap at this app's single-process scale — same
  class of gap as `ImportJob`'s documented idempotency race (ADR #25),
  not something this issue attempts to close completely.
- `app/db.py::init_db`'s Alembic-URL password-masking bug (found during
  review, pre-existing, out of scope here) should get its own follow-up
  issue before a password-protected Postgres deployment relies on it.
- #11 (control operations) is the next dependency in the roadmap per
  issue #35 / `.agent/LOOP.md` — it should reconcile its own,
  already-drafted-but-uncommitted design doc against this event-store
  foundation before implementation, per issue #35's explicit ordering
  ("#22 completed before #11 implementation proceeds").
