# Issue #22: Event store, projector/rebuild foundation, exclusive SQLite/PostgreSQL backend contract

**Status:** design + implementation, this branch (`yangjeep/issue-22-event-store`).
**Parent epic:** #21. **Blocks:** #11. **Author:** Claude (agent), 2026-08-15.

Grounded in the actual current repository state at `origin/main` commit `2d0ac20`
(`app/config.py`, `app/db.py`, `app/models.py`, `app/audit.py`, `app/jobs.py`,
migrations, `tests/`), not on the PRD's or issue's prose description of what
supposedly exists. See "Current-state findings" below for where reality
differs from what a naive reading of the issue would assume.

## 0. Current-state findings that shape this design

- **PostgreSQL support already exists**, contrary to the (superseded) root
  `CLAUDE.md` this session initially loaded before switching to `origin/main`.
  `app/db.py::build_engine` already dialect-switches on `"://"` in the target
  string; `DATABASE_URL` (unprefixed, by convention) already routes to
  Postgres via `psycopg[binary]`. A dedicated, CI-gated live-Postgres test
  (`tests/test_postgres_compat.py::test_migrations_apply_cleanly_against_postgres`)
  already proves migrations + ORM writes work against a real `postgres:16`
  container — but only in CI (`test-postgres` job); there is no Docker/Postgres
  available in this sandbox to re-verify it locally, and the worklog that
  introduced it (`docs/worklog/2026-07-20-postgres-compat.md`) explicitly
  says it hadn't been observed passing at the time it was written. This
  design reuses that exact test file/CI-gating convention rather than
  inventing a new one; Postgres verification for this issue is reported as
  a CI result once pushed, not a locally-reproduced one.
- **The backend-selection contract has a real gap.** `Settings.resolved_engine_target`
  (`app/config.py:60-63`) silently prefers `database_url` over
  `database_path`/`data_dir` with no validation — issue #22 explicitly
  requires ambiguous/mixed configuration to be *rejected*, and a test proving
  it's "rejected or otherwise impossible by construction." Today it is
  neither: it's a silent, undocumented-to-the-operator precedence rule. This
  design adds a `pydantic` `model_validator` that raises when both
  `DATABASE_URL` and `GRC_DATABASE_PATH` are set, closing that gap. (`data_dir`
  alone is not part of the ambiguity check — it defaults to `"./data"` and is
  also used for non-DB storage, so requiring it be unset whenever
  `DATABASE_URL` is set would reject harmless/typical configurations.)
- **No event-sourcing/replay pattern exists yet.** The codebase has several
  bespoke *immutable-row* patterns (`PolicyVersion`, `VendorUserSnapshot`/`Row`,
  `PolicyApprovalSnapshot`) and one *mutable-row-plus-audit-log* pattern
  (`AuditEvent` via `app/audit.py::record_audit_event`, called inline by every
  router in the same transaction as the mutation it describes) — but nothing
  that stores an aggregate as a sequence of events and derives current state
  by replaying them. This issue adds that primitive; it does not touch any
  existing model.
- **The `jobs` table (`app/jobs.py`, ADR #24) has the closest-related
  concurrency-safety patterns already proven in this codebase**: a unique
  `idempotency_key` with check-then-insert, named `CHECK` constraints for
  status enums, and a guarded `UPDATE ... WHERE <full predicate>` idiom for
  race-safe claiming. This design reuses the idempotency-key and named-constraint
  conventions directly; it does not reuse the job-queue/claim machinery itself,
  since an event store is an append-only log, not a work queue.
- **Router/service mutation pattern**: business logic lives inline in routers
  (`db.add()` → `db.flush()` inside `try/except IntegrityError` → `record_audit_event(...)`
  → redirect), with exactly one small precedent for a shared plain-function
  helper (`app/requirements.py::add_requirement`, used by 3 call sites to keep
  a two-row invariant in sync). The new `append_event`/`append_and_project`
  functions follow that same "plain function, no framework" shape.

## 1. Scope

Build the generic event-store + projector/rebuild infrastructure only.
Do **not** migrate any existing domain (`InternalControl`, `Policy`,
`Risk`, etc.) onto it in this slice — that is #11's job, and only for the
control-operations domain it actually introduces. This issue proves the
mechanism with a test-only representative aggregate (`tests/test_events.py`),
not a new shipped product concept, so the diff stays infrastructure-only
and doesn't invent speculative domain surface (`CLAUDE.md` rule 2 /
`.agent/RULES.md` §1.11 "no speculative abstraction without a second caller").

