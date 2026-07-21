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

**Revision note (this version):** a design review of the first draft found
seven blocking issues — an unsafe seed-gating assumption, an unsafe
migration backfill, two authorization bypasses, an unenforced "disabled
means hidden" claim, an under-specified category model, and an
under-specified SOC2 catalog. Every section below has been rewritten
against the actual current code (`app/routers/frameworks.py`,
`app/seed.py`, `app/deps.py`, `app/routers/dashboard.py`,
`migrations/versions/`) rather than assumptions about it. See "Review
findings resolved" at the end for the mapping from each finding to the
section that resolves it.

Existing schema already generalizes further than expected: `Framework`,
`FrameworkRequirement`, `InternalControl`, and `ControlRequirementMapping`
have no framework-specific FK or single-framework assumption anywhere — a
control can already map to requirements across multiple frameworks with
zero schema change. That many-to-many model is preserved unchanged by this
slice. `Framework.is_active` exists today, but — contrary to the first
draft's claim — nothing in the codebase actually enforces "hidden from
active workflows" except `app/routers/dashboard.py`'s framework summary
query. This slice defines and enforces that contract for real (see
"Framework and category disable semantics").

## Scope of this slice

1. A **system framework catalog reconciliation** mechanism, separate from
   demo-data seeding, that runs on every startup (idempotent) and can add
   ISO/SOC2 catalog rows to installations that already have data.
2. A **safe migration** that identifies the pre-existing seeded ISO
   framework by content fingerprint, not by blindly stamping every
   framework row — and defines what happens to rows it can't identify.
3. A **domain service** (`app/framework_admin.py`) that is the only path
   to changing `Framework.is_active` or `FrameworkCategory.in_scope`,
   admin-gated, invariant-enforcing, and concurrency-safe on both SQLite
   and PostgreSQL.
4. A precise, tested **contract for what "disabled" means**: which queries
   exclude a disabled framework, which routes refuse new mutations against
   one, and what stays fully intact and reversible.
5. A **normalized category model** (`FrameworkCategory`), replacing the
   free-string `category` idea from the first draft, shared by ISO Annex A
   themes and SOC2 TSCs, with a real mandatory/optional distinction.
6. An **expanded SOC2 catalog skeleton** — still explicitly placeholder,
   still not a full transcription — plus an explicit decision to treat full
   catalog completion as a mandatory follow-up slice (1b) before any
   cross-framework mapping work begins.
7. Expanded **migration and regression test coverage**, including
   PostgreSQL-specific concurrency tests.

Out of scope for this slice (future specs, unchanged from the first
draft): cross-framework requirement-to-requirement mapping catalog;
framework badges/filters across control register, control details,
checklist, assessments, evidence, CSV import/export, reports, dashboards,
search; framework-specific readiness calculation; evidence-reuse UI; full
e2e browser test matrix (parent feature item 10, picked up incrementally
as each slice lands).

## What the current code actually does (read before designing against it)

This section exists because the first draft got several of these wrong.

- `app/seed.py::seed_if_empty` returns immediately if **any** `Framework`
  row exists. It creates the ISO framework, its 5 requirements, 4 demo
  `InternalControl`s, their mappings, and 2 demo `Risk`s, all gated on that
  one check.
- `app/routers/frameworks.py::update_framework` (`POST
  /frameworks/{id}/edit`) is gated only by the router-level
  `Depends(require_login)` — **any logged-in user**, not just admins, can
  flip `is_active` today via this route's form field.
- `app/routers/frameworks.py::create_framework` (`POST /frameworks`) is
  also only login-gated, and its `Framework(...)` construction doesn't
  pass `is_active` explicitly — it gets the model's default (`True`). So
  any logged-in user can create a new, immediately-active framework today.
- `app/routers/dashboard.py::dashboard` **does** filter
  `Framework.is_active.is_(True)` for its program-summary widget — this
  one query already respects the "disabled" contract.
- `app/routers/frameworks.py::list_frameworks` (`GET /frameworks`),
  `view_framework`, `view_requirement`, `update_assessment`, `add_note`,
  `create_requirement`, and `import_requirements` **do not** check
  `is_active` anywhere. A disabled framework today is fully visible and
  fully mutable through every route except the dashboard summary.
- `app/progress.py::compute_progress` iterates `framework.requirements`
  with no category or scope filter — it has no notion of TSC scope today.
- `app/models.py::Job` already establishes this codebase's pattern for a
  concurrency-safe, cross-dialect guarded mutation (a conditional `UPDATE`
  rather than `SELECT ... FOR UPDATE SKIP LOCKED`), and
  `docs/architecture.md`/`tests/test_postgres_compat.py` establish the
  pattern for dialect-gated tests (skipped locally, run in CI's
  `test-postgres` job against a real `postgres:16` service container).
  Both patterns are reused below instead of inventing new ones.
