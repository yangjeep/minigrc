# Feature 2: reusable spreadsheet-style register framework + Controls reference migration

**Date:** 2026-07-20
**Status:** Approved by user, ready for implementation
**Part of:** miniGRC platform pivot (umbrella issue #5, PR #6)

## Context

Feature 1 (Bootstrap shell) is done and merged into the long-lived PR. This
phase builds the reusable spreadsheet-style register framework named in
ADR-23 and, per user decision, migrates Controls as the reference
implementation (Controls today has no create/edit/delete route at all —
this is a net-new capability, not just a reskin).

## Library choice

**Tabulator** (MIT, no paid tier at all), vendored under
`app/static/vendor/tabulator-6.x/`, no CDN, no npm/node build step —
consistent with Bootstrap's vendoring policy and the repo's "no frontend
build step" invariant. Rejected AG Grid Community: functionally
comparable for this app's needs, but its Enterprise tier sits one config
flag away, which is a bigger footgun for "no paid runtime dependencies
unless explicitly approved" than Tabulator (a single open-source project
with no paid edition at all). Both are vanilla-JS-usable without an SPA
conversion.

## Backend: generic register layer

New `app/registers/` package:

- `app/registers/config.py` — `RegisterConfig` dataclass: `model` (SQLAlchemy
  class), `entity_type` (str, for audit), `fields` (ordered list of
  `FieldSpec`: name, type [text/number/date/bool/enum], required,
  choices/max_length as applicable), `order_by`, `require_admin_for`
  (subset of `{"create", "edit", "delete"}`, default empty — Controls
  needs none, matching decision #8/#12: ordinary GRC data stays available
  to any logged-in user).
- `app/registers/router.py` — `build_register_router(config) ->
  APIRouter`, mounted at `/api/registers/<name>`:
  - `GET /` — list all rows as JSON (no pagination yet — dataset scale
    doesn't need it; the config shape leaves a clean seam for a
    server-side row model later without an API-shape change).
  - `POST /` — create a row; validates required/enum/max_length fields
    server-side regardless of client; 422 with `{field: [errors]}` on
    failure.
  - `PATCH /{id}` — partial update. Body includes `expected_updated_at`
    (ISO string echoed back from the last `GET`/mutation response); if it
    doesn't match the row's current `updated_at`, respond 409 with the
    current row so the client can show a conflict instead of silently
    overwriting a concurrent edit.
  - `DELETE /{id}`.
  - `POST /bulk` — array of `{id, fields, expected_updated_at}`; single
    transaction, all-or-nothing (simplest correct semantics — a partial-
    success UI is unnecessary complexity for this app's scale).
  - Every mutation writes an `AuditEvent` via the existing
    `app/audit.py::record_audit_event`, using `config.entity_type`.
- `app/deps.py::verify_csrf_header` — new dependency, reads
  `X-CSRF-Token` header instead of the `csrf_token` form field, reusing
  `csrf_tokens_match`. The existing form-based `verify_csrf` is untouched
  (still used by every non-grid POST form).

## Frontend: generic grid wrapper

- `app/static/js/register-grid.js` — given a container element + a small
  inline JSON config (columns, API base path, CSRF token from
  `csrf_token(request)`), initializes Tabulator with an ajax data source
  pointed at the register's `GET /`. Wires:
  - cell edit → `PATCH /{id}` with `expected_updated_at`; on 409/422,
    reverts the cell and shows a Bootstrap alert with the server's
    message.
  - "Add row" button → `POST /`.
  - row delete action → `DELETE /{id}` behind a confirm.
  - multi-row selection + "Apply to selected" → `POST /bulk`.
  - Loading state: Bootstrap spinner while the initial `GET` is in
    flight. Empty state: reuses `_states.html`'s `empty_state()` macro
    text, rendered by Tabulator's placeholder option.

## Reference migration: Controls

- `app/templates/controls/list.html` becomes a grid container
  (`<div id="controls-grid">`) + a small inline `<script>` block
  configuring columns: name (text), owner (text), status (enum select:
  `CONTROL_STATUSES`), review_frequency (enum select:
  `REVIEW_FREQUENCIES`), description (text, longer editor), mapped
  requirements count (read-only, links to `/controls/{id}`).
- `app/routers/controls.py` gains
  `include_router(build_register_router(CONTROLS_REGISTER_CONFIG))`
  mounted under the existing `/controls` prefix's app-level registration
  — the existing `GET /controls` (page), `GET /controls/{id}` (detail +
  mapping UI), and `POST /controls/{id}/mappings` routes are unchanged.
- No admin gating on Controls CRUD — matches existing authorization
  decisions.

## Testing

- Backend: `tests/test_register_api.py` — generic-framework contract
  tests using the Controls registration as the concrete case: list,
  create (valid/invalid), patch (valid/enum-violation/conflict), delete,
  bulk (success/one-bad-row-rolls-back-all), permission (any logged-in
  user can do all four actions), audit event written for each mutation
  type.
- No JS test runner introduced — keeps the "no frontend build step"
  invariant from Feature 1. Frontend behavior is covered by the backend
  contract tests above plus browser-driven verification (Chrome
  DevTools): inline cell edit, add row, delete row, a forced save
  failure (edit two tabs to the same row, confirm 409 surfaces), keyboard
  navigation between cells, screenshot for the PR.

## Commit sequence

1. `test: define register grid API contract` (failing tests against the
   not-yet-built generic layer)
2. `feat: add generic register config and JSON API`
3. `feat: vendor tabulator`
4. `feat: add register-grid.js client wrapper`
5. `feat: migrate controls list to spreadsheet grid`
6. `fix: ...` (any issues found in browser verification)

## Known gaps / follow-ups

- Saved views/filters (mentioned as "if reasonable" in the original ask)
  are deferred — Tabulator's client-side filter/sort UI covers the
  immediate need without persisting view state server-side.
- Server-side pagination is not implemented (not needed at this data
  scale) but the API shape (`GET /` returning a flat list) doesn't block
  adding it later.
- Framework requirements/assessments (a nested, harder case — one grid
  row combines `FrameworkRequirement` + `RequirementAssessment` fields
  across two tables) and Risks/Assets migrate in Features 3 and 4,
  reusing this same `RegisterConfig`/`build_register_router` pattern.
