# Issue #43: organization-level framework/catalog version pinning

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature/architecture

## Repository-reality check (read first)

Confirmed by direct inspection of `app/framework_catalog.py` and
`app/models.py::Framework`/`FrameworkRequirement`, before designing:

1. `reconcile_system_catalogs` **never mutates or removes an existing
   Framework/FrameworkRequirement row** — it is purely additive, keyed by
   `Framework.catalog_key`. A future second catalog version (e.g.
   `"soc2-2022-sample"`) would show up as a **brand-new** `Framework` row,
   never an in-place edit of `"soc2-2017-sample"`.
2. `ControlRequirementMapping` rows are permanent FKs to specific
   `FrameworkRequirement` rows, which themselves belong to one specific
   `Framework` row. Once a mapping exists against catalog version 1's
   requirement, that FK never repoints itself to a version-2 row —
   nothing in this codebase ever rewrites it.
3. **Consequence: historical control-to-requirement mapping/period/export
   data is already pinned to its original catalog version, structurally,
   with zero new code.** The issue's own worry — "an upgrade silently
   reinterpreting a past period against criteria the organization never
   adopted" — cannot happen through the mapping data itself, because
   nothing in this codebase ever repoints an existing mapping.
4. `ControlPeriod` (app/models.py) is **deliberately framework-neutral**
   by its own documented design ("periods are framework-neutral, matching
   the epic's principle that frameworks map onto controls, not the other
   way round"). Pinning a single `framework_id` onto `ControlPeriod`
   would violate that stated invariant and doesn't even make sense for an
   org running SOC 2 and ISO 27001 simultaneously. Rejected as a
   mechanism.
5. **What is actually missing**, confirmed by grep — there is no explicit,
   queryable fact anywhere in the codebase for "which catalog version is
   the organization's active program currently operating under, for a
   given framework family." Today that's only ever *implicit*, inferred
   from which `ControlRequirementMapping` rows happen to exist. An
   "upgrade" action has nothing to act on, because there is no "current
   adoption" state to move.
6. `Framework.catalog_key` (e.g. `"soc2-2017-sample"`) conflates **family**
   (SOC 2 vs ISO 27001) and **version** (2017 vs some future 2022) into one
   opaque string. There is no column that says "these two `Framework` rows
   are alternate versions of the same family" — needed before an upgrade
   action can know what it's upgrading *to*.
7. #12's own design doc (already merged) explicitly names #43 as "a
   separate, narrower, not-yet-scheduled P2" relative to the larger,
   never-merged "Feature 14" multi-framework design
   (`docs/superpowers/specs/2026-08-17-issue12-soc2-primary-framework-design.md`
   §1.3) — confirming this issue is intentionally scoped narrower than
   that abandoned design (no `FrameworkCategory`/TSC-scoping model, no
   `CatalogConflict` admin UI, no admin-only gating of the existing
   framework routes). This design does not resurrect that scope.

## What this design actually adds

### 1. `Framework.catalog_family` (new nullable column)

Distinguishes "same conceptual catalog, different version" from
"unrelated framework." Set by `reconcile_system_catalogs` for every
`SYSTEM_CATALOGS` entry going forward (`"iso27001"`, `"soc2"`); `NULL` for
every user-created custom framework, exactly like `catalog_key` already
is. Migration backfills the two existing rows using an explicit,
hardcoded `catalog_key -> catalog_family` mapping (not a parsing
heuristic) — matches #42's precedent for deterministic, documented
backfill rules.

### 2. `FrameworkAdoption` — a new, small, event-sourced aggregate

One row per `catalog_family` (a natural singleton scope, consistent with
"one deployment = one organization" — no `org_id` needed). Tracks which
`Framework.id` the organization currently treats as its adopted version
for that family, and the one it was previously on, if any.

```
FrameworkAdoption
  id                     == aggregate_id
  catalog_family          unique
  framework_id            currently-adopted Framework.id
  previous_framework_id   nullable — the Framework.id adopted before the most recent upgrade
  adopted_at               when this family was first adopted
  upgraded_at              nullable — when the most recent upgrade happened
  actor_type / actor_id
```

Events (`app/events.py::append_and_project`, aggregate_type
`"framework_adoption"`):

- `FrameworkAdoptionAdopted` — first adoption of a family. Payload:
  `catalog_family`, `framework_id`.
- `FrameworkAdoptionUpgraded` — the explicit, deliberate action the issue
  asks for. Payload: `catalog_family`, `from_framework_id`,
  `to_framework_id`. **Never touches any `ControlRequirementMapping`,
  `FrameworkRequirement`, or `ControlPeriod` row** — it only moves which
  `Framework.id` is treated as "current" going forward. Existing mappings
  against the old version are left exactly where they are (per #6 above,
  they already stay correctly pinned); the issue's explicit non-goal
  ("no general diff/migration engine") means this action does not attempt
  to re-map or copy anything.

`app/framework_adoption.py` functions:

- `adopt_framework(session, framework, *, actor_type, actor_id)` —
  requires `framework.catalog_family is not None`. Idempotent if already
  adopted to that exact framework; raises if the family already has a
  *different* adopted framework (must go through `upgrade_framework_adoption`
  instead — adoption and upgrade are deliberately distinct actions with
  distinct audit trails, matching the issue's "distinct from and never
  conflated with ordinary requirement/mapping edits").
- `upgrade_framework_adoption(session, adoption, to_framework, *, actor_type, actor_id)` —
  requires `to_framework.catalog_family == adoption.catalog_family` and
  `to_framework.id != adoption.framework_id`; raises `FrameworkAdoptionError`
  otherwise (e.g. an attempt to "upgrade" across families, or a no-op
  upgrade to the same version).
- `get_adoption(session, catalog_family)` / `list_adoptions(session)`.

### 3. Authorization

Both actions gated behind `require_write_access` (admin/operator), the
same bar every other framework-mutating route in `app/routers/frameworks.py`
already uses — no new, stricter authorization tier introduced without a
concrete reason; consistent with existing precedent rather than an
arbitrary new bar.

### 4. Minimal UI

`GET /frameworks/adoptions` — one row per `catalog_family`, showing the
currently adopted version and, if a newer sibling `Framework` row exists
in the same family that isn't yet adopted, an "Upgrade" action.
`POST /frameworks/adoptions/{catalog_family}/upgrade`. Today, with exactly
one version per `SYSTEM_CATALOGS` family, this page correctly shows "up
to date, no newer version available" for both — this is infrastructure
for the future second-version scenario the issue itself says isn't
imminent yet, proven correct now via synthetic test data rather than
waiting for a real second catalog edition to exist.

## Historical-pinning proof (the issue's own required verification)

Test scenario, built with synthetic Framework rows (no need to wait for a
real second SOC 2/ISO edition): org adopts catalog v1 → maps a control to
a v1 requirement → runs a control period/audit-package export against
that mapping → a synthetic catalog v2 (same `catalog_family`) is
reconciled → org explicitly upgrades adoption v1 → v2 → assert: the
original mapping/period/export data is byte-for-byte unchanged and still
resolves through the v1 `Framework`/`FrameworkRequirement` rows; a new
mapping created after the upgrade can target v2's requirements; the v1
`Framework` row itself is untouched (still exists, still `is_active`,
never deleted/renamed).

## Test strategy

- **Unit** (`tests/test_framework_adoption.py`): adopt/idempotent-re-adopt/
  reject-family-mismatch-on-adopt/reject-same-family-second-adopt (must use
  upgrade)/upgrade success/upgrade-rejects-cross-family/upgrade-rejects-
  same-version/projection rebuild reproduces the same current state.
- **Integration**: the historical-pinning scenario above, exercised through
  real `ControlRequirementMapping` + `ControlPeriod` + the existing #34
  audit-package export path, proving the export is unaffected by a later
  upgrade.
- **Migration**: `catalog_family` backfill verified via a real upgrade →
  downgrade → upgrade ×2 cycle against a live SQLite file (this session's
  established habit for schema changes).
- **Browser/E2E**: `/frameworks/adoptions` list + upgrade POST through real
  auth/CSRF; a reader/auditor cannot trigger an upgrade.
- **SQLite/PostgreSQL**: plain SQLAlchemy Core/ORM throughout, no
  backend-specific SQL.
- **No headless UAT required for this slice**: the adoption/upgrade
  mechanism has no real second catalog version to exercise in a live
  deployment today (per the issue's own framing, this is forward-looking
  infrastructure) — matches this session's established precedent for
  backend-only-with-a-thin-admin-surface issues (#42's control-definition
  lifecycle took the same position). The browser/E2E coverage above proves
  the real HTTP/auth/CSRF path; a headless UAT scenario would only be able
  to exercise it against the two real, singleton, already-adopted-by-default
  system catalogs, which have no upgrade target to click — it would not
  prove anything the E2E test doesn't already prove more directly.
