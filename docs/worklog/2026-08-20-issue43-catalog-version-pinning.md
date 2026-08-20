# Issue #43: organization-level framework/catalog version pinning

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature/architecture

## Summary

Defines what happens when a framework catalog family (SOC 2, ISO 27001)
is later superseded by a new version, while an organization already has
an active program built against an older one. See
`docs/superpowers/specs/2026-08-20-issue43-catalog-version-pinning-design.md`
for the full design, including the repository-reality check that shows
most of the issue's stated worry ("an upgrade silently reinterpreting a
past period") cannot actually happen through existing mapping data —
`reconcile_system_catalogs` never mutates an existing `Framework`/
`FrameworkRequirement` row, so a `ControlRequirementMapping`'s FK never
repoints itself.

## What was actually missing

There was no explicit, queryable fact anywhere for "which catalog
version is the organization's active program currently operating under,
for a given framework family" — only implicit inference from which
mappings happened to exist. An "upgrade" action had nothing to act on.

## What changed

- `Framework.catalog_family` (new nullable column) — distinguishes
  "alternate versions of the same catalog" from "unrelated framework,"
  separate from the existing `catalog_key` (which conflates family +
  version into one opaque string). Set by `app/framework_catalog.py` for
  every `SYSTEM_CATALOGS` entry (`"iso27001"`, `"soc2"`); `NULL` for
  user-created custom frameworks.
- `FrameworkAdoption` (new event-sourced aggregate,
  `app/framework_adoption.py`) — one row per `catalog_family`: which
  `Framework.id` is currently adopted, and the one it was upgraded from,
  if any. `adopt_framework`/`upgrade_framework_adoption` are deliberately
  distinct actions with distinct audit trails — upgrading a family
  already adopted to something else must go through the upgrade path,
  never a second "first adoption."
- `reconcile_system_catalogs` now auto-adopts a catalog family's
  first-ever version at bootstrap (only when no adoption exists yet for
  that family) — the adoption mechanism reflects reality without a
  separate onboarding step, and never touches an adoption automatically
  once one exists (a version change past that point is always the
  explicit upgrade action).
- `migrations/versions/7ea3c156037f_...py` — additive `catalog_family`
  column + `framework_adoptions` table, with a deterministic backfill
  (explicit `catalog_key -> catalog_family` mapping, not a parsing
  heuristic) that also retroactively auto-adopts any already-reconciled
  system catalog framework, mirroring exactly what the app does for a
  fresh install.
- `app/routers/frameworks.py` + `app/templates/frameworks/adoptions.html`
  — `GET /frameworks/adoptions` (adoption status per family + upgrade
  affordance when a newer sibling version exists) and
  `POST /frameworks/adoptions/{catalog_family}/upgrade`, gated behind
  `require_write_access` — the same authorization bar every other
  framework-mutating route in this file already uses.

## Historical-pinning proof

`tests/test_framework_adoption.py::test_upgrade_never_touches_historical_mappings_or_periods`
builds a synthetic v1 catalog, maps a control to it, creates a control
period, then upgrades adoption to a synthetic v2 — and asserts the v1
mapping/period data is byte-for-byte unchanged and the v1 `Framework` row
itself is never deleted/renamed. No real second SOC 2/ISO edition needs
to exist for this proof; the mechanism is exercised with synthetic
catalog versions since a real second edition isn't imminent yet (per the
issue's own framing).

## A real regression found and fixed before shipping

Adding a required `catalog_family` field to `SystemCatalog` broke an
existing test (`test_find_or_create_catalog_framework_recovers_from_concurrent_insert`)
that constructed a `SystemCatalog` directly without it — fixed the call
site with an explicit `catalog_family="race-test-family"` rather than
making the field optional/defaulted, since every real `SYSTEM_CATALOGS`
entry always has one.

## A real gap found via adversarial review

`list_upgrade_candidates` filters out inactive frameworks for the UI's
dropdown, but `upgrade_framework_adoption` itself did not re-check
`is_active` — a crafted POST with a hand-picked `to_framework_id` could
have upgraded to a deactivated framework, bypassing the UI's own
filtering. Fixed by validating `to_framework.is_active` inside the
service function itself, with a regression test
(`test_upgrade_framework_adoption_rejects_inactive_target`).

## Known, accepted low-severity race (not fixed)

Two near-simultaneous upgrade POSTs for the same catalog family (a
double-click, not a normal single-admin workflow) could both read the
adoption before either commits and both succeed, producing two
`FrameworkAdoptionUpgraded` events instead of one. The final projected
state is still correct either way (same `framework_id`/
`previous_framework_id` outcome), only the audit trail would show one
extra event. Same category and same accepted-without-locking precedent
as issue #47's PII-redaction race — a rare, human-triggered, admin-only,
non-destructive double-submit, not a correctness bug in the end state.

## Test strategy and results

- **Unit** (`tests/test_framework_adoption.py`, 12 tests): adopt/
  idempotent-re-adopt/reject-no-family/reject-second-adopt-same-family,
  upgrade success/reject-cross-family/reject-no-op/reject-inactive-target,
  upgrade-candidate listing, cross-family adoption listing, projection
  rebuild, and the historical-pinning proof above.
- **Browser/E2E** (`tests/test_framework_adoption_routes.py`, 7 tests):
  the adoptions page shows the two real reconciled system catalogs as
  "up to date"; adding a synthetic newer sibling version surfaces an
  upgrade control; a full upgrade POST moves the adoption while leaving
  the original framework row untouched; `require_write_access` is
  enforced (a reader gets 403); unknown catalog family and cross-family
  upgrade attempts are rejected (404 / flash error).
- **Migration**: applied to a live SQLite file simulating both a fresh
  install (no existing frameworks) and an upgrading existing deployment
  (a pre-existing `catalog_key`-only framework row) — verified the
  backfill correctly sets `catalog_family` and auto-creates the adoption
  event/row; a downgrade → upgrade ×2 cycle produced exactly one
  `FrameworkAdoptionAdopted` event and one adoption row (no duplication),
  reusing the same `aggregate_id` across repeat runs.
- **Regression**: fixed one pre-existing test call site broken by the new
  required `SystemCatalog.catalog_family` field (see above); full
  `tests/test_framework_catalog.py` suite green afterward.
- **Lint/format**: `ruff check .` / `ruff format --check .` clean.
- **SQLite/PostgreSQL**: plain SQLAlchemy Core/ORM throughout — no
  backend-specific SQL. No local PostgreSQL available in this
  environment (same limitation as every prior issue this session); CI's
  `test-postgres` job is the parity check of record.
- **No headless UAT scenario for this slice**: with exactly one version
  per system-catalog family existing today, a live deployment has no
  real upgrade target to exercise — the browser/E2E coverage above
  proves the real HTTP/auth/CSRF path more directly than a headless UAT
  scenario against a synthetic second version would. Documented as a
  deliberate scope decision, matching #42's precedent for backend
  mechanisms with only a thin admin surface.
- **Claude Desktop UAT: PENDING** — same reasoning as above; a runbook
  would only be meaningful once a real second catalog edition exists to
  click through.

## Known deferred/untested paths

- No general diff/migration engine for re-mapping controls from an old
  version's requirements to a new version's — explicit non-goal in the
  issue; upgrading only moves which version is "current," it never
  attempts to carry mappings forward.
- No UI to *browse* a catalog family's full version history beyond
  "current" and "previous" — not required by the issue's acceptance
  criteria.
