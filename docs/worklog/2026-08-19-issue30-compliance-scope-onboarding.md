# Issue #30: Compliance scoping, SOC 2 program bootstrap, and guided onboarding

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a real, event-sourced compliance-scope domain (`ComplianceScope`,
singleton per deployment) and a guided onboarding checklist
(`app/onboarding.py`) that reads existing authoritative state — no new
mutable "progress" tracker. See
`docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-design.md`
for the full repository-reality inventory and the several places this
design deliberately narrows the issue/PRD wording (no new framework-
selection step, no TSC-filtered starter-control generation, no starter
*policy* content, `in_scope` added directly to `Person`/`VendorSystem`
rather than a new join table or asset model) — each with the reasoning for
why, not a silent scope cut.

## What changed

- `app/models.py` — `ComplianceScope` (event-sourced singleton projection
  over the new `"compliance_scope"` aggregate) and `TRUST_SERVICE_CATEGORIES`;
  `in_scope: bool` added to `Person` and `VendorSystem`.
- `app/compliance_scope.py` (new) — `define_or_revise_scope` (validates the
  TSC allow-list and always includes "security"; validates audit-period
  ordering), projectors, `rebuild_compliance_scope_projection`.
- `app/starter_controls.py` (new) — `generate_starter_controls_for_framework`:
  idempotent, additive-only placeholder-control generation for every
  uncovered `FrameworkRequirement`, reusing the plain-row + `record_audit_event`
  shape `app/framework_catalog.py`/`app/seed.py` already use for bootstrap
  actions.
- `app/onboarding.py` (new) — `compute_onboarding_steps`: six steps, each a
  pure read over real tables (define scope, scope people/systems, generate
  starter controls, assign owners, configure an evidence source, start an
  operating period).