## 2. Event-store schema — `DomainEvent` (new table `domain_events`)

New model in a new module `app/events.py` (not `app/models.py` — kept
separate because this is generic infrastructure, not a compliance-domain
entity; it still uses the shared `app.db.Base`).

| column | type | notes |
|---|---|---|
| `id` | `String(32)`, PK | `new_id()` — same 32-char hex UUID4 scheme as every other table (`app/models.py:29-30`). Not sortable; use `recorded_at`/`aggregate_sequence` for order. |
| `aggregate_type` | `String(100)`, not null | e.g. `"control_occurrence"`. Free string, not an enum — new aggregate types are added by callers, not by editing this table. |
| `aggregate_id` | `String(32)`, not null | The id of the domain aggregate this event belongs to (a `new_id()`-shaped id minted by the caller, not necessarily a FK to any single table). |
| `aggregate_sequence` | `Integer`, not null | 1-based, monotonic per `(aggregate_type, aggregate_id)`. |
| `event_type` | `String(100)`, not null | e.g. `"ControlOccurrencePerformed"`. |
| `schema_version` | `Integer`, not null, default `1` | Payload shape version for this `event_type`. |
| `payload_json` | `Text`, not null | Immutable event-specific data, JSON-encoded via plain `json.dumps`/`json.loads` at the call site — **not** SQLAlchemy's `JSON` column type. This matches `Job.payload_json`/`Job.result_json` exactly (`app/models.py:874-875`), whose docstring states the reason directly: "plain JSON-encoded text (portable across SQLite/Postgres without a JSON column type dependency)" (`app/models.py:853-854`). Verified by reading the actual migration (`3061d73bef5e_add_jobs_table.py:28`, `sa.Text()`) rather than trusting a paraphrase — an earlier inspection pass described this column as using a `JSON` type, which the migration and model both contradict. |
| `occurred_at` | `DateTime`, not null | Business time: when the fact actually happened. Defaults to `recorded_at` unless the caller backdates it (migration/import facts only — see §5). |
| `recorded_at` | `DateTime`, not null, default `utcnow()` | System time: when this row was appended. Never caller-supplied. |
| `actor_type` | `String(30)`, not null, default `"system"` | `"user" \| "system" \| "migration"`. |
| `actor_id` | `String(32)`, nullable | e.g. `User.id` when `actor_type == "user"`. |
| `correlation_id` | `String(32)`, nullable | Optional cross-event/cross-aggregate correlation. |
| `causation_id` | `String(32)`, nullable | Optional: the `id` of the event that caused this one. |
| `idempotency_key` | `String(200)`, nullable, unique | Caller-supplied stable key for retry-safe append (same shape as `Job.idempotency_key`). |

Constraints (all explicitly named, per the existing migration convention —
`647102981d1c_initial_mvp_schema.py`, `3061d73bef5e_add_jobs_table.py`):

- `UniqueConstraint(aggregate_type, aggregate_id, aggregate_sequence, name="uq_domain_event_aggregate_sequence")` — no two events can claim the same sequence slot for the same aggregate.
- `UniqueConstraint(idempotency_key, name="uq_domain_event_idempotency_key")` — safe with a nullable column: both SQLite and PostgreSQL treat multiple `NULL`s as non-conflicting under a unique constraint (same as `Job.idempotency_key` today).
- `CheckConstraint(aggregate_sequence >= 1, name="ck_domain_event_aggregate_sequence_positive")`.

**Immutability enforcement.** Beyond "no update/delete route is exposed"
(the pattern every other immutable table in this repo relies on), this
table adds an actual technical guard: SQLAlchemy `before_update`/`before_delete`
mapper-event listeners on `DomainEvent` that raise `RuntimeError` unconditionally.
This is directly required by the issue's own test list ("Normal application
code cannot mutate/delete existing domain events") and is cheap/local to
`app/events.py` — it does not require a DB trigger or a second enforcement
layer.

## 3. Append + idempotency + aggregate sequencing

```python
def append_event(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict,
    occurred_at: datetime | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    idempotency_key: str | None = None,
    schema_version: int = 1,
) -> DomainEvent
```

Behavior:

1. If `idempotency_key` is given, look up an existing `DomainEvent` with that
   key first (`select(...).where(DomainEvent.idempotency_key == idempotency_key)`)
   and return it unchanged if found — no new row, no re-projection. This is
   the exact check-before-insert idiom `app/jobs.py::enqueue_job` already
   uses for `Job.idempotency_key`.
