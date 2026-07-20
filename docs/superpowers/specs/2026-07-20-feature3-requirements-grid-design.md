# Feature 3: migrate framework requirements checklist onto the register grid

**Date:** 2026-07-20
**Status:** Approved, ready for implementation
**Part of:** miniGRC platform pivot (umbrella issue #5, PR #6)

## Context

Feature 2 built the generic register framework and proved it with Controls
(a flat, single-table entity). Framework requirements are the harder,
nested case: one grid row combines `FrameworkRequirement` (the catalogue
entry) with its 1:1 `RequirementAssessment` (the org's assessment of it).
Marking a requirement "not applicable" requires a note in the same
submission (`app/routers/frameworks.py::update_assessment`) — a rule a
spreadsheet cell edit can't enforce cleanly, so assessment fields stay
**read-only** in the grid; the existing detail-page form remains the only
way to change applicable/state/owner.

## Generic framework extensions

Four small additions to `app/registers/config.py`/`router.py`, all
backward-compatible defaults so Controls is unaffected:

- `RegisterConfig.scope_field: str | None = None` — when set, `GET`
  requires and filters by `?<scope_field>=<value>` in the query string;
  `POST` requires the same key in the payload.
- `RegisterConfig.create_fn: Callable[[Session, dict], Any] | None = None`
  — when set, `create_row` calls this instead of
  `config.model(**fields)`. Requirements uses it to call the existing
  `app/requirements.py::add_requirement` helper — the docstring there
  says "three call sites (seed, manual add, CSV import) can't drift out
  of sync"; the grid becomes the fourth caller of that same helper, not
  a fourth reimplementation.
- `RegisterConfig.creatable`/`deletable`/`bulk_enabled: bool = True` —
  when `False`, `build_register_router` doesn't mount that route at all
  (404, not a permission error). Requirements sets `creatable=False`
  (creation stays on the existing manual-add-form + CSV-import entry
  points, which already own the uniqueness UX) and `deletable=False` (no
  delete route or business process exists for a requirement today —
  notes, control mappings, and audit history make deletion a bigger,
  separate decision, deliberately out of scope here).
- `create_row` gets a generic `try/except IntegrityError` → 422 — needed
  for the `(framework_id, reference_code)` uniqueness constraint;
  harmless addition for Controls (no unique constraint there today).

## Migration

- `app/routers/frameworks.py::view_framework` — requirements table on
  `frameworks/{id}` becomes a grid container. Existing search/applicable/
  state filter form, "+ Add one manually" link, and CSV import form are
  unchanged (still the only ways to create/bulk-import requirements this
  feature).
- Grid columns: reference_code (editable, links to
  `/frameworks/{id}/requirements/{req_id}`), title (editable), summary
  (editable), display_order (editable, number), applicable/status/owner
  (read-only, computed from `requirement.assessment`).
- `REQUIREMENTS_REGISTER_CONFIG`: `model=FrameworkRequirement`,
  `scope_field="framework_id"`, `creatable=False`, `deletable=False`,
  `bulk_enabled=True` (bulk-editing catalogue fields like display_order
  across selected rows is a real use case; assessment fields aren't
  exposed so there's no note-requiring conflict in bulk either).

## Testing

- `tests/test_register_api.py` (or a new `tests/test_requirements_register_api.py`)
  extends the contract-test pattern: scoped list (wrong/missing
  `framework_id` → 400), patch catalogue fields, duplicate reference_code
  → 422, assessment fields present but read-only (PATCH attempt on
  `applicable` is silently ignored — not in `config.fields` as editable),
  delete route absent (404), create route absent (404), audit event
  written on patch.
- Browser verification: load `frameworks/{id}`, inline-edit a
  reference_code/title, confirm existing filter form and CSV import still
  work, confirm no delete/add-row affordance appears for this grid.

## Commit sequence

1. `test: define scoped/read-only-field register API contract`
2. `feat: extend register framework with scope_field/create_fn/route toggles`
3. `feat: migrate framework requirements checklist to grid`
4. `fix: ...` (browser-verification fixups, if any)