- `app/routers/onboarding.py` (new) — `/onboarding` checklist,
  `/onboarding/scope` view/edit form, `/onboarding/generate-starter-controls`;
  `require_login` for reads, `require_write_access` + `verify_csrf` for
  mutations (#37's convention).
- `app/routers/people.py`, `app/routers/vendor_systems.py` — existing edit
  routes gain an `in_scope` field; existing `record_audit_event` calls on
  those routes already cover it, no new audit plumbing needed.
- `app/routers/dashboard.py` / `app/templates/dashboard.html` — a small
  "N/6 onboarding steps complete" banner linking to `/onboarding` when
  incomplete (the full readiness-landing rebuild is #33's job, not this
  one's).
- `app/templates/onboarding/{checklist,scope_form}.html`,
  `app/templates/people/detail.html` (+`in_scope` field),
  `app/templates/vendors/{_form,detail}.html` (+`in_scope` field).
- New migration `f3a1b6c2d4e8` — `compliance_scopes` (new table, no
  backfill) + `in_scope` on `people`/`vendor_systems` (backfilled to
  `False` via `sa.false()` server_default, matching
  `a248573ccdfe`'s `is_primary` precedent — no existing person/vendor
  system retroactively becomes "in scope").

## Repository-reality findings that shaped the design (not bugs — documented up front)

- `app/framework_catalog.py::reconcile_system_catalogs` already seeds a
  SOC 2 sample catalog with `is_primary=True` on every startup, so "a
  primary framework exists" is already true before onboarding ever runs —
  no separate framework-selection step was built.
- `app/seed.py::seed_if_empty` already creates 4 demo `InternalControl`
  rows (all with an owner) mapped to 6 of the 14 seeded sample
  requirements (including SOC 2's `CC1.1`), unconditionally, on every
  fresh deployment. Two consequences, both intentional and tested:
  - the "starter controls" step correctly starts *incomplete* even though
    `InternalControl` rows already exist, because `CC2.1`-`CC9.1` remain
    uncovered (`test_starter_controls_step_reflects_real_coverage_gap`);
  - the "assign owners" step correctly starts *complete* on a bare
    deployment (the demo controls all have owners) and then correctly
    flips back to incomplete once starter-control generation adds new,
    deliberately-unowned controls
    (`test_assign_owners_step_is_false_once_an_unowned_control_exists`,
    and demonstrated end-to-end in the UAT scenario). This is the
    checklist correctly surfacing new real work, not a bug.

## Accepted, low-severity concurrency note (not fixed, matches existing risk tolerance)

Neither `define_or_revise_scope` nor `generate_starter_controls_for_framework`
uses an idempotency key — a double-submitted form (double-click, or two
concurrent admin sessions) could append two `ComplianceScopeRevised`
events instead of one, or create two starter controls for the same
uncovered requirement. Both are non-destructive, low-frequency, admin-only
actions with the same risk profile `app/control_occurrences.py::record_occurrence_manually`
already accepts for the same reason (no natural deterministic key for a
user-directed single action) — not fixed here, consistent with the
project's existing tolerance for this exact class of race on similarly
low-stakes bootstrap actions.

## Test strategy and results

- **Unit** (`tests/test_compliance_scope.py`, 8 tests): define/revise,
  TSC allow-list validation (unknown category rejected, "security" always
  included even with no selection), audit-period ordering validation,
  singleton revision behavior, historical event payloads unchanged by a
  later revision, projection rebuild.
- **Unit** (`tests/test_starter_controls.py`, 4 tests): one control per
  uncovered requirement, idempotent second call, correctly leaves
  pre-existing mappings (demo seed) untouched and only fills the real
  gap, audit event recorded per created control.
- **Unit** (`tests/test_onboarding.py`, 8 tests): every step's
  completion boundary, run against the real ambient demo-seeded state
  described above rather than an assumed-empty database.
- **Routes/regression** (`tests/test_onboarding_routes.py`, 14 tests):
  checklist/scope-form round trip, invalid-period rejection, RBAC
  (reader/auditor 403, no state change), CSRF (missing/wrong token),
  starter-control generation + idempotency over HTTP, `in_scope` toggle
  on the existing People/VendorSystem edit routes (+ RBAC).
- **Migration** (`tests/test_compliance_scope_migration.py`): SQLite (4
  tests, including a real backfill proof — pre-existing person/vendor
  rows inserted before the migration runs, confirmed `in_scope=False`
  after); PostgreSQL-gated (1 test, skipped locally, runs in CI).
- **Headless UAT (required)** — `tests/uat/test_onboarding.py`: full
  fresh-deployment journey (dashboard banner → define scope → mark a
  person in-scope → generate starter controls → start an operating
  period → confirm the checklist reflects each real change, including
  the "assign owners" regression noted above); reader-403 check.
  `GRC_UAT_MODE=1 pytest -m uat tests/uat` — **10 passed, 1 skipped**
  (Postgres-gated, pre-existing).
- **Full regression suite**: `pytest` — **722 passed, 5 skipped**
  (Postgres-gated), **11 deselected** (`uat` marker), **2 xfailed**
  (issue #65, unrelated/pre-existing), **0 failed**. Delta from the
  pre-#30 baseline (684/4/9/2) is exactly this issue's 38 new passing
  tests + 1 new Postgres-gated skip + 2 new UAT scenarios.
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  confirmed reader/auditor cannot mutate scope or trigger generation
  (tested); confirmed `DomainEvent` immutability holds for the new
  `"compliance_scope"` aggregate (inherited from `app/events.py`, not
  re-implemented); confirmed the migration's `in_scope` backfill cannot
  fabricate a "true" fact (defaults to `False`, tested against a
  pre-existing row); confirmed no secrets/PII exposure beyond fields
  already present on `Person`/`VendorSystem`; confirmed free-text scope
  fields render through Jinja2's default autoescaping (no `|safe`, no
  XSS surface); confirmed no scope expansion beyond the design doc's
  explicit boundaries.

## Known deferred/untested paths

- No starter *policy* content catalog — PRD §5.9 step 6 is not
  implemented (no existing content pipeline to build on).
- Trust Services Category selection does not filter starter-control
  generation — no per-requirement category data exists to filter by.
- No "repositories/cloud environments" relational model — recorded as
  free text only on the scope record.
- The double-submit race noted above (accepted, not fixed).
- PostgreSQL migration test was not run against a live PostgreSQL server
  in this sandbox; will run in CI's `test-postgres` job.
