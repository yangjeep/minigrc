# Feature 3: framework requirements checklist migrated to grid

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Third phase of the platform pivot (umbrella issue #5, PR #6). Extends the
register framework for the nested case (`FrameworkRequirement` +
`RequirementAssessment`) and migrates the frameworks/{id} requirements
table onto it. Assessment fields (applicable/status/owner) stay read-only
in the grid — marking a requirement not applicable requires a note in the
same submission, which a spreadsheet cell can't enforce, so that workflow
stays on the existing requirement detail page.

## Files Changed

- `docs/superpowers/specs/2026-07-20-feature3-requirements-grid-design.md`
- `app/registers/config.py` — `scope_field`, `create_fn`,
  `creatable`/`deletable`/`bulk_enabled`.
- `app/registers/router.py` — scoped list filtering, conditional route
  mounting, generic `IntegrityError` → 422.
- `app/routers/frameworks.py` — `REQUIREMENTS_REGISTER_CONFIG`, simplified
  `view_framework` (dead server-side filter params removed).
- `app/templates/frameworks/detail.html` — grid replaces the requirements
  table and the now-redundant server-side filter form.
- `app/static/js/register-grid.js` — `listUrl` (scoped fetch, separate
  from the clean `apiBase` used for row mutations), `deletable: false`
  support.
- `tests/test_requirements_register_api.py` — 8 new contract tests.
- `tests/test_requirements.py` — 6 assertions repointed from
  server-rendered HTML to the register JSON API (the grid fetches data
  client-side, so it's no longer in the initial page response).

## Verification

- [x] Tests pass (`pytest` — 232 passed)
- [x] Lint/format clean
- [x] Manually verified in a real browser: grid loads seeded requirements
      with header filters; inline title edit via `PATCH` persists across
      reload (confirmed via `fetch` in the page context, since the grid
      column truncates long text visually); no delete/add-row affordances
      render (matches `deletable=False`/`creatable=False`); reference-code
      links still route to the existing requirement detail page.

## Decisions & Alternatives Rejected

- Assessment fields (`applicable`/`implementation_state`/`owner`) exposed
  as **read-only computed fields**, not editable — the "not applicable
  requires a note" business rule
  (`app/routers/frameworks.py::update_assessment`) can't be expressed as
  an independent cell edit without either bypassing the rule or adding
  significant per-cell workflow complexity to the generic grid. Deferred,
  not solved with a hack.
- `creatable=False`/`deletable=False` rather than mounting those routes
  and permission-gating them — a 405 on an unmounted route is more
  honest than a 403 on a route that exists but nobody should ever call;
  also avoids inventing a delete/undo story for requirements (which have
  notes, control mappings, and audit history) not asked for in this
  feature.
- `create_fn` override added to the generic config so Requirements'
  future creation path (not wired yet, since `creatable=False`) can reuse
  `app/requirements.py::add_requirement` directly rather than
  reimplementing requirement+assessment creation a second way.

## Known Gaps / Follow-ups

- Bulk edit (`bulk_enabled=True` by default) works for catalogue fields
  but wasn't separately exercised in the browser pass — covered by the
  Feature 2 bulk contract tests against the same generic code path.
- Requirements creation/deletion via the grid remains out of scope —
  manual-add-form and CSV import are still the only entry points.
