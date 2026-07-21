# Feature 14 (slice 1/4): Multi-framework foundation — ISO 27001 + SOC 2

## Context

Feature 14 asks for full ISO 27001 + SOC 2 multi-framework support:
framework selection, unified controls, cross-framework mapping, framework-
specific assessment views, evidence reuse UI, migration/compat, and a large
e2e test matrix. That's several independent subsystems, so this spec covers
only the first, unblocking slice: **the ability for a tenant to have SOC 2
exist as data, enable/disable either framework, and scope SOC 2 to specific
Trust Services Categories (TSCs), with a correct migration/reconciliation
path for installations that already have data.** No UI labeling sweep, no
cross-framework requirement-mapping catalog, no framework-specific
readiness calculation, and no evidence-reuse UI are in this slice — those
remain separate specs that depend on this one.

**Revision history:**
- **Round 1** rewrote the original draft against actual code after review
  found: an unsafe seed-gating assumption, an unsafe migration backfill,
  two authorization bypasses, an unenforced "disabled means hidden" claim,
  an under-specified category model, and an under-specified SOC2 catalog.
- **Round 2** (this version) resolves five further blockers found in
  round 1's design: sequential-only reconciliation idempotency (real race
  under concurrent workers/pods), silent creation of a duplicate active
  ISO framework alongside an unidentified legacy one, an unsafe demo-seed
  heuristic (`InternalControl` absence ≠ "new install"), a `category_id`
  FK that didn't enforce same-framework ownership, and an incomplete
  canonical-vs-user-owned mutability boundary (framework create/edit
  weren't admin-gated; no defined immutable-field list). See "Review
  findings resolved — round 2" for the mapping from each finding to the
  section that resolves it.

Existing schema already generalizes further than expected: `Framework`,
`FrameworkRequirement`, `InternalControl`, and `ControlRequirementMapping`
have no framework-specific FK or single-framework assumption anywhere — a
control can already map to requirements across multiple frameworks with
zero schema change. That many-to-many model is preserved unchanged by this
slice.

## Scope of this slice

1. A **concurrency-safe system framework catalog reconciliation**
   mechanism (advisory-lock-based on PostgreSQL, bounded-retry
   write-transaction-based on SQLite), separate from demo-data seeding,
   idempotent under concurrent callers, safe to retry after a crash.
2. A **legacy-ISO resolution protocol**: exact fingerprint adoption,
   explicit conflict detection and a persistent, admin-surfaced conflict
   record for ambiguous cases, and a hard block on activating a new
   canonical framework while a conflict is unresolved — never silent
   guessing, relabeling, or merging.
3. A **config-gated, marker-tracked demo-data seed** (`GRC_SEED_DEMO_DATA`,
   off by default, following this repo's existing `GRC_`-prefixed
   pydantic-settings convention), fully decoupled from catalog
   reconciliation, with a fail-closed guard against seeding demo data into
   a real installation.
4. A **database-enforced, same-framework category ownership model**
   (composite foreign key) and a scope-toggle service that validates
   framework identity from the route, not from client-supplied form data.
5. A **complete canonical-vs-user-owned mutability boundary**: framework
   create/edit routes become admin-only, canonical system fields become
   immutable through every mutation path (UI, CSV import, generic CRUD,
   service calls), with defined override fields for admin customization
   and defined collision handling when a user-owned row collides with a
   canonical catalog key.
6. An explicit **catalog completeness status** per framework
   (`seed_incomplete` / `ready` / `conflict`), gating readiness/progress
   presentation for SOC2 until Slice 1b completes its catalog, without
   regressing ISO's already-shipped behavior.
7. Expanded **migration, concurrency, and regression test coverage**.

Out of scope for this slice (future specs, unchanged from round 1):
cross-framework requirement-to-requirement mapping catalog; framework
badges/filters across control register, control details, checklist,
assessments, evidence, CSV import/export, reports, dashboards, search;
framework-specific readiness calculation; evidence-reuse UI; full e2e
browser test matrix (parent feature item 10, picked up incrementally as
each slice lands).

## What the current code actually does (unchanged from round 1, still the baseline)

- `app/seed.py::seed_if_empty` returns immediately if **any** `Framework`
  row exists, and otherwise creates the ISO framework, 5 requirements, 4
  demo `InternalControl`s, their mappings, and 2 demo `Risk`s — all as one
  function gated on one check.
- `app/routers/frameworks.py::update_framework`/`create_framework` are
  gated only by `Depends(require_login)` — any logged-in user can flip
  `is_active` today, and new frameworks default to `is_active=True`.
- `app/routers/dashboard.py::dashboard` filters
  `Framework.is_active.is_(True)` for its summary; no other route filters
  on `is_active` at all.
- `app/progress.py::compute_progress` has no category/scope awareness.
- `app/models.py::Job` establishes this codebase's pattern for a
  concurrency-safe, cross-dialect guarded mutation; `app/db.py` sets
  SQLite `PRAGMA journal_mode = WAL` and `busy_timeout = 5000`;
  `tests/test_postgres_compat.py` establishes the dialect-gated-test
  pattern (`skipif not TEST_DATABASE_URL`, run for real in CI's
  `test-postgres` job); migrations use `op.batch_alter_table` uniformly
  for constraint changes on both dialects.
- `app/config.py::Settings` is a `pydantic_settings.BaseSettings` with
  `env_prefix="GRC_"` — every existing runtime flag
  (`session_cookie_secure`, `google_workspace_directory_enabled`, etc.)
  follows this convention; there is no other config mechanism in this
  repo, so new flags follow it too rather than inventing a parallel one.
- `tests/conftest.py::app` fixture calls
  `create_app(database_path=db_path)` with no other override — `app_env`/
  `seed_demo_data`-style flags are not currently parameterized per test.

## 1. Concurrency-safe system catalog reconciliation (resolves round-2 finding 1)

### Why round 1's algorithm was only sequentially idempotent

Round 1 specified "look up by unique key, create if absent." That's
idempotent against *sequential* re-runs, but under two reconcilers running
truly concurrently (multiple app workers/pods starting at once against the
same database — a realistic case for the Postgres/Kubernetes deployment
target this app supports, per `docs/architecture.md`), both can execute
the "absent" branch before either commits, and both attempt to `INSERT`
the same `(code)`/`(framework_id, code)`/`(framework_id, reference_code)`
row — one succeeds, one raises `IntegrityError`, which (without handling)
crashes that worker's startup and poisons its transaction.

### Mechanism, precisely

**Primary serialization — a transaction-scoped lock, acquired first, with
a bounded retry and a defined timeout on both dialects:**

- **PostgreSQL:** `pg_try_advisory_xact_lock(hashtext('minigrc:framework_catalog_reconciliation'))`
  — a single, fixed, stable lock key (this slice's reconciliation is one
  all-or-nothing pass over the whole system catalog; no finer-grained
  locking is needed since the work is small and infrequent). This is the
  *non-blocking* variant: it returns `true`/`false` immediately rather
  than waiting indefinitely. `reconcile_system_catalog` calls it in a
  bounded retry loop (e.g. up to 30 attempts, short sleep between), and
  fails startup with a clear diagnostic only if every attempt returns
  `false`. Being transaction-scoped (`_xact_`), the lock is automatically
  released when the transaction ends for **any** reason — commit,
  rollback, or the owning connection/process dying — so a crashed worker
  can never leave the lock held.
- **SQLite:** an explicit `BEGIN IMMEDIATE` on the reconciliation
  connection (acquires SQLite's reserved write lock immediately, rather
  than lazily on first write) inside the same bounded retry loop,
  catching `sqlite3.OperationalError` ("database is locked" — the
  already-configured `busy_timeout=5000` gives each attempt up to 5s
  before that error surfaces) and retrying up to the same bound before
  failing startup with a clear diagnostic ("Timed out acquiring the
  framework-catalog reconciliation lock — is another instance running
  against this database file?"). This is stated as an explicit protocol,
  not "WAL already serializes writers" — WAL's single-writer guarantee is
  real but doesn't by itself define retry bounds, timeout behavior, or the
  startup-failure diagnostic; those are specified here regardless of
  dialect. The lock is connection-scoped, so a crashed process releases it
  the same way Postgres's transaction-scoped lock does.

**Secondary safety net — conflict-safe inserts, so a race that somehow
still occurs cannot fail startup or poison the transaction:** every row
creation in reconciliation (`Framework`, `FrameworkCategory`,
`FrameworkRequirement`) uses a dialect-appropriate `INSERT ... ON CONFLICT
DO NOTHING` statement against the row's unique key
(`sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(...)`
on Postgres, `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_nothing(...)`
on SQLite — both dialects support this natively, dispatched by a small
`session.bind.dialect.name` check in one shared helper). `ON CONFLICT DO
NOTHING` never raises `IntegrityError` — it silently affects zero rows
when the unique key already exists, so **the transaction is never left in
a poisoned/unusable state and no worker fails startup from a uniqueness
race**, even without the lock. Immediately after every conflict-safe
insert attempt, reconciliation performs a fresh `SELECT` by the same
unique key to obtain the authoritative row — **this is the "loser re-reads
and validates the winning row" behavior**: whichever process's `INSERT`
did not take effect simply proceeds using the row the other process
created, with no special-cased branch needed. Whether a given call
actually created the row is known from the statement's `rowcount`
(`1` = this call created it; `0` = it already existed) — **audit events
for a "create" action are written only when `rowcount == 1`**, which is
exactly what guarantees no duplicate reconciliation audit/history records
across concurrent or repeated calls.

### Atomicity, and why "partial completion" cannot persist

The entire reconciliation pass runs inside one transaction, committed only
at the very end (same "one session per request/attempt, commit at the
boundary" shape this codebase already uses everywhere else). A crash or
injected failure mid-reconciliation rolls the whole transaction back —
nothing partially committed can exist. This means "safely resume a
partially completed reconciliation" resolves to something stronger and
simpler than resuming from a partial state: **retrying the entire,
idempotent function from scratch is always correct and cheap**, so that's
the retry policy, on both a startup failure and the explicit CLI command.

### Where reconciliation runs (unchanged from round 1's reasoning, restated)

- Automatically at startup, in `create_app()`, right after `init_db()`
  and before the (now config-gated, see §3) demo seed step.
- Also exposed as `python -m app.cli reconcile-framework-catalog`,
  calling the exact same function — same lock protocol, same conflict-safe
  inserts, no separate implementation.
- Never inside an Alembic migration — migrations stay schema-only in this
  repo (see every file in `migrations/versions/`); catalog *content* is
  application data that may need a future release without a matching
  schema change.

### Tests

- **Concurrent PostgreSQL reconciliation** (`skipif not
  TEST_DATABASE_URL`): two real threads/connections call
  `reconcile_system_catalog` against the same empty database at
  approximately the same time; assert exactly one row per `(code)`/
  `(framework_id, code)`/`(framework_id, reference_code)` exists
  afterward, exactly one "create" `AuditEvent` per row, and neither call
  raises `IntegrityError` to its caller.
- **Concurrent SQLite reconciliation**: two threads/connections against
  the same SQLite file; same assertions; additionally assert the second
  caller's `BEGIN IMMEDIATE` attempt is observed to retry at least once
  (via a countable hook/log, not just "it eventually succeeded") when the
  first caller intentionally holds its transaction open briefly.
- **Injected mid-run failure and retry**: monkeypatch the requirement-
  creation step to raise after the framework+categories are created but
  before requirements complete; assert the whole transaction rolled back
  (no framework/category rows exist afterward); call
  `reconcile_system_catalog` again without the injected failure; assert
  the full catalog now exists correctly and exactly once.
- **No duplicate audit records across repeats**: call
  `reconcile_system_catalog` three times sequentially against the same
  session/engine; assert the count of `system_reconcile_create` audit
  events is identical after the 2nd and 3rd calls to after the 1st.
- **CLI parity**: `python -m app.cli reconcile-framework-catalog` produces
  identical results to the startup call against the same database.

## 2. Legacy ISO resolution protocol (resolves round-2 finding 2)

### Why round 1's approach was wrong

Round 1's migration matched the pre-existing ISO framework by an exact
content fingerprint, and — when that match failed (zero or multiple
hits) — had reconciliation silently create a **new, freshly active**
canonical `iso27001-2022` framework, leaving the old, unidentified row
(which may still hold real controls, assessments, and mappings) sitting
alongside it. That's exactly the "two active ISO representations that
appear equivalent" outcome this round's review flagged: an admin (or an
automated readiness calculation in a later slice) would have no way to
tell which one is real without reading raw data.

### Decision table

| Scenario | Existing framework rows | Fingerprint result | Action | Conflict record? | New canonical framework's `is_active` |
|---|---|---|---|---|---|
| Fresh install | None at all | N/A | Create canonical ISO directly | No | `True` (normal default) |
| Exact unique match | ≥1 exists | Exactly one row matches the full fingerprint (`name`, `version`, `is_placeholder_content` all equal the exact seed text) | Adopt that row: stamp `code`/`family`/`is_system_provided` on it, in place | No | N/A — existing row's `is_active` is left exactly as it was |
| No candidates, other data exists | ≥1 exists, none plausibly ISO-like | Zero rows match the full fingerprint **and** zero rows match the loose heuristic below | Create canonical ISO fresh (nothing here could plausibly be a renamed legacy ISO row) | No | `True` |
| Potential single candidate | ≥1 exists | Zero exact matches, exactly one row matches the loose heuristic (`is_placeholder_content = True`, `code IS NULL`) — i.e. a framework that looks like it could be the seeded ISO catalogue but was renamed away from the exact fingerprint | Create canonical ISO **inactive**; do not touch the candidate row | Yes — `potential_legacy_match`, candidate id recorded | `False`, forced regardless of `default_active` |
| Multiple/ambiguous candidates | ≥1 exists | More than one row matches the full fingerprint, **or** more than one row matches the loose heuristic | Create canonical ISO **inactive**; do not touch any candidate row | Yes — `multiple_exact_match` or `multiple_candidate_match`, all candidate ids recorded | `False`, forced |

The loose heuristic (`is_placeholder_content = True AND code IS NULL`) is
intentionally broader than the exact fingerprint — a false positive here
(flagging something for review that turns out unrelated) is cheap and
recoverable (an admin acknowledges it); a false negative (silently
creating a duplicate active ISO framework next to a real renamed one)
is the exact failure this round's review is about. Rows already carrying
a `code` (i.e. previously identified in an earlier reconciliation run) are
excluded from candidate matching — they're already resolved.

### `CatalogConflict` (new table)

```python
CATALOG_CONFLICT_TYPES = (
    "potential_legacy_match",
    "multiple_candidate_match",
    "multiple_exact_match",
    "user_owned_key_collision",
    "user_owned_requirement_collision",
)

class CatalogConflict(Base):
    """A persistent, actionable record of a system-catalog reconciliation
    ambiguity — reconciliation never guesses, relabels, or merges; it
    records what it found here and waits for an admin. `candidate_ids_json`
    is populated directly from the SELECT that found the ambiguity, never
    invented — see app/framework_catalog.py for the query each conflict
    type is produced from.
    """

    __tablename__ = "catalog_conflicts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    catalog_key: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "iso27001-2022"
    conflict_type: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_ids_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list of Framework/FrameworkRequirement ids
    detail: Mapped[str] = mapped_column(Text, default="")
    detected_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolution_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
```

### What Slice 1 exposes, and what it explicitly defers

Automated **merging** of a legacy framework into the canonical one (or
vice versa) is genuinely out of scope for this slice — it would mean
re-pointing existing `ControlRequirementMapping`/`RequirementAssessment`/
`EvidenceRequirementMapping`/`RequirementNote` rows from the legacy
framework's requirements onto the canonical framework's requirements, a
real data-migration operation with its own edge cases (different
requirement sets, different assessment states) that deserves its own
design, not a rushed addition here.

**What Slice 1 does expose:** detection (above), persistent recording, and
one single admin-only resolution action — **acknowledge**. Acknowledging a
conflict does not touch the legacy row or the canonical row at all; it
only marks the conflict `resolved_at`/`resolved_by_user_id`/
`resolution_action="acknowledged_kept_separate"`, which is what unblocks
normal activation of the canonical framework through the existing
activate/deactivate route. The admin is then free to use existing tools
(the metadata edit route, the activate/deactivate route) to decide by hand
whether to keep both, deactivate the legacy one, or rename either — this
slice does not add any new merge tooling, and says so explicitly in the
admin UI copy.

**The block on canonical activation:** `set_framework_active` (§5) checks,
before allowing `active=True` on a framework whose `code` is non-null, for
any `CatalogConflict` row with that `catalog_key` and `resolved_at IS
NULL`; if found, raises `UnresolvedCatalogConflictError` (route → flash
error pointing at Admin > Settings). Deactivating is never blocked.

### Admin UI

Admin Settings surfaces any unresolved `CatalogConflict` as a prominent
banner: which catalog key is affected, the candidate framework's name(s)/
ids (linked to their detail pages), the conflict type in plain language
("A framework that looks like it might be your existing ISO 27001
catalogue was found, but it doesn't exactly match what this app expects —
review it and confirm whether to keep it separate from the new ISO 27001
system framework."), and a single "Acknowledge — keep separate" button.
Copy explicitly states this app does not merge frameworks automatically.

### Tests

One test per decision-table row (fresh install; exact unique match,
existing `is_active` preserved, no conflict; no-candidates-with-other-data,
canonical created active, no conflict; single potential candidate,
canonical created inactive, conflict recorded with the correct id;
multiple candidates, canonical inactive, conflict lists all ids), plus:
activation blocked while unresolved (`UnresolvedCatalogConflictError`);
activation succeeds after acknowledgement; non-admin cannot acknowledge
(`403`); acknowledging writes an audit event containing actor, before
(`resolved_at IS NULL`)/after (`resolved_at` set) state, and the recorded
candidate ids.

## 3. Config-gated, marker-tracked demo-data seeding (resolves round-2 finding 3)

### Why "no `InternalControl` exists" (or any single-table check) is unsafe

Any such check conflates "this table happens to be empty right now" with
"this is a fresh installation" — an install that deleted its only control,
or that simply hasn't created one yet for legitimate reasons, would
silently receive injected demo data on its next restart. The task is
explicit that this is unacceptable, and round 1's `InternalControl`-based
replacement for round 0's `Framework`-based check has the same underlying
flaw, just moved to a different table.

### Design: explicit config flag + persistent marker + fail-closed guard

Following this repo's existing, and only, configuration convention
(`app/config.py::Settings`, `pydantic_settings.BaseSettings`,
`env_prefix="GRC_"`) rather than inventing a parallel mechanism:

```python
# app/config.py additions
seed_demo_data: bool = False
seed_demo_data_force: bool = False
```

`GRC_SEED_DEMO_DATA` defaults `False` — a production deployment gets zero
demo controls/risks unless explicitly opted in. `GRC_SEED_DEMO_DATA_FORCE`
is the narrowly-scoped override described below, requiring a second,
separate env var to reduce the chance of an accidental production seed.

**Persistent marker** (new table, singleton row, same shape as the
existing `TrustCenterSettings` singleton):

```python
class DemoSeedState(Base):
    __tablename__ = "demo_seed_state"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
```

Both timestamps and the marker row itself are written in the **same
transaction** as the demo controls/risks/mappings, committed together at
the very end — exactly the same atomicity argument as §1's reconciliation.
This means a crash mid-seed can never leave a "started but not completed"
marker row visible, because the whole transaction (including the marker
insert) rolls back together — so "partial failure can be retried safely"
resolves to the same "retry from scratch is always correct" property as
reconciliation, not a two-phase resume protocol.

**Seed function** (`app/seed.py::seed_demo_data_if_needed(session,
settings)`, renamed from `seed_if_empty`):

1. If `not settings.seed_demo_data`: return immediately — no query, no
   effect. This is the production default path.
2. If a `DemoSeedState` row already exists with `dataset_version` matching
   the current code's expected version: return immediately (already
   seeded, idempotent).
3. **Fail-closed guard:** query for any `AuditEvent` with `actor != "system"`
   — every real user-driven mutation in this app already writes an
   `AuditEvent` with the acting user's email (confirmed throughout
   `app/routers/frameworks.py`, `app/audit.py`'s existing call sites), and
   the only `actor="system"` events come from seeding/reconciliation
   themselves — so this is a real, already-existing signal for "has any
   genuine user action ever happened here," not a new heuristic invented
   for this purpose. If any such event exists **and** `settings.
   seed_demo_data_force` is not also `True`: log a clear error ("
   GRC_SEED_DEMO_DATA is enabled but this installation already has real
   user activity — refusing to inject demo data. Unset
   GRC_SEED_DEMO_DATA, or set GRC_SEED_DEMO_DATA_FORCE=true if you really
   intend to seed demo data into this database.") and return without
   seeding — a misconfigured flag on a real production database must not
   crash startup, it must loudly no-op.
4. Otherwise, seed the demo controls/risks/mappings (looking up the ISO
   framework's known reference codes via `SELECT ... WHERE reference_code
   IN (...)`, since reconciliation is guaranteed to have already created
   them by this point in `create_app()`'s ordering — no local variable
   handoff needed) and the `DemoSeedState` marker, all in one transaction.

### Ordering (explicit)

```
1. init_db(engine)                          # Alembic migrations — schema only
2. reconcile_system_catalog(session)        # always runs, every startup, regardless of seed_demo_data
3. if settings.seed_demo_data:
       seed_demo_data_if_needed(session, settings)   # gated, marker-tracked, fail-closed
4. normal application startup                # FastAPI app object, router registration, etc. — unchanged
```

Catalog reconciliation is completely independent of demo seeding — it
always runs, regardless of `seed_demo_data`, and demo seeding never
creates or modifies catalog rows itself (it only reads the reference codes
reconciliation already guaranteed exist).

### Test-harness change required

`tests/conftest.py`'s `app` fixture currently calls
`create_app(database_path=db_path)` with no other override, and several
existing tests depend on demo controls/risks being present (this was true
implicitly under the old always-on `seed_if_empty`). `create_app` gains an
explicit `seed_demo_data: bool | None = None` parameter (same override
pattern already used for `database_path`/`data_dir` — an explicit param
always wins over env-derived settings, per `CLAUDE.md` constraint #10),
and `tests/conftest.py::app` is updated to pass `seed_demo_data=True` so
existing demo-data-dependent tests keep passing without relying on a real
environment variable per test process.

### Tests

Fresh install with `seed_demo_data=False` (default): zero
`InternalControl`/`Risk` rows, catalog fully reconciled regardless. Fresh
install with `seed_demo_data=True`: demo data present, marker row written.
Repeated startup with `seed_demo_data=True`: second call is a no-op (marker
already matches version), no duplicate demo rows. Existing-data
installation (real `AuditEvent` with a non-`system` actor present) with
`seed_demo_data=True` and no force flag: refuses to seed, logs the
diagnostic, no new rows. Same scenario with `seed_demo_data_force=True`:
seeds anyway. Partial-failure simulation (inject a failure mid-seed):
transaction rolls back entirely, no marker row, no partial demo rows;
retrying without the injected failure succeeds cleanly. Catalog
reconciliation with `seed_demo_data=False`: ISO/SOC2 catalog still fully
present — proving the two mechanisms are truly decoupled.

## 4. Database-enforced category ownership + closed scope-toggle bypass (resolves round-2 finding 4)

### Composite foreign key, not a plain single-column FK

`FrameworkCategory` gains a second unique constraint (in addition to
`(framework_id, code)`) on `(id, framework_id)` — trivially true given
`id` is already a primary key, but required so a composite foreign key
can target this exact column pair:

```python
class FrameworkCategory(Base):
    __tablename__ = "framework_categories"
    __table_args__ = (
        UniqueConstraint("framework_id", "code", name="uq_framework_category_code"),
        UniqueConstraint("id", "framework_id", name="uq_framework_category_id_framework"),
    )
    # ... fields as in round 1 (§5 there): display_name, in_scope, is_mandatory,
    # is_system_provided, display_order, updated_at, updated_by_user_id
    # ... plus category_scope_configurable is on Framework, not here — see below
```

`FrameworkRequirement` already has a `NOT NULL framework_id` (existing
`ForeignKey("frameworks.id")`). Rather than a plain `category_id =
ForeignKey("framework_categories.id")`, it declares a **composite** FK
using that same `framework_id` column plus the new nullable `category_id`:

```python
class FrameworkRequirement(Base):
    __table_args__ = (
        UniqueConstraint("framework_id", "reference_code", name="uq_requirement_framework_code"),  # existing
        ForeignKeyConstraint(
            ["category_id", "framework_id"],
            ["framework_categories.id", "framework_categories.framework_id"],
            name="fk_requirement_category_same_framework",
        ),
    )
    category_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_system_provided: Mapped[bool] = mapped_column(default=False)
```

This is enforced by the database on both PostgreSQL and SQLite (composite
foreign keys are standard SQL, supported by both) — assigning a
`category_id` that belongs to a *different* `framework_id` than the
requirement's own is rejected at the database layer, not just by
application code, even from a raw SQL statement or a future bug. Standard
multi-column FK semantics (`MATCH SIMPLE`, the default on both dialects)
mean the constraint is trivially satisfied whenever `category_id IS
NULL` — "no category assigned yet" remains fully valid.

`app/requirements.py::add_requirement` gains `category_id: str | None =
None` and `is_system_provided: bool = False` keyword arguments.

### Migration mechanics

1. `op.create_table("framework_categories", ...)` with both unique
   constraints from the start (no existing rows to migrate — this is a
   brand-new table).
2. `op.add_column("framework_requirements", sa.Column("category_id",
   sa.String(32), nullable=True))` and `is_system_provided` (`Boolean`,
   `server_default=false`) as plain column adds.
3. `with op.batch_alter_table("framework_requirements", schema=None) as
   batch_op: batch_op.create_foreign_key(...)` for the composite FK —
   SQLite cannot `ALTER TABLE ADD CONSTRAINT` directly (same reason
   `8961da81a764_add_user_status_and_google_subject.py` uses
   `batch_alter_table` for its check/unique constraints); this is a
   harmless no-op-wrapper on Postgres, per the existing convention
   documented in `docs/architecture.md`.

### `Framework.category_scope_configurable` (new column)

Whether a framework's categories can be toggled in/out of scope at all is
catalog data, not a hardcoded per-family check in the service (which would
reintroduce the "framework-specific assumption" this feature is trying to
eliminate elsewhere) — it's a plain boolean column, set by reconciliation
from each `FrameworkDef` (`True` for SOC2, `False` for ISO), re-synced on
every reconciliation run exactly like `is_mandatory` (§5, round 1).

### Closing the scope-toggle bypass

The route now carries **both** ids, and the service cross-checks them
instead of trusting either alone:

```python
def set_category_in_scope(
    session: Session, *, framework_id: str, category_id: str, in_scope: bool, actor: str
) -> FrameworkCategory:
    category = session.get(FrameworkCategory, category_id)
    if category is None or category.framework_id != framework_id:
        raise CategoryNotFoundError(framework_id=framework_id, category_id=category_id)
    if not category.framework.category_scope_configurable:
        raise CategoryScopeNotConfigurableError(...)   # e.g. any ISO category
    if category.is_mandatory:
        raise MandatoryCategoryError(...)               # e.g. SOC2 Security
    before = category.in_scope
    category.in_scope = in_scope
    record_audit_event(
        session, entity_type="framework_category", entity_id=category.id,
        action="update_scope", actor=actor,
        detail=f"framework_id={framework_id} category_code={category.code} before={before} after={in_scope}",
    )
    return category
```

Route: `POST /admin/settings/frameworks/{framework_id}/categories/{category_id}/toggle`
— `require_admin` + `verify_csrf`. `framework_id` and `category_id` both
come from the URL path (never from a form field an attacker could
mismatch against the path), and the service's `category.framework_id !=
framework_id` check means a request naming the *wrong* `framework_id` in
its path for a real `category_id` from a different framework is rejected
regardless — the route never "trusts" that the two path segments agree
just because both individually resolve to real rows.

Every validation check (`CategoryNotFoundError`,
`CategoryScopeNotConfigurableError`, `MandatoryCategoryError`) is raised
**before** any mutation or `record_audit_event` call — so a rejected
request produces zero state change and zero audit event by construction,
not merely by the caller checking a return value; `get_db`'s existing
rollback-on-exception behavior is a second layer under that, not the
primary guarantee.

**Category toggling is not gated by `Framework.is_active`** — configuring
which TSCs are in scope is plain configuration, not "new compliance work,"
consistent with the metadata-edit route also staying open on a disabled
framework (§4/round 1's disable-semantics table).

### Tests

- **DB-level**: attempting to `INSERT`/assign a `category_id` belonging to
  a different framework directly via the ORM raises an `IntegrityError`
  from the composite FK, on both SQLite and Postgres (the Postgres case
  via the existing `test_postgres_compat.py`-style gated test).
- **Cross-framework toggle request**: `POST
  /admin/settings/frameworks/{iso_id}/categories/{soc2_security_category_id}/toggle`
  → `CategoryNotFoundError` (mismatch), no mutation, no audit event.
- **ISO category toggle request**: any ISO category →
  `CategoryScopeNotConfigurableError`, no mutation.
- **Nonexistent category id**: `CategoryNotFoundError`, `404`.
- **Mandatory SOC2 Security removal**: `MandatoryCategoryError`, no
  mutation.
- **Valid optional SOC2 category toggle** (e.g. Availability): succeeds,
  `in_scope` flips, one audit event with actor/before/after/category code.
- **Toggle on a disabled framework**: succeeds (not gated by
  `is_active`), confirming the deliberate distinction from the four
  assessment-relevant routes that *are* gated.

## 5. Canonical-vs-user-owned mutability boundary (resolves round-2 finding 5)

### Framework create/edit routes become admin-only

`app/routers/frameworks.py::new_framework_form`, `create_framework`,
`edit_framework_form`, and `update_framework` all gain
`Depends(require_admin)` (replacing/augmenting the router-level
`require_login`). This directly resolves the "remaining design decision"
flagged open at the end of round 1 — the review settles it: framework
administration (creation and metadata editing), not just activation, is
admin-only.

### Immutable canonical fields

For any `Framework`/`FrameworkCategory`/`FrameworkRequirement` row with
`is_system_provided=True`, the following are immutable through **every**
mutation path — the generic metadata-edit UI, CSV import, the
register-grid generic CRUD endpoints, and any direct service call:

| Entity | Immutable (canonical) fields |
|---|---|
| `Framework` | `code`, `family`, `version`, `is_placeholder_content`, `is_system_provided`, `category_scope_configurable` |
| `FrameworkCategory` | `code`, `display_name`, `is_mandatory`, `is_system_provided` |
| `FrameworkRequirement` | `reference_code`, `title`, `summary`, `category_id`, `is_system_provided` |

`Framework.name`/`Framework.description` are **also** treated as canonical
display text for a system-provided framework (they're literally the
catalog's own name/description) rather than freely editable — see
override fields below. `Framework.is_active` is never edited through this
route at all (round 1, §3) — it's exclusively the domain service's job.

**How this is enforced concretely:**

- `update_framework`: when the target `Framework.is_system_provided` is
  `True`, the route only accepts and applies
  `display_name_override`/`org_notes` (new override fields, below); any
  attempt to change `name`/`version`/`description`/`is_placeholder_content`
  away from their current canonical values is rejected with a `400` and no
  mutation (defense in depth beyond simply not rendering those fields as
  editable inputs in the template). When `is_system_provided` is `False`
  (a genuinely custom framework), the route behaves exactly as it does
  today — full `name`/`version`/`description`/`is_placeholder_content`
  editability, no canonical-field concept applies.
- `FrameworkCategory`: no edit or delete route exists anywhere in this
  slice (categories are only created by reconciliation and only their
  `in_scope` flag is ever mutated, via §4's service) — its canonical
  fields are immutable simply because nothing exposes them for editing at
  all, not because of an added guard. Stated here explicitly so this isn't
  an accidental omission.
- `FrameworkRequirement`: this app has no requirement-edit route today
  (only `create_requirement` and CSV import exist), so "immutable through
  generic edit" is already true by omission. CSV import
  (`app/csv_import.py` → `app/requirements.py::add_requirement`) already
  cannot silently overwrite a system-provided requirement's canonical
  fields, because `add_requirement` always attempts a fresh `INSERT` and
  the existing `UniqueConstraint("framework_id", "reference_code")`
  rejects a duplicate `reference_code` outright (confirmed by the existing
  `test_duplicate_reference_code_within_csv_rejected`/
  `test_duplicate_reference_code_rejected` tests) — a CSV row that
  collides with an existing reference code fails the whole import rather
  than overwriting anything. This is already-correct behavior, called out
  here so the requirement is verifiably satisfied rather than assumed.
- Register-grid generic CRUD (`app/registers/router.py`, used by
  `REQUIREMENTS_REGISTER_CONFIG` in `app/routers/frameworks.py`) is already
  configured `creatable=False, deletable=False` for framework requirements
  (existing config, unchanged) — its editable fields are `reference_code`,
  `title`, `summary`, `display_order` (also unchanged) which, for a
  system-provided row, are exactly the canonical fields this section says
  must be immutable. **This is a gap the generic register config does not
  currently close** — closing it (making those fields read-only in the
  register grid specifically for `is_system_provided=True` rows) is listed
  as a required implementation change in the matrix below, not yet
  reflected in today's `REQUIREMENTS_REGISTER_CONFIG`.

### User-owned override fields

```python
# Framework additions
display_name_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
org_notes: Mapped[str] = mapped_column(Text, default="")
```

The effective display name anywhere this app renders a framework's name is
`framework.display_name_override or framework.name` — a small helper
(`Framework.effective_name` property) rather than repeating the fallback
in every template. `org_notes` is a free-text field for an organization's
own commentary, entirely separate from the catalog's own `description`.
Both fields are freely editable on a system-provided framework through the
same `update_framework` route (this is exactly the "customization without
corrupting canonical identity" the review asks for) and have no meaning/
no UI on a custom framework (where `name`/`description` are already fully
editable, so an override would be redundant).

### Ownership-collision behavior

Two distinct collision shapes, both handled by reconciliation recording a
`CatalogConflict` (§2's table, reused — `conflict_type` values
`user_owned_key_collision`/`user_owned_requirement_collision`) rather than
ever adopting, overwriting, or relabeling the user-owned row:

- **`Framework.code` collision**: not reachable through any current UI
  (no form field ever sets `code`/`family` — only reconciliation/migration
  do), but reconciliation defends against it anyway: if its `SELECT
  Framework WHERE code = :catalog_code` finds a row with
  `is_system_provided=False`, it records the conflict, does **not** touch
  that row, and does **not** attempt to create a second row with the same
  `code` (blocked by uniqueness regardless) — that catalog framework is
  simply skipped for this reconciliation pass, and the framework's
  `catalog_status` (§6) stays `conflict` until an admin resolves it.
- **`FrameworkRequirement.reference_code` collision**: reachable in
  practice — the existing `create_requirement` route lets any admin (now
  admin-gated per this section) add a custom requirement to *any*
  framework, including a system-provided one, with a `reference_code` the
  catalog doesn't yet define (e.g. `CC1.2`, ahead of Slice 1b introducing
  it as a real catalog entry). When reconciliation's `SELECT
  FrameworkRequirement WHERE (framework_id, reference_code) = ...` finds a
  row with `is_system_provided=False`, it records
  `user_owned_requirement_collision` (with the existing row's id and the
  colliding `reference_code` in `detail`), does not convert that row to
  system-provided or touch its content, and does not create a duplicate
  (blocked by the existing unique constraint) — that one catalog entry is
  skipped for this pass, and `catalog_status` for that framework becomes
  `conflict` until resolved.

Reconciliation never converts a user-owned row into a system-provided one
or vice versa, in either direction, under any circumstance — the only way
`is_system_provided` is ever set is at row-creation time.

### Mutation/immutability matrix

| Path | System-provided `Framework` | Custom `Framework` | System-provided `FrameworkCategory` | System-provided `FrameworkRequirement` |
|---|---|---|---|---|
| `POST /frameworks` (create) | N/A (creation always makes a custom framework; a system framework only ever comes from reconciliation) | Admin-only (this section); always created `is_active=False` (round 1, §3) | — | — |
| `POST /frameworks/{id}/edit` (metadata) | Admin-only; only `display_name_override`/`org_notes` accepted; canonical fields rejected with `400` if changed | Admin-only; fully editable, unchanged from today | — | — |
| Activate/deactivate | Admin-only, via `set_framework_active` only (round 1 §3 + round 2 §2's conflict check) | Same | — | — |
| Category scope toggle | — | — | Admin-only, via `set_category_in_scope` only (§4); blocked if `is_mandatory` or `not category_scope_configurable` | — |
| `POST /frameworks/{id}/requirements` (create requirement) | Admin-only (this section); can add a *custom* (`is_system_provided=False`) requirement to a system framework — allowed, tracked for collision (above) | Admin-only; unchanged | — | Existing rows never touched by this route (it only creates new ones) |
| CSV import | Admin-only; a colliding `reference_code` fails the whole import (existing behavior, unchanged) | Admin-only; unchanged | — | Cannot overwrite — duplicate `reference_code` rejected |
| Register-grid generic CRUD | — | — | — | **Must be changed**: `reference_code`/`title`/`summary`/`display_order`/`category_id`/`is_system_provided` become read-only in `REQUIREMENTS_REGISTER_CONFIG` specifically when `row.is_system_provided is True` — not yet true today, listed as a required change for the implementation plan |
| Reconciliation (system process) | Creates or additively fills `category_id` only; never edits existing canonical content | Never touches | Creates, or re-syncs `is_mandatory`/`category_scope_configurable` only | Creates, or additively fills `category_id` only |
| Direct service call (`set_framework_active`/`set_category_in_scope`) | Only path to `is_active`/`in_scope` | Same | Same | N/A |

## 6. Catalog completeness status (resolves finding 6, refines round-1 §6)

### Why a blanket "partial forever" rule would be wrong

ISO's 5-requirement seed has always been documented as "representative,
not a full catalogue" (`docs/domain/domain-model.md`), and
`compute_progress`/the dashboard have used it for real, shipped readiness
percentages since the MVP. A rule that gates *any* "partial" catalog out
of readiness presentation would retroactively regress already-correct ISO
behavior nobody asked to change. The actual product need is narrower:
**don't let SOC2's brand-new, deliberately-incomplete 13-row skeleton be
presented as if it were real, complete readiness** — that's a SOC2-launch
concern, not a generic "partial catalogs can't compute progress" rule.

### `Framework.catalog_status` (new column)

```python
CATALOG_STATUSES = ("seed_incomplete", "ready", "conflict")
catalog_status: Mapped[str] = mapped_column(String(16), default="seed_incomplete")
```

Set by reconciliation: ISO is seeded `"ready"` (grandfathering its
already-shipped, already-accepted "5 representative codes" status — no
regression); SOC2 is seeded `"seed_incomplete"` (its 13-row skeleton is
explicitly not yet presentation-ready). `"conflict"` overrides either
value whenever an unresolved `CatalogConflict` exists for that framework's
`code` (§2, §5) — reconciliation recomputes this on every run rather than
leaving a stale value after a conflict is created or resolved.

This status measures **identifier-skeleton completeness against the
official catalog structure** (does the expected set of reference
codes/categories exist), not "full licensed descriptive text present" —
the latter is permanently out of scope for both frameworks under the
copyright boundary (`docs/domain/domain-model.md`), exactly as it already
is for ISO.

### What's gated while `catalog_status == "seed_incomplete"`

- **Dashboard** (`app/routers/dashboard.py`): the `incomplete_requirements`
  aggregate sum excludes any framework with `catalog_status ==
  "seed_incomplete"`; that framework still appears in `progress_list` (so
  an admin can see it exists and is active) but the template renders the
  literal text "SOC 2 catalog setup incomplete — readiness percentage
  unavailable" instead of `compute_progress`'s percent for it.
- **Framework detail page** (`GET /frameworks/{id}`): same banner instead
  of the computed percentage.
- **Framework list page**: same short status text next to the framework's
  name (this specific completeness wording is explicitly required by this
  section — unlike the general framework-badge sweep, which stays
  deferred to a later slice).

### What's *not* gated

Enabling SOC2 at the framework level, toggling its category scope,
performing real assessment work (`update_assessment`, note-adding) against
its 13 existing requirements, and adding custom SOC2 requirements via CSV
import or the create-requirement route all continue to work normally
while `catalog_status == "seed_incomplete"` — an organization can start
real work today; only the *readiness percentage presentation* and any
future reporting/mapping surface are gated. This directly satisfies
"enabling SOC 2 at the framework level must not imply full SOC 2
coverage" without blocking legitimate early use.

### Slice 1b and beyond

Slice 1b (mandatory before the cross-framework mapping/readiness slices,
per round 1's §6 decision, unchanged) is responsible for expanding the
requirement skeleton to the full, verified TSC identifier set and then —
only after validating every expected canonical entry is present — flipping
SOC2's `catalog_status` to `"ready"`. The (already-deferred) mapping and
readiness slices must both check `catalog_status == "ready"` before
computing or exposing anything for a given framework — noted here as a
forward constraint on those slices, not built now.

SOC 2 remains described everywhere (seed description, admin UI copy,
`org_notes` guidance) as an audit/reporting framework, never a
certification — unchanged from round 1.

## 7. Migration, concurrency, and regression test coverage (updated)

In addition to every test listed in round 1's §7 (fresh install; existing
ISO-only install; multi-custom-framework install; PostgreSQL migration
column/table assertions; audit before/after content) and the new tests
listed under each numbered section above, the full matrix for this round:

- Concurrent reconciliation on PostgreSQL and on SQLite (§1).
- Retry after an injected partial-reconciliation failure (§1).
- No duplicate audit/history records under concurrency or repeated calls
  (§1).
- Fresh database vs. existing-data legacy decisions — all five rows of
  §2's decision table.
- Exact legacy ISO match (adopted silently, existing `is_active`
  preserved).
- Potential/ambiguous legacy ISO conflicts (canonical created inactive,
  conflict recorded, activation blocked, admin acknowledgement unblocks
  it).
- Explicit demo-seed enablement, the persistent `DemoSeedState` marker,
  and the fail-closed guard with and without the force override (§3).
- Production-shaped startup (`seed_demo_data=False`) produces zero demo
  rows while catalog reconciliation still fully runs (§3).
- Same-framework category database integrity via the composite FK, on
  both dialects (§4).
- Direct route/service bypass attempts: non-admin on every admin-gated
  route (framework create/edit/activate/deactivate, category toggle,
  conflict acknowledgement); crafted POSTs targeting canonical fields on a
  system-provided framework; a mismatched `framework_id`/`category_id`
  pair in a toggle request.
- Canonical-field mutation attempts through the metadata-edit UI, CSV
  import, the register-grid generic CRUD endpoint, and direct service
  calls — one test per path in the mutation/immutability matrix (§5).
- User-owned/canonical identifier collisions — both `Framework.code` and
  `FrameworkRequirement.reference_code` shapes (§5), and reconciliation
  after such a collision is later resolved (conflict acknowledged, next
  reconciliation run proceeds normally for everything else, still skips
  the collided entry until the underlying row is manually renamed/removed
  by an admin).
- Partial SOC2 catalog gating: dashboard/detail/list wording and aggregate
  exclusion while `catalog_status == "seed_incomplete"` (§6).
- Preservation of all existing user-owned compliance data across every
  scenario above — a snapshot-before/snapshot-after comparison of
  `RequirementAssessment`/`RequirementNote`/`ControlRequirementMapping`/
  `EvidenceRequirementMapping`/custom `Framework`/`InternalControl`/`Risk`
  rows, asserting an exact match unless the scenario explicitly says
  otherwise.

Test file layout: `tests/test_framework_catalog.py` (reconciliation,
concurrency, legacy resolution, collision handling), `tests/
test_framework_admin.py` (service rules, authorization, concurrency of the
activation invariant), `tests/test_demo_seed.py` (new — config gating,
marker, fail-closed guard), `tests/test_admin_settings.py` (route-level:
rendering, CSRF, conflict banner, flash messages), extensions to `tests/
test_requirements.py` (`category_id`/`is_system_provided` plumbing,
composite-FK integrity), `tests/test_pages.py` (`/admin/settings` render
sweep), and extensions to `tests/test_postgres_compat.py` (new
tables/columns, both concurrency tests).

## Internal consistency check (reviewed against the full document, not just new sections)

- Every table/column/route name introduced in round 1 and retained here is
  used identically throughout: `frameworks.code`/`family`/
  `is_system_provided`/`category_scope_configurable`/`catalog_status`/
  `display_name_override`/`org_notes`; `framework_categories` (`code`,
  `display_name`, `in_scope`, `is_mandatory`, `is_system_provided`,
  `display_order`); `framework_requirements.category_id`/
  `is_system_provided`; `catalog_conflicts`; `demo_seed_state`; routes
  `POST /admin/settings/frameworks/{id}/toggle` and `POST
  /admin/settings/frameworks/{framework_id}/categories/{category_id}/toggle`.
- Fresh-install defaults (canonical ISO active, SOC2 inactive, no
  conflict) do not contradict legacy-conflict behavior (canonical ISO
  forced inactive only when a real candidate exists) — the decision table
  in §2 is the single source of truth for both, with "fresh install" as
  its own explicit row rather than an implicit default.
- SQLite and PostgreSQL behavior are specified concretely and separately
  everywhere a dialect difference matters (reconciliation locking,
  `with_for_update()`, conflict-safe inserts, `batch_alter_table`) — no
  section relies on "the other dialect behaves the same" without saying
  why (WAL single-writer vs. advisory lock; `FOR UPDATE` no-op vs. real
  row lock).
- Audit events are only promised where the algorithm that produces them is
  fully specified in the same section: reconciliation creates (§1, gated
  on `rowcount == 1`), conflict acknowledgement (§2), activation/
  deactivation (round 1 §3), category scope toggle (§4) — no other audit
  claims appear.
- Terminology is used consistently: **system-provided** = created or
  identified by reconciliation/migration (`is_system_provided=True`);
  **canonical** = the specific fields on a system-provided row that are
  immutable through normal mutation paths (§5's table); **user-owned** =
  any row/field not covered by the above, including custom frameworks,
  custom requirements on a system framework, and the override fields;
  **override** = `display_name_override`/`org_notes`, admin-editable
  fields that never replace canonical identity; **demo data** = the
  `InternalControl`/`Risk`/mapping rows from `seed_demo_data_if_needed`,
  entirely separate from catalog data and gated by `GRC_SEED_DEMO_DATA`,
  not by any business-table emptiness check.
- `ControlRequirementMapping` (control ↔ requirement, many-to-many, no
  framework FK) remains completely unmodified — no section here changes
  it or assumes anything new about it.
- No section assumes exactly one framework per control (the existing
  many-to-many mapping disproves that) or exactly one framework per
  installation (§2's multi-candidate and §5's collision scenarios both
  specifically exercise more-than-one-framework installations).
- Later slices (cross-framework mapping, framework-specific readiness,
  evidence-reuse UI, the badge sweep) remain deferred throughout, restated
  in "Explicitly deferred" below with no scope creep introduced by this
  round's changes.
- Slice 1b remains explicitly mandatory before the mapping/readiness
  slices (§6), now additionally gated by a machine-checkable
  `catalog_status` field rather than only a prose commitment.

## Explicitly deferred

- Cross-framework requirement↔requirement mapping catalog (confidence,
  system-vs-user-provided, notes) — next spec.
- Framework badges/filters in control register, control details,
  checklist, assessments, evidence, CSV import/export, reports,
  dashboards, search (the general labeling sweep, distinct from §6's
  specific "catalog setup incomplete" wording, which is in this slice) —
  next spec.
- Framework-specific readiness/assessment views and computation
  (`compute_progress` scope-awareness) — next spec, depends on the
  mapping catalog and on `catalog_status == "ready"` (§6).
- Evidence reuse UI clarity — next spec.
- **SOC2 catalog completion (Slice 1b)** — mandatory before the
  cross-framework mapping catalog slice; the only thing that can flip
  SOC2's `catalog_status` to `"ready"`.
- Automated legacy-framework merge tooling (§2) — Slice 1 only detects,
  records, and lets an admin acknowledge; merging requirement/assessment/
  mapping data between a legacy and canonical framework is a distinct,
  larger design left for a future slice if ever needed.
- Re-syncing already-created catalog rows' `title`/`summary` text on a
  future catalog content correction — reconciliation is strictly
  create-or-fill-null, never update-existing-content.
- Full e2e browser scenario matrix (parent feature item 10) — spread
  across the specs above as each becomes testable end-to-end.

## Review findings resolved — round 1

| # | Finding | Resolved by |
|---|---|---|
| 1 | Seed gate (`seed_if_empty`) would never deliver SOC2 to existing ISO installs | Split reconciliation from demo seeding (superseded/refined further by round 2 §1 and §3). |
| 2 | Blind `UPDATE frameworks SET code = 'iso27001' WHERE code IS NULL` could mislabel custom frameworks | Fingerprint match requiring a unique hit (superseded/refined further by round 2 §2's full decision table). |
| 3 | `update_framework`/`create_framework` could mutate/create `is_active` outside admin gating | `app/framework_admin.py` sole path, `with_for_update()` concurrency guard (extended by round 2 §5 with full admin-gating of create/edit themselves). |
| 4 | `is_active` was claimed to already mean "hidden from active workflows" without enforcement | Exact per-route table of gated vs. ungated behavior — unchanged and still in force this round. |
| 5 | `category`/`FrameworkScope` were unrelated free-form strings with an ambiguous `is_default` field | Normalized `FrameworkCategory` table (extended by round 2 §4 with the composite FK for same-framework enforcement). |
| 6 | 6 representative SOC2 criteria too few, risk of fabricating licensed content | Option B, 13-row skeleton, mandatory Slice 1b (extended by round 2 §6 with a machine-checkable `catalog_status`). |
| 7 | Test/migration coverage didn't address multi-framework installs or Postgres concurrency | Full scenario list (extended by round 2 §7). |

## Review findings resolved — round 2

| # | Finding | Resolved by |
|---|---|---|
| 1 | Reconciliation's check-then-insert was only sequentially idempotent — concurrent workers/pods could race on unique constraints and crash startup | §1: `pg_try_advisory_xact_lock` (Postgres) / `BEGIN IMMEDIATE` with bounded retry (SQLite) as primary serialization, dialect-portable `ON CONFLICT DO NOTHING` inserts as a non-poisoning safety net, `rowcount`-gated audit writes preventing duplicates, whole-pass transaction atomicity making partial-failure retry trivially safe. |
| 2 | Reconciliation could create a fresh active canonical ISO framework while an unidentified legacy ISO-like framework (owning real data) remained active | §2: five-row decision table (fresh/exact/no-candidate/potential/multiple), a persistent `CatalogConflict` record populated from the actual candidate-matching query, canonical framework forced inactive whenever any candidate exists, activation blocked (`UnresolvedCatalogConflictError`) until an admin-only, audited acknowledgement — with automated merging explicitly and honestly deferred. |
| 3 | `InternalControl`-absence (or any single business-table check) is an unsafe "is this a fresh install" proxy | §3: explicit `GRC_SEED_DEMO_DATA`/`GRC_SEED_DEMO_DATA_FORCE` config flags (following the repo's existing `Settings`/`GRC_` convention), a persistent `DemoSeedState` marker with dataset version, a fail-closed guard keyed on real (`actor != "system"`) audit history, and full decoupling from catalog reconciliation ordering. |
| 4 | A single-column `category_id` FK didn't prevent a requirement from pointing at another framework's category; the scope-toggle route trusted client-supplied framework identity | §4: composite foreign key `(category_id, framework_id) → (framework_categories.id, framework_categories.framework_id)`, database-enforced on both dialects; `set_category_in_scope` takes `framework_id` from the route path and cross-checks it against the category's actual `framework_id`, rejecting any mismatch before touching state. |
| 5 | Framework create/edit weren't admin-gated; no defined immutable-field list; no defined override mechanism; no defined collision handling | §5: `require_admin` added to all four framework CRUD routes; explicit immutable-canonical-fields table per entity type; `display_name_override`/`org_notes` as the sanctioned customization path; `CatalogConflict`-based collision handling for both `Framework.code` and `FrameworkRequirement.reference_code`; a full mutation/immutability matrix covering every existing mutation path including the previously-unaddressed register-grid generic CRUD gap. |

## Remaining design decisions requiring user input

- **§6, SOC2 catalog completeness** (unchanged from round 1): Slice 1b
  should be scoped against a licensed TSC copy if the org has one, rather
  than a secondary-source reconstruction — worth confirming before that
  slice is written.
- **§2, legacy-framework merge tooling**: this round deliberately scopes
  Slice 1 down to detect-record-acknowledge only, with actual merging of a
  legacy framework's data into (or instead of) the canonical one left
  undesigned. If an org is actually likely to hit this case in practice
  (e.g. because this app has already been in real use with a renamed ISO
  framework before this feature ships), it may be worth prioritizing a
  merge-tooling slice sooner rather than leaving it fully open-ended —
  worth a product call, not something this design should guess at.
- **§5, register-grid canonical-field lockdown**: closing the generic
  register-grid gap (making `reference_code`/`title`/`summary`/
  `display_order` read-only for `is_system_provided=True` rows) needs a
  small extension to `app/registers/config.py`'s `FieldSpec`/`RegisterConfig`
  (a per-row, not just per-field, read-only predicate) that doesn't
  exist today — confirmed as in-scope by this design, but worth flagging
  that it's a small generic-infrastructure change, not purely a
  frameworks-specific one, in case that affects sequencing with other
  register-grid consumers.