- Migrations are a single linear chain (`migrations/versions/`, checked via
  `down_revision`), using `op.batch_alter_table` for SQLite-safe
  constraint changes (see `8961da81a764_add_user_status_and_google_subject.py`
  for the established shape this slice's migration follows).

## 1. System framework catalog reconciliation (resolves finding 1)

### Why seeding and reconciliation must be separate mechanisms

`seed_if_empty`'s demo `InternalControl`/`Risk` rows are illustrative
example data — useful only on a brand-new, empty install, never wanted on
an install that already has real data. The ISO/SOC2 **catalog**
(frameworks, categories, requirements) is different: it's product data
every installation should have, old or new, and it must be safe to add to
an installation that already has months of real assessments, controls, and
mappings on top of it. Gating catalog delivery on "no framework exists
yet" (the first draft's plan) means an existing ISO-only install would
never receive the SOC2 catalog, because its one existing `Framework` row
makes that check `True` forever. These need two different gates.

### `app/framework_catalog.py` (new module)

Defines the system catalog as plain Python data (not seeded imperative
code) and one idempotent reconciliation function:

```python
@dataclass(frozen=True)
class CategoryDef:
    code: str            # stable, e.g. "security", "organizational"
    display_name: str
    is_mandatory: bool
    default_in_scope: bool
    display_order: int

@dataclass(frozen=True)
class RequirementDef:
    reference_code: str
    title: str
    summary: str
    category_code: str
    display_order: int

@dataclass(frozen=True)
class FrameworkDef:
    code: str             # immutable system catalog key, e.g. "iso27001-2022"
    family: str           # stable across catalog versions, e.g. "iso27001"
    name: str
    version: str
    description: str
    default_active: bool
    categories: tuple[CategoryDef, ...]
    requirements: tuple[RequirementDef, ...]

SYSTEM_CATALOG: tuple[FrameworkDef, ...] = (ISO_27001_2022, SOC2_2017_RPOF_2022)

def reconcile_system_catalog(session: Session, *, actor: str = "system") -> ReconciliationResult: ...
```

**Algorithm — additive only, never overwrites existing content:**

For each `FrameworkDef` in `SYSTEM_CATALOG`:

1. Look up `Framework` by `code` (unique). If absent, **create** it
   (`is_system_provided=True`, `is_placeholder_content=True`,
   `is_active=default_active`) and write an `AuditEvent`
   (`actor="system"`, action `"system_reconcile_create"`). If present,
   touch nothing about it — name/version/description/is_active are
   whatever the install currently has.
2. For each `CategoryDef`: look up `FrameworkCategory` by
   `(framework_id, code)` (unique). If absent, create it with
   `in_scope=default_in_scope`, `is_mandatory`, `is_system_provided=True`,
   audited the same way. If present, **only** `is_mandatory` is re-synced
   from the catalog on every run (it's a product-level constraint, not a
   user choice — see "Canonical category model" below); `in_scope` is
   never touched once the row exists, because that's the org's own scope
   decision.
3. For each `RequirementDef`: look up `FrameworkRequirement` by
   `(framework_id, reference_code)` — the existing unique constraint,
   reused as the stable key. If absent, create it via `add_requirement(...,
   category_id=..., is_system_provided=True)` (which also creates its
   paired `RequirementAssessment`, per that helper's existing contract),
   audited. If present **and its `category_id` is currently `NULL`**, set
   `category_id` — this is populating an empty field that nothing else in
   the app can currently set (there is no requirement-edit route), not
   overwriting user data. `title`/`summary`/`display_order` of an existing
   row are never touched by reconciliation, full stop — even for rows the
   reconciler itself created earlier, in case a future catalog release
   corrects the placeholder text. Re-syncing existing catalog text is
   explicitly out of scope for this slice (see "Explicitly deferred").

This makes the whole function safe to call on every single startup,
against any installation shape: empty, ISO-only, ISO+SOC2, or ISO plus an
admin-created custom framework — it only ever creates rows keyed by a
system catalog code/reference-code it owns, or fills a `NULL` field that
nothing else populates. It never inspects or reacts to unrelated custom
framework rows.

### Where reconciliation runs

Three-part answer, matching the requirement to define and justify this:

- **Automatically at application startup**, in `create_app()`, immediately
  after `init_db()` (schema migrations) and before `seed_if_empty` (demo
  data). Same "self-heals on every restart, no ops action needed" property
  `seed_if_empty` already has today.
- **Also exposed as an explicit CLI command**,
  `python -m app.cli reconcile-framework-catalog`, for ops to run on
  demand (e.g. after restoring a backup, or to verify idempotency in a
  runbook) without restarting the process.
- **Never inside an Alembic migration.** Migrations in this repo are
  schema-only (`add_column`, `create_table`, `batch_alter_table`) — see
  every existing revision in `migrations/versions/`. Catalog *content*
  (titles, summaries, category assignments) is application data that may
  need a future correction release without a matching schema change;
  coupling it to migration history would force a new Alembic revision for
  every future wording fix. The migration in this slice only adds the
  columns/tables reconciliation needs (see "Safe migration strategy"); it
  never itself inserts `Framework`/`FrameworkCategory`/`FrameworkRequirement`
  rows.

### `app/seed.py` after this change

`seed_if_empty`'s framework/requirement-creation block is deleted — that's
now `reconcile_system_catalog`'s job, and it always runs first. Its guard
condition changes from "no `Framework` exists" (which reconciliation would
make permanently `False`, silently killing demo-data seeding on every
fresh install) to **"no `InternalControl` exists"** — the actual signal
that demo data hasn't been seeded yet. The demo controls/risks now look up
the ISO framework's known reference codes (`A.5.1`, `A.5.9`, `A.8.1`,
`A.8.8`, `A.5.30`) via `select(FrameworkRequirement).where(reference_code
== ...)` instead of holding local references from a creation step it no
longer performs — those rows are guaranteed to exist because reconciliation
always runs before `seed_if_empty` in `create_app()`.

### Audit distinction (system vs. user-initiated)

`AuditEvent.actor` already fully separates these — reconciliation always
passes `actor="system"` (matching `app/seed.py`'s existing convention for
its own seed rows), while every route-driven change in this slice passes
`request.state.user.email`. No schema change needed; this is called out
explicitly so the distinction isn't accidentally lost during
implementation.

## 2. Safe migration strategy for the pre-existing ISO framework (resolves finding 2)

### Why the first draft's backfill was wrong

`UPDATE frameworks SET code = 'iso27001' WHERE code IS NULL` stamps
**every** framework row with no code — including any custom framework an
install has already created via `POST /frameworks` (fully possible today,
that route has always existed and isn't framework-type-aware). That
mislabels a custom framework as ISO 27001 and, if more than one such row
exists, would violate the new `UNIQUE` constraint on `code` outright.

### Identification: fingerprint match, not a blanket update

Every pre-feature installation's ISO framework was created by the *exact*
literal text in today's `app/seed.py` (`name="ISO/IEC 27001:2022 Annex A
(sample catalogue)"`, `version="2022"`, `is_placeholder_content=True`) —
this app has never offered any other way to produce that exact row. The
migration's data step matches on that literal fingerprint and requires an
**exact single match**:

```sql
-- pseudocode; implemented as a parametrized statement in the migration
SELECT id FROM frameworks
WHERE name = 'ISO/IEC 27001:2022 Annex A (sample catalogue)'
  AND version = '2022'
  AND is_placeholder_content = true
```

- **Exactly one match** → that row gets
  `code='iso27001-2022'`, `family='iso27001'`, `is_system_provided=true`.
  This is the only row this migration ever writes to.
- **Zero or more than one match** → **nothing is backfilled**. This is the
  explicit "ambiguous installation" case (an admin renamed the seeded
  framework, or somehow created more than one row with that exact
  fingerprint). Those rows are left exactly as they are —
  `code=NULL`, `family=NULL`, `is_system_provided=false` — which is a
  fully valid, permanently supported state (`code`/`family` are nullable
  precisely for this: an unidentified or custom framework). Startup
  reconciliation (§1) will then find no `Framework` with `code =
  'iso27001-2022'` and create a **new** one (disabled-by-default status
  per `default_active`, same as any fresh install) alongside the
  unidentified row, and records an audit event
  (`action="system_reconcile_create"`, detail noting the pre-existing
  unmatched row's id) so an admin reviewing the audit log can see and
  reconcile the apparent duplicate by hand. This is a deliberately rare
  edge case (it only triggers if an admin edited the seeded ISO
  framework's name/version away from the exact seed text, or hand-created
  a duplicate of it) traded off against the alternative of ever silently
  guessing at a framework's identity.

### Stable code/family policy

- `Framework.code`: **immutable, version-specific system catalog key**
  (e.g. `"iso27001-2022"`, `"soc2-2017-rpof-2022"`). `NULL` for any
  framework this app didn't create from its own catalog (custom, or an
  unmatched legacy row). Unique where non-null.
- `Framework.family`: **stable across catalog versions**, used by app
  logic that shouldn't break if a future catalog version bump changes
  `code` (e.g. `"iso27001-2022"` → a hypothetical future
  `"iso27001-2027"` would still be `family="iso27001"`). Not unique — this
  slice only ships one version per family, but the column exists so a
  future catalog version doesn't need a new column. `NULL` for
  custom/unidentified frameworks.
- `Framework.is_system_provided`: `True` only for rows created or
  identified by reconciliation/migration; `False` for anything created
  through `POST /frameworks` or left unidentified by the migration. This
  is what "custom framework" means operationally in this app: any
  framework with `is_system_provided=False`.
- Display name (`Framework.name`) stays fully independent and admin-
  editable via the existing metadata edit route — editing it never
  affects `code`/`family`/`is_system_provided` (those are set once, at
  creation/identification time, and never derived from `name` again).

### Existing custom frameworks without a code

They keep `code=NULL`, `family=NULL`, `is_system_provided=False`
indefinitely — this is a supported, permanent state, not a transient one
pending some future backfill. No part of this slice (or, per the "no
single-framework assumption" constraint, any future slice) may assume
`code` is non-null.

## 3. Authorization: one domain service, no bypass path (resolves finding 3)

### `app/framework_admin.py` (new module)

The **only** code path allowed to change `Framework.is_active` or
`FrameworkCategory.in_scope`. Two functions, both requiring the caller to
have already checked `require_admin` (the service re-asserts nothing about
roles itself — enforcement is the router dependency; the service enforces
the *data* invariants that must hold regardless of caller):

```python
class LastActiveFrameworkError(Exception): ...
class MandatoryCategoryError(Exception): ...

def set_framework_active(
    session: Session, framework_id: str, *, active: bool, actor: str
) -> Framework:
    """Raises LastActiveFrameworkError if this would leave zero active
    frameworks. See 'Concurrency' below for why the SELECT is locked."""
    ...

def set_category_in_scope(
    session: Session, category_id: str, *, in_scope: bool, actor: str
) -> FrameworkCategory:
    """Raises MandatoryCategoryError if category.is_mandatory."""
    ...
```

Both write an `AuditEvent` with actor, before-state, after-state, and the
affected framework/category id in `detail`, e.g.:
`detail=f"framework_id={framework.id} name={framework.name!r} before=active after=inactive"`.

### Closing the two existing bypasses

1. **`POST /frameworks/{id}/edit` (`update_framework`) drops the
   `is_active` form field entirely.** That route becomes metadata-only
   (`name`/`version`/`description`/`is_placeholder_content`), stays
   available to any logged-in user (unchanged from today, consistent with
   this app's existing "role gates only integration/credential/destructive
   operations" convention — renaming a framework isn't one of those), and
   can no longer touch activation state at all, regardless of what a
   crafted POST body contains — the FastAPI route signature simply has no
   `is_active` parameter to bind to.
2. **`POST /frameworks` (`create_framework`) always creates
   `is_active=False`.** A brand-new custom framework starts inactive; an
   admin must explicitly activate it through the new admin-gated route.
   This closes "framework creation can bypass activation rules" without
   making framework *creation* itself admin-only (unchanged from today —
   only *activation* is admin-gated, per Feature 14 item 9's exact list).

### New admin-gated routes (`app/routers/admin_settings.py`)

- `POST /admin/settings/frameworks/{framework_id}/toggle` —
  `require_admin` + `verify_csrf`. Calls `set_framework_active`. Catches
  `LastActiveFrameworkError` → flash error, no mutation, no audit event.
- `POST /admin/settings/categories/{category_id}/toggle` —
  `require_admin` + `verify_csrf`. Calls `set_category_in_scope`. Catches
  `MandatoryCategoryError` → flash error (or a `400` for a direct API-style
  call), no mutation, no audit event. The Security category's checkbox is
  additionally rendered `disabled` in the template — the route-level
  rejection is the real enforcement; the disabled checkbox is only UX
  polish on top of it, exactly like the first draft already intended.

Both routes are the *only* way to reach `set_framework_active`/
`set_category_in_scope` — nothing else in the router layer calls them.

### Concurrency: the "at least one active framework" invariant

**SQLite:** `app/db.py::_set_sqlite_pragmas` already sets
`PRAGMA journal_mode = WAL`, which serializes all writers — only one write
transaction commits at a time; a second concurrent writer blocks (up to
`busy_timeout`) rather than racing. A read-then-write invariant check
inside one request's transaction cannot be raced by another SQLite writer
today. This is stated explicitly rather than left implicit, per the
requirement to address SQLite's concurrency behavior directly.

**PostgreSQL:** has real row-level MVCC concurrency — two transactions
updating two *different* `Framework` rows do not block each other under
default `READ COMMITTED` isolation, so a naive "count other active rows,
then update" check has a genuine TOCTOU race (two admins simultaneously
disabling the last two active frameworks could both pass the check and
commit, leaving zero active). `set_framework_active` closes this with an
explicit lock read before the invariant check:

```python
active_rows = session.execute(
    select(Framework).where(Framework.is_active.is_(True)).with_for_update()
).scalars().all()
```

On PostgreSQL this takes real row locks on every currently-active
framework row, so a second concurrent call to `set_framework_active`
blocks until the first transaction commits or rolls back, then re-reads
the now-current state — the race is closed. On SQLite,
`with_for_update()` is a documented no-op (SQLAlchemy's SQLite dialect
does not emit `FOR UPDATE`, the same "harmless no-op wrapper" pattern this
codebase already relies on for `op.batch_alter_table`) — which is fine,
because SQLite's WAL single-writer serialization already makes the race
impossible without it, as above.

### Tests

- Route-level authorization: non-admin gets `403` on both new `POST`
  routes and on the settings `GET` page.
- Bypass attempts: a direct `POST /frameworks/{id}/edit` with an
  `is_active` field in the body does not change `is_active` (the field is
  simply ignored/rejected by FastAPI's form binding since the parameter no
  longer exists); a direct `POST /frameworks` does not create an active
  framework regardless of body content.
- Service rules: `set_framework_active` raises `LastActiveFrameworkError`
  when disabling the only active framework, with no DB mutation and no
  audit event; `set_category_in_scope` raises `MandatoryCategoryError` for
  the Security category, with no DB mutation and no audit event.
- Concurrency:
  - A SQLite test documenting (not just asserting) that two sequential
    same-process calls attempting to disable the last two active
    frameworks result in exactly one success and one
    `LastActiveFrameworkError` — this is a correctness test of the
    application-level check, not a true concurrency test, and the spec/
    test docstring says so explicitly, per the SQLite behavior called out
    above.
  - A PostgreSQL-gated test (`tests/test_postgres_compat.py`-style,
    `skipif not TEST_DATABASE_URL`) that starts two real threads/
    connections each attempting to disable one of the last two active
    frameworks concurrently, and asserts the final active count is
    exactly 1 (not 0), proving the `with_for_update()` lock actually
    serializes them on a real Postgres server.
- Audit content: asserts the recorded `AuditEvent` includes actor,
  before-state, after-state, and the framework/category id — not just
  "an event was written."

## 4. Framework and category disable semantics (resolves finding 4)

This section is the actual, enforced contract — not a docstring claim.

### What "framework disabled" changes

| Surface | Behavior when `Framework.is_active = False` |
|---|---|
| `GET /` dashboard summary | **Excluded** — already true today (`dashboard.py` filters `is_active.is_(True)`); unchanged by this slice. |
| `GET /frameworks` (list) | **Included, unchanged.** Still shows every framework, active or not — an admin managing frameworks needs to see disabled ones to re-enable them. No new "hide from list" behavior is added; this is deliberate, not an oversight. |
| `GET /frameworks/{id}` (detail), `GET .../requirements/{id}` (detail) | **Fully readable**, unchanged — historical data must stay inspectable. |
| `POST .../requirements/{id}/assessment` (assessment update) | **Rejected** with a flash error ("This framework is disabled — re-enable it under Admin > Settings to make changes.") and no mutation, if `requirement.framework.is_active` is `False`. |
| `POST .../requirements/{id}/notes` (add note) | Same rejection rule as assessment update. |
| `POST /frameworks/{id}/requirements` (add requirement) | Same rejection rule. |
| `POST /frameworks/{id}/import` (CSV import) | Same rejection rule. |
| `POST /frameworks/{id}/edit` (metadata edit) | **Still allowed** — renaming/describing a disabled framework isn't "new compliance work" and shouldn't be blocked; only the four routes above (the ones that create or change assessment-relevant state) are gated. |
| Evidence/control mappings, Trust Center links, audit history | **Untouched** — nothing in this app currently filters any of those by `Framework.is_active`, and this slice adds no such filter. A disabled framework's requirements remain valid targets of existing `ControlRequirementMapping`/`EvidenceRequirementMapping` rows. |

This makes "disabled" mean exactly: *hidden from the dashboard's top-line
summary, and closed to new assessment/note/requirement/import activity* —
while remaining fully visible and fully re-activatable. Re-enabling a
framework requires zero data repair, because nothing was ever deleted or
rewritten — the four gated routes simply start accepting requests again.

### What "category out of scope" changes

`FrameworkCategory.in_scope = False` **only** ever means "this TSC is
recorded as not in this org's SOC2 audit scope." It does **not**:

- bulk-change any `RequirementAssessment.applicable` value for
  requirements under that category (explicitly rejected as an approach —
  see the first draft's "TSC scope model" decision, which is preserved:
  scope state lives only in `FrameworkCategory.in_scope`, never bleeds
  into per-requirement assessment data);
- delete or hide any `FrameworkRequirement`, `RequirementAssessment`,
  `RequirementNote`, evidence mapping, or control mapping under it;
- change `compute_progress`'s output in any way.

**Explicit, named gap for this slice:** `compute_progress` does not yet
filter by category scope at all — enabling/disabling a TSC has *no* effect
on any readiness percentage today. That's `app/progress.py` unmodified.
Making readiness respect scope is exactly the "framework-specific
readiness calculation," which the parent feature and this spec both
already defer to a later slice (it depends on the not-yet-built
cross-framework mapping catalog for the "combined coverage" view to make
sense). Restoring a category to in-scope is therefore trivially lossless
by construction — nothing was ever touched.

### ISO categories are not exposed for toggling in this slice

ISO's 4 Annex A themes get `FrameworkCategory` rows too (see §5) so the
model is genuinely shared, not SOC2-special-cased — but the admin settings
page in this slice only renders scope checkboxes for the SOC2 framework's
categories. ISO's category rows exist purely as data (for
`FrameworkRequirement.category_id` linkage, ready for later slices) with
`in_scope=True` and no UI to change it yet — real ISO practice doesn't
have a "theme-level opt out" concept the way SOC2 has TSC selection, so
there's no product requirement to build that toggle now, and not building
it avoids inventing UI nobody asked for.

## 5. Canonical category model (resolves finding 5)

Replaces the free-string `category`/`FrameworkScope` design from the first
draft entirely with one normalized table, shared by both frameworks:

```python
class FrameworkCategory(Base):
    """A named grouping within a framework — an ISO Annex A theme or a
    SOC2 Trust Services Category. `is_mandatory` is a catalog-owned
    product rule (Security/Common Criteria must always be in scope for a
    SOC2 report) and is kept in sync by system reconciliation on every
    run; `in_scope` is the org's own scope decision and is never touched
    by reconciliation once the row exists.
    """

    __tablename__ = "framework_categories"
    __table_args__ = (
        UniqueConstraint("framework_id", "code", name="uq_framework_category_code"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    framework_id: Mapped[str] = mapped_column(ForeignKey("frameworks.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    in_scope: Mapped[bool] = mapped_column(default=True)
    is_mandatory: Mapped[bool] = mapped_column(default=False)
    is_system_provided: Mapped[bool] = mapped_column(default=False)
    display_order: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    framework: Mapped[Framework] = relationship()
    requirements: Mapped[list[FrameworkRequirement]] = relationship(back_populates="category")
```

`FrameworkRequirement` gets a nullable FK instead of a free string:

```python
category_id: Mapped[str | None] = mapped_column(ForeignKey("framework_categories.id"), nullable=True)
is_system_provided: Mapped[bool] = mapped_column(default=False)

category: Mapped[FrameworkCategory | None] = relationship(back_populates="requirements")
```

`app/requirements.py::add_requirement` gains
`category_id: str | None = None` and `is_system_provided: bool = False`
keyword arguments, threaded straight through. The three existing call
sites (manual add route, CSV import, and — after §1's refactor —
reconciliation) are unaffected unless they choose to pass these.

Naming note: `is_mandatory` replaces the first draft's ambiguous
`is_default` — it says exactly what it enforces (this category cannot be
toggled out of scope), rather than describing an initial value that could
be read as changeable later.

### Uniqueness and integrity

- `(framework_id, code)` unique — a category code is stable and unique
  *within* a framework, not globally (ISO's `"technological"` and a
  hypothetical future framework's own `"technological"` theme are
  different rows, not a collision).
- `FrameworkRequirement.category_id` is a real FK (`ON DELETE` behavior:
  no delete route exists for `FrameworkCategory` in this slice, so this is
  not yet exercised — left at the SQLAlchemy/Alembic default, restrict).
- `display_name` is the human-readable label; `code` is the stable,
  never-displayed-as-the-primary-label identifier reconciliation and any
  future app logic matches against.

### SOC2 categories seeded (§6 has the requirement catalog itself)

| `code` | `display_name` | `is_mandatory` | `default_in_scope` |
|---|---|---|---|
| `security` | Security (Common Criteria) | `True` | `True` |
| `availability` | Availability | `False` | `False` |
| `processing_integrity` | Processing Integrity | `False` | `False` |
| `confidentiality` | Confidentiality | `False` | `False` |
| `privacy` | Privacy | `False` | `False` |

### ISO categories seeded

| `code` | `display_name` | `is_mandatory` | `default_in_scope` |
|---|---|---|---|
| `organizational` | Organizational | `False` | `True` |
| `people` | People | `False` | `True` |
| `physical` | Physical | `False` | `True` |
| `technological` | Technological | `False` | `True` |

None of ISO's categories are mandatory or exposed for toggling in this
slice (see §4) — `is_mandatory=False` here just means "the toggle-guard
rule doesn't apply," not that a toggle UI exists for them.

### Migration behavior for existing ISO requirements

The migration (§2) only adds the nullable `category_id` column — it does
not populate it for any pre-existing row. Reconciliation (§1) is what
populates it, using its "only fill a `NULL` field, never overwrite" rule,
for the 5 known ISO reference codes on whichever `Framework` row ends up
identified as `code='iso27001-2022'` (either the migration-backfilled
existing row, or a freshly reconciled one in the ambiguous case).

## 6. SOC2 catalog strategy (resolves finding 6)

**Catalog identity:** "2017 Trust Services Criteria with Revised Points of
Focus — 2022," stored as `code="soc2-2017-rpof-2022"`,
`family="soc2"`, `version="2017 (rPOF 2022)"`.

**Decision: Option B — a mandatory catalog-completion slice (1b), not full
transcription now.** The AICPA's complete Trust Services Criteria — all
nine Common Criteria series with their full points of focus, plus the
Availability/Confidentiality/Processing Integrity/Privacy supplemental
criteria — is licensed material this repository cannot reproduce (same
copyright boundary as ISO's Annex A, see `docs/domain/domain-model.md`),
and getting the *complete, accurate* set of stable identifiers right
(exact sub-criterion numbering and count per category) requires checking
against an authoritative source this task doesn't have available to
verify against. Rather than risk inventing incorrect identifiers under
"do not invent criterion text," this slice ships a **wider-than-before but
still explicitly partial skeleton**, and treats full completion as its own
follow-up slice that must land before the cross-framework mapping catalog
slice (which needs real per-criterion identifiers to map against).

**Slice 1 SOC2 requirement skeleton** — one representative placeholder row
per Common Criteria series (all under the mandatory `security` category,
mirroring the CC-series structure) plus one per supplemental category,
13 rows total (up from the first draft's 6), titles paraphrased exactly
like ISO's placeholders:

| `reference_code` | `category_code` | Paraphrased placeholder title |
|---|---|---|
| `CC1.1` | `security` | Control environment and organizational structure |
| `CC2.1` | `security` | Communication and information for internal control |
| `CC3.1` | `security` | Risk identification and assessment |
| `CC4.1` | `security` | Monitoring of control performance |
| `CC5.1` | `security` | Selection and development of control activities |
| `CC6.1` | `security` | Logical and physical access controls |
| `CC7.1` | `security` | System operations and anomaly detection |
| `CC8.1` | `security` | Change management |
| `CC9.1` | `security` | Risk mitigation |
| `A1.1` | `availability` | Capacity and availability monitoring |
| `PI1.1` | `processing_integrity` | Processing accuracy and completeness |
| `C1.1` | `confidentiality` | Confidential information identification and protection |
| `P1.1` | `privacy` | Notice and communication of privacy practices |

**Provenance/version metadata:** `Framework.is_placeholder_content=True`
(same field ISO uses — no new per-requirement field needed, since the
distinction is already framework-level and consistent with existing
precedent) plus a description explicitly stating: catalog identity string
above, that titles/summaries are paraphrased placeholders not AICPA's
licensed text, and that **SOC 2 is an audit/reporting framework — this app
never performs or implies a SOC 2 examination or opinion; only an
independent CPA firm can issue one.**

**Slice 1b (new, mandatory, next after this one, before the mapping
catalog slice):** source a verified, complete TSC identifier/title
skeleton against an authoritative reference, expand the 13 rows above to
the full set, and confirm no part of it drifts into reproducing licensed
descriptive text. This is called out as a real open item — see "Remaining
design decisions requiring user input" below.

## 7. Migration and regression test coverage (resolves finding 7)

- **Fresh installation**: `init_db` → reconciliation → `seed_if_empty` on
  an empty DB produces both frameworks (`iso27001-2022` active,
  `soc2-2017-rpof-2022` inactive), all categories, all requirements, and
  the existing demo controls/risks — asserted by table counts and spot-
  checked rows.
- **Existing ISO-only installation**: seed a DB shaped like today's
  `app/seed.py` output (pre-migration schema), run the migration then
  reconciliation, assert: the existing framework row gets
  `code='iso27001-2022'`/`family='iso27001'`/`is_system_provided=True`;
  its 5 existing requirements get `category_id` backfilled and keep their
  original `title`/`summary`/`id`; `is_active` stays `True`; SOC2 is added
  fresh and disabled; existing `InternalControl`/`ControlRequirementMapping`/
  `Risk`/`AuditEvent` rows are byte-for-byte unchanged.
- **Existing installation with multiple/custom frameworks**: seed the
  standard ISO framework plus a second, admin-created custom framework
  (arbitrary name/version, `is_system_provided` doesn't exist pre-
  migration so this is just "a second row with different name/version").
  Assert: only the fingerprint-matched ISO row gets a `code`; the custom
  row keeps `code=NULL`/`is_system_provided=False` permanently; SOC2 is
  still added correctly.
- **Ambiguous legacy framework data**: seed a DB where the "ISO" row's
  `name` has been edited away from the exact seed fingerprint (simulating
  an admin rename via the existing edit route). Assert: migration
  backfills nothing; reconciliation creates a **new**
  `code='iso27001-2022'` framework alongside the renamed one; an audit
  event notes the unmatched row's id; no existing data is deleted or
  altered.
- **Migration followed by startup reconciliation producing a disabled
  SOC2 framework**: explicit assertion that `soc2-2017-rpof-2022` is
  `is_active=False` immediately after first reconciliation, on both a
  fresh and an existing-ISO install.
- **Idempotent reconciliation across repeated startups**: call
  `reconcile_system_catalog` three times in a row against the same
  session/engine; assert row counts for frameworks/categories/requirements
  are identical after the 2nd and 3rd calls to after the 1st, and no
  duplicate `AuditEvent` "create" rows are written on the 2nd/3rd calls.
- **Preservation**: existing controls, mappings, assessments, evidence,
  applicability, notes, and custom frameworks are unchanged (covered
  across the scenarios above, plus a dedicated test that snapshots all
  `RequirementAssessment`/`RequirementNote`/`ControlRequirementMapping`/
  `EvidenceRequirementMapping` rows before and after reconciliation and
  asserts an exact match).
- **Disable/re-enable without data loss**: disable ISO via the service,
  assert dashboard excludes it and the 4 gated routes reject with no
  mutation, assert list/detail pages still render it fully; re-enable;
  assert every previously-existing row (assessments, notes, mappings) is
  still present and unchanged, and the 4 gated routes work again.
- **Remove/restore a SOC2 category scope without data loss**: toggle
  `availability` out of scope, assert no `RequirementAssessment` row
  changes; toggle back in scope; assert still no change anywhere except
  `FrameworkCategory.in_scope` itself and its audit trail.
- **Disable the final active framework**: attempt to disable the only
  active framework (in a fresh single-ISO install before SOC2 is
  activated) → `LastActiveFrameworkError`, no mutation, no audit event.
- **Non-admin mutation attempts through both new and legacy routes**:
  `403` on the two new admin routes; a non-admin's `POST
  /frameworks/{id}/edit` with an `is_active` field in the body has no
  effect (route no longer accepts that field at all); a non-admin's
  `POST /frameworks` cannot produce an active framework.
- **Concurrent admin toggles on PostgreSQL**: the `with_for_update()`-
  backed two-thread test described in §3, `skipif not TEST_DATABASE_URL`.
- **SQLite and PostgreSQL migration paths**: extend
  `tests/test_postgres_compat.py`'s existing
  `test_migrations_apply_cleanly_against_postgres` to also assert
  `frameworks.code`, `frameworks.family`, `frameworks.is_system_provided`,
  `framework_categories`, and `framework_requirements.category_id` exist
  post-migration on a live Postgres server (mirroring how that test
  already asserts `PIVOT_TABLES` and `users` columns).
- **Audit records containing actor and before/after state**: a dedicated
  assertion (not just "an AuditEvent exists") that parses/matches the
  `detail` string for actor, before, and after values on both the
  framework-toggle and category-toggle audit events.
- **SOC2-only, ISO-only, and both-enabled configurations**: three
  end-to-end tests at the foundation/configuration level — disable ISO
  and enable SOC2 (dashboard shows only SOC2's progress); ISO active,
  SOC2 left disabled (default fresh-install state); both active
  (dashboard shows both) — each asserting the dashboard's `framework_count`
  and `progress_list` reflect exactly the active set.

Test file layout: `tests/test_framework_catalog.py` (reconciliation +
migration scenarios), `tests/test_framework_admin.py` (service +
authorization + concurrency), `tests/test_admin_settings.py` (route-level:
rendering, CSRF, flash messages), extensions to `tests/test_requirements.py`
(`category_id`/`is_system_provided` plumbing through `add_requirement`),
`tests/test_pages.py` (add `/admin/settings` to the render sweep), and the
`tests/test_postgres_compat.py` extension above.

## Internal consistency check

- Table/column/route names are consistent everywhere in this document:
  `frameworks.code`/`family`/`is_system_provided`; `framework_categories`
  (not `framework_scopes` — that table from the first draft is dropped
  entirely, superseded); `framework_requirements.category_id`/
  `is_system_provided`; routes `POST
  /admin/settings/frameworks/{id}/toggle` and `POST
  /admin/settings/categories/{id}/toggle` (not `.../scope/{id}/toggle` —
  renamed to match the `FrameworkCategory` table name).
- System catalog data (`Framework`/`FrameworkCategory`/
  `FrameworkRequirement` rows where `is_system_provided=True`) is
  distinguished throughout from user-owned configuration
  (`Framework.is_active`, `FrameworkCategory.in_scope`, any custom
  framework/requirement) and assessment data (`RequirementAssessment`,
  `RequirementNote`) — reconciliation only ever touches the first
  category, and only additively.
- `ControlRequirementMapping` (control ↔ requirement, many-to-many, no
  framework FK) is unmodified and unmentioned as needing any change —
  explicitly preserved.
- No requirement-to-requirement mapping table is introduced anywhere in
  this document — that remains the next slice's job.
- Nothing in the data model, service, routes, or tests assumes exactly one
  framework per control (the existing many-to-many mapping already
  disproves that) or exactly one framework per installation (the
  multiple-custom-framework migration scenario in §7 exists specifically
  to exercise that).

## Explicitly deferred

- Cross-framework requirement↔requirement mapping catalog (confidence,
  system-vs-user-provided, notes) — next spec.
- Framework badges/filters in control register, control details,
  checklist, assessments, evidence, CSV import/export, reports,
  dashboards, search — next spec, depends on `category`/`code` existing.
- Framework-specific readiness/assessment views and computation
  (`compute_progress` scope-awareness) — next spec, depends on the
  mapping catalog.
- Evidence reuse UI clarity — next spec.
- **SOC2 catalog completion (slice 1b)** — see §6. Mandatory before the
  cross-framework mapping catalog slice begins.
- Re-syncing already-created catalog rows' `title`/`summary` text on a
  future catalog content correction — reconciliation in this slice is
  strictly create-or-fill-null, never update-existing-content.
- Full e2e browser scenario matrix (parent feature item 10) — spread
  across the specs above as each becomes testable end-to-end.

## Review findings resolved

| # | Finding | Resolved by |
|---|---|---|
| 1 | Seed gate (`seed_if_empty`) would never deliver SOC2 to existing ISO installs | §1: `app/framework_catalog.py::reconcile_system_catalog`, decoupled from `seed_if_empty`, runs on every startup + explicit CLI command; `seed_if_empty`'s guard changed to `InternalControl` existence. |
| 2 | Blind `UPDATE frameworks SET code = 'iso27001' WHERE code IS NULL` could mislabel custom frameworks or violate uniqueness | §2: exact-fingerprint match requiring a single unique hit; ambiguous/zero/multi-match cases left untouched, reconciliation adds the system row fresh instead of guessing; `code` vs `family` split for version-stable app logic. |
| 3 | `update_framework`/`create_framework` could mutate or create `is_active` outside admin gating | §3: `app/framework_admin.py` is the sole path to `is_active`/`in_scope` changes; `is_active` removed from the metadata edit route entirely; new frameworks always start inactive; `with_for_update()` closes the Postgres TOCTOU race, SQLite's WAL serialization addressed explicitly. |
| 4 | `is_active` was claimed to already mean "hidden from active workflows" without enforcement | §4: exact per-route table of what disabled changes (4 gated mutation routes) and what it doesn't (list/detail read access, evidence/control mappings, audit history) — all backed by tests in §7. |
| 5 | `category`/`FrameworkScope` were unrelated free-form strings with an ambiguous `is_default` field | §5: normalized `FrameworkCategory` table, FK from `FrameworkRequirement`, `is_mandatory` (catalog-owned, renamed from `is_default`) vs `in_scope` (user-owned) split, shared by both ISO and SOC2 with documented differing UI exposure. |
| 6 | 6 representative SOC2 criteria are too few for later mapping/readiness slices, and inventing more risks fabricating licensed content | §6: explicit Option B — expanded-but-still-partial 13-row skeleton now, mandatory "slice 1b" catalog-completion slice called out before any mapping work begins. |
| 7 | Test/migration coverage didn't address multi-framework installs, ambiguity, or Postgres concurrency | §7: full scenario list including multi-custom-framework and ambiguous-legacy-data migration tests, and a real two-thread Postgres concurrency test alongside the existing SQLite-serialization argument. |

## Remaining design decisions requiring user input

- **§6, SOC2 catalog completeness**: this slice ships a 13-row skeleton
  and defers full AICPA TSC identifier coverage to a mandatory "slice 1b"
  rather than guessing at exact sub-criterion numbering/count without a
  verifiable source. If a licensed copy of the 2017 TSC (with 2022 rPOF)
  is available to the user/org, slice 1b should be scoped against it
  directly rather than a secondary-source reconstruction — worth
  confirming before that slice is written.
- **Metadata-edit route's authorization level** (§3): this revision keeps
  `POST /frameworks/{id}/edit` (name/version/description) open to any
  logged-in user, matching today's behavior and this app's general
  "role gates only integration/credential/destructive operations"
  convention — only *activation* is now admin-gated, per Feature 14 item
  9's literal list. If the intent was that *all* framework administration
  (including renaming/describing) should be admin-only, that's a one-line
  change to add `require_admin` to that route, but it wasn't explicitly
  asked for and would be a small scope expansion beyond what item 9 lists.