2. Otherwise, compute the next `aggregate_sequence` for
   `(aggregate_type, aggregate_id)` as `max(existing) + 1` (or `1` if none),
   and attempt the insert inside a `SAVEPOINT` (`session.begin_nested()`).
   If the unique-sequence constraint is violated (a genuine concurrent
   append to the same aggregate — rare at this app's scale, but the issue
   explicitly requires it be rejected, not silently corrupted), the
   savepoint is rolled back and the sequence is recomputed and retried, up
   to a small fixed number of attempts, after which a `ConcurrentAppendError`
   is raised for the caller to surface as a conflict (analogous to how
   `app/registers/router.py` surfaces `409` on a stale `expected_updated_at`).
3. `recorded_at` is always `utcnow()` at append time — never caller-supplied.
   `occurred_at` defaults to the same value unless the caller passes one
   explicitly (only for documented migration/import facts, per §5).

`append_and_project` composes this with immediate projection update in the
same DB transaction (the caller's existing request-scoped session — same
discipline as `record_audit_event`, which never commits itself and relies
on `app/deps.py::get_db`'s commit-at-request-end boundary):

```python
ProjectorFn = Callable[[Session, DomainEvent], None]


def append_and_project(
    session: Session,
    projectors: dict[str, ProjectorFn],
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict,
    **kwargs,
) -> DomainEvent:
    event = append_event(
        session,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        **kwargs,
    )
    projectors[event.event_type](session, event)
    return event
```

`projectors` is a plain `dict` the *caller* owns and passes in — there is no
global mutable registry. This matches `.agent/RULES.md`'s "no speculative
plugin/connector SDK" guidance: the first real caller (#11) defines its own
`CONTROL_OCCURRENCE_PROJECTORS = {...}` next to its own event types; this
module does not need to know about it.

## 4. Projection rebuild

```python
def rebuild_projection(
    session: Session,
    projectors: dict[str, ProjectorFn],
    *,
    aggregate_type: str,
    reset: Callable[[Session], None],
) -> None:
    reset(session)
    events = session.scalars(
        select(DomainEvent)
        .where(DomainEvent.aggregate_type == aggregate_type)
        .order_by(DomainEvent.aggregate_id, DomainEvent.aggregate_sequence)
    )
    for event in events:
        projectors[event.event_type](session, event)
```

`reset` is a caller-supplied callable that deletes/zeroes the aggregate
type's projection rows — kept as a parameter rather than inferred, since
this module has no way to know what a given aggregate's projection table(s)
look like. Ordering by `(aggregate_id, aggregate_sequence)` is sufficient
for determinism because a projection for one aggregate instance only
depends on that instance's own event history, not on interleaving with
other aggregates' events.

An unrecognized `event_type` (no entry in `projectors`) raises `KeyError`
immediately rather than silently skipping — a projector gap must fail loud
during rebuild, not produce a silently-incomplete projection.

## 5. `occurred_at` vs `recorded_at`, actor semantics, no forged history

- `recorded_at` is unconditionally `utcnow()` — this row provably did not
  exist in the database before this instant.
- `occurred_at` defaults to `recorded_at`. A caller may only pass a
  different `occurred_at` for genuine backdating (e.g. a migration/import
  path recording that a fact became true earlier than the moment it was
  captured into miniGRC) — and per the issue's explicit constraint, such
  a caller must always also pass `actor_type="migration"`, never
  `"user"`, so a later reader can never mistake a migration-authored
  historical fact for a real-time human action. This design does not
  add a runtime guard forcing `actor_type="migration"` whenever
  `occurred_at != recorded_at`, since this issue does not introduce any
  migration/import caller yet (§7) — this is a documented obligation for
  whichever future issue (#11's own migration, if any) is the first to
  actually backdate an event, not enforced here speculatively.

## 6. Backend-selection contract fix

`app/config.py::Settings` gains:

```python
@model_validator(mode="after")
def _reject_ambiguous_database_config(self) -> "Settings":
    if self.database_url and self.database_path:
        raise ValueError(
            "Ambiguous database configuration: both DATABASE_URL and "
            "GRC_DATABASE_PATH are set. A miniGRC deployment must select "
            "exactly one relational backend for this deployment."
        )
    return self
```

Verified safe against every existing call site:

- `app/main.py::create_app` (lines 81-99) calls `get_settings()` (the real,
  validated constructor path) *first*, then — only when an explicit test
  `database_path`/`data_dir` is given — calls `settings.model_copy(update={...,
  "database_url": ""})`. `model_copy(update=...)` does not re-run validators
  in Pydantic v2, so the test-only override path is unaffected either way;
  and since normal test/dev environments don't set both `DATABASE_URL` and
  `GRC_DATABASE_PATH` simultaneously, the initial `get_settings()` call
  never actually hits the new branch during the existing test suite.
- Real operator misconfiguration (both env vars set at process start) now
  fails fast at `get_settings()` with a clear message, instead of silently
  picking `DATABASE_URL` and ignoring `GRC_DATABASE_PATH` without telling
  the operator.

**Startup diagnostics.** `create_app` gains one `logger.info` line stating
the resolved backend dialect (`"sqlite"` or `"postgresql"`, from
`engine.url.get_backend_name()`) — never the full URL, which may embed
credentials.

## 7. Migration/backfill decisions

- One additive Alembic migration creates `domain_events` with the
  constraints in §2. No existing table is touched.
- **No existing mutable domain is backfilled into events in this slice.**
  Per the issue's explicit instruction ("Do not invent historical domain
  events for current mutable rows... Document the migration strategy/gap
  for follow-up"): `InternalControl`, `Policy`, `Risk`, etc. keep their
  current mutable-row-plus-`AuditEvent` pattern until #11 (or a dedicated
  follow-up) deliberately migrates a specific domain. Inventing
  `ControlCreated`/`PolicyApproved`/etc. events for rows that were never
  actually created through this event store would fabricate provenance
  this design explicitly must not create.

## 8. SQLite/PostgreSQL behavior

No dialect-specific branching in `app/events.py` — the model uses portable
SQLAlchemy types only (`String`, `Integer`, `DateTime`, `Text`), the same
set already used by `Job`/`ImportJob`. The migration uses plain
`op.create_table(...)` (no `batch_alter_table` needed — that idiom is only
required for *altering* an existing SQLite table, not creating a new one).
Postgres-specific read optimization (materialized views over
`domain_events` for a future readiness dashboard) is explicitly out of
scope here — no PRD/issue requirement or concrete query yet justifies one
(`.agent/RULES.md` §1.5 / `#22`'s own "do not introduce speculative MVs").

Testing follows the repository's existing, only dual-backend pattern
exactly: SQLite gets full coverage for free (every test uses the real
file-backed SQLite fixture in `tests/conftest.py`); Postgres-specific
verification extends `tests/test_postgres_compat.py` — `domain_events`
is added to `PIVOT_TABLES`, and the existing
`test_migrations_apply_cleanly_against_postgres` gets one more ORM
round-trip creation plus a negative test for the new
`ck_domain_event_aggregate_sequence_positive` check constraint. No new
CI job, marker, or test file is introduced.

## 9. Security/integrity review (addressed here; re-verified adversarially post-implementation)

- **Forged actor identity**: `actor_type`/`actor_id` are plain caller-supplied
  strings — this module does not authenticate them. Callers (future #11
  routers) are responsible for passing the authenticated request's actor,
  exactly as `record_audit_event(..., actor=request.state.user.email)`
  already does everywhere. Out of scope for this module to re-verify.
- **Event backdating**: addressed in §5 — `recorded_at` cannot be forged by
  a caller; `occurred_at` can only diverge from it via explicit caller
  choice, which this issue documents but does not yet exercise.
- **Idempotency abuse**: a caller reusing another aggregate's idempotency
  key would silently receive that other event back from `append_event`
  (since the lookup is keyed only on `idempotency_key`, not also
  `aggregate_type`/`aggregate_id`). Mitigation: document that
  `idempotency_key` values must be namespaced by the caller (e.g.
  `f"{aggregate_type}:{aggregate_id}:{command_name}"}`), matching how
  `Job.idempotency_key` is used today (caller-namespaced, not enforced by
  this module). Not enforcing a namespace format here avoids inventing a
  schema for a key format no real caller has demonstrated yet.
- **Aggregate sequence races/concurrency**: handled by the unique
  constraint + savepoint-retry in §3.
- **Raw SQL/update paths that could mutate history**: the mapper-level
  `before_update`/`before_delete` guards in §2 block ORM-level mutation;
  they do not block a raw `UPDATE`/`DELETE` issued outside the ORM (e.g. a
  manual DB console session) — no automated system can prevent that, and
  no existing immutable table in this repo (`PolicyVersion`,
  `VendorUserSnapshotRow`, `AuditEvent`) attempts to either.
- **JSON/payload validation**: `payload_json` accepts any JSON-serializable
  dict; this module does not validate per-`event_type` payload shape
  (that belongs to each aggregate's own projector, which knows its event
  types' shapes — this module stays schema-agnostic, only `schema_version`
  is tracked generically).
- **Migration integrity**: additive-only migration, reviewed by hand
  before commit (§10).
- **Database URL/credential leakage**: the new startup log line
  (§6) logs only the dialect name, never `settings.database_url` or
  `settings.resolved_engine_target`.
- **Backend-selection ambiguity**: closed by §6.

## 10. Test strategy by layer

- **Unit**: `append_event` sequencing (1, 2, 3... per aggregate), duplicate
  sequence rejected via retry-then-error, idempotency-key short-circuit
  returns the same row without incrementing sequence, `before_update`/
  `before_delete` guards raise, `Settings` validator rejects
  `database_url` + `database_path` both set, `build_engine`'s existing
  dialect-selection tests are unaffected.
- **Regression**: N/A — no prior defect being fixed here; every test above
  is new-behavior coverage, not a regression test.
- **Integration**: `tests/test_events.py` demonstrates one full
  representative command → event → projection → rebuild flow (test-only
  aggregate, real `DomainEvent` table, real `append_and_project`/
  `rebuild_projection`) against the real SQLite test DB; the same flow's
  schema/constraint portability is additionally proven via the
  `tests/test_postgres_compat.py` extension (§8), which runs for real
  against Postgres only in CI's `test-postgres` job.
- **Browser/E2E**: not applicable — this issue adds no route/template/UI
  surface. Will apply once #11 exposes control-occurrence events through
  the app.
- **Claude Desktop UAT**: not applicable for the same reason — pure
  backend infrastructure, nothing user-visible to click through. Reported
  as UAT: N/A (not PENDING — PENDING implies a user-visible feature is
  awaiting acceptance; there is none yet).

## 11. Downstream compatibility — what #11 should build on

- Use `app/events.py::append_event`/`append_and_project`/`rebuild_projection`
  directly; do not create a second event-store table or a parallel
  mutable-row-plus-audit-log pattern for control occurrences.
- Define control-occurrence event types (`ControlOccurrenceExpected`,
  `ControlOccurrencePerformed`, etc.) and their own
  `CONTROL_OCCURRENCE_PROJECTORS` dict + projection table(s) — this module
  intentionally does not predefine them.
- Continue calling `record_audit_event` for non-material/administrative
  audit trail entries as today; only genuinely material compliance facts
  (per `.agent/RULES.md` §1.2-1.3) need to become `DomainEvent`s.
- Namespace any `idempotency_key` the control-occurrence domain uses,
  per §9.

## 12. Post-adversarial-review revisions

An independent adversarial-review pass (`pr-review-toolkit:code-reviewer`,
reproducing every finding with an executable script rather than inferring)
found real defects in the first implementation. Sections §3/§4/§6 above
describe the **as-implemented, revised** design; this section records what
changed and why, since the inline code samples in §3/§4 were not
re-transcribed line-for-line after these fixes — `app/events.py` and
`app/config.py` are the source of truth for exact code.

- **Idempotent replay re-ran the projector.** `append_and_project` always
  called the projector even when `append_event` returned an existing row
  from an idempotent replay, directly contradicting this doc's own §3
  claim ("no new row, no re-projection"). Fixed by having the internal
  helper report whether a row was actually created; `append_and_project`
  only projects when it was.
- **The retry loop treated every `IntegrityError` as sequence contention**,
  silently retrying (and eventually masking as `ConcurrentAppendError`,
  without `raise ... from`) genuine caller bugs like a NOT NULL violation.
  Fixed: after a failed insert, the retry loop checks whether another row
  now actually occupies the exact `(aggregate_type, aggregate_id,
  aggregate_sequence)` slot just attempted — only that confirms a genuine
  sequence race worth retrying; anything else re-raises the original
  error immediately, and the final `ConcurrentAppendError` (if attempts
  are exhausted) chains `from` the last real error.
- **The idempotency-key TOCTOU race was wider than documented** — the
  original code checked for an existing key only once, before the retry
  loop, so a concurrent commit landing after that check (but before this
  call's own insert) produced a hard `ConcurrentAppendError` instead of
  the intended idempotent return. Fixed: the idempotency-key lookup is
  re-run at the start of every retry attempt, closing the window down to
  the residual, accepted narrow race between that re-check and the
  immediately-following insert (same class of gap as `ImportJob`'s
  documented idempotency race, ADR #25).
- **`session.flush()` (no argument) flushed the whole session**, not just
  the candidate event being appended — an unrelated pending object
  elsewhere in the same transaction could fail the flush and leave the
  session needing a rollback the retry loop didn't expect. Fixed:
  `session.flush(objects=[candidate])` scopes the flush to just the
  event being appended.
- **The immutability guards didn't cover ORM-level bulk statements** —
  `before_update`/`before_delete` are per-instance mapper events; they
  don't fire for `session.execute(update(DomainEvent)...)`,
  `session.execute(delete(DomainEvent)...)`, or the legacy
  `session.query(DomainEvent).update()/.delete()`, all of which are
  ordinary application code, not a DBA console. Fixed by adding a
  `Session`-level `do_orm_execute` listener that rejects any ORM-level
  UPDATE/DELETE bound to `DomainEvent`. This still cannot (and is not
  intended to) block raw SQL issued outside the ORM — same limitation as
  every other immutable table in this repo.
- **`rebuild_projection`'s ordering (`aggregate_id, aggregate_sequence`)
  does not reproduce real historical order** for a projection whose state
  depends on interleaving *between* aggregates, not only on each
  aggregate's own history — `aggregate_id` is a random UUID4 hex, not
  sortable. Fixed: ordered by `(recorded_at, aggregate_type,
  aggregate_id, aggregate_sequence)` — real append time first, with the
  old per-aggregate key retained only as a deterministic tiebreak.
- **The backend-selection check was a pydantic `model_validator`**, which
  ran at `Settings()` construction — including inside `create_app`'s
  `get_settings()` call, *before* its test-only `database_path` override
  (which always clears `database_url`) is applied. An ambiguous ambient
  environment (both `DATABASE_URL` and `GRC_DATABASE_PATH` set) would
  therefore reject even when the caller was about to override it, and a
  validator failure also embeds a truncated repr of the raw input dict —
  risking `encryption_key`'s tail characters leaking into the error
  string. Fixed: `reject_ambiguous_database_config` is now a plain
  function, called once in `create_app` right after the settings/override
  are fully resolved (immediately before `build_engine`), raising a plain
  `RuntimeError` with no pydantic error-formatting involved.
- **Redundant index removed**: `ix_domain_events_aggregate_type` was
  dropped from both the model and the migration — the
  `uq_domain_event_aggregate_sequence` unique constraint already provides
  an equally usable leading-column index for the same lookups, so the
  extra index was pure write amplification on an append-only table.
- **Test tightening**: the two `DomainEvent`-specific negative tests added
  to `tests/test_postgres_compat.py` assert `sqlalchemy.exc.IntegrityError`
  specifically rather than bare `Exception` (the pre-existing tests in
  that file keep their original bare-`Exception` form — matching them
  exactly rather than "fixing" unrelated pre-existing code). The
  backdating test now round-trips through the DB (`session.expire` +
  re-read) instead of asserting only the in-memory attribute.

**Reviewed and deliberately not changed:**

- SQLite does not enforce `VARCHAR(N)` length the way PostgreSQL does, so
  an over-length `aggregate_id`/`idempotency_key` would silently succeed
  on SQLite and fail on Postgres. This matches the existing, repo-wide
  convention (no other `String(N)` column in `app/models.py` adds a
  `CHECK(length(...))` guard either); adding one uniquely for this table
  would be inconsistent scope expansion, not a fix.
- `DateTime` (no `timezone=True`) silently drops the tz-aware offset
  `utcnow()` produces, repo-wide — not something introduced here, and
  out of scope to change for one table when every other timestamp column
  in this codebase has the same shape.
- An adjacent, pre-existing issue was surfaced during review:
  `app/db.py::init_db` builds the Alembic config's `sqlalchemy.url` from
  `str(engine.url)`, and SQLAlchemy's `URL.__str__` masks the password
  (`***`) — so `alembic upgrade head` would fail to authenticate against
  a password-protected Postgres server. Not introduced by this issue, not
  caught by CI (the Postgres service container is passwordless), and out
  of scope to fix here; worth its own follow-up issue before a real
  password-protected Postgres deployment relies on `init_db`.
