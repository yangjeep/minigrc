# Feature 2: reusable register grid framework + Controls migration

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Second phase of the platform pivot (umbrella issue #5, PR #6). Adds a
generic spreadsheet-style register framework (`app/registers/`, Tabulator
frontend) and migrates Controls onto it as the reference implementation —
Controls previously had no create/edit/delete UI at all, so this is a net
new capability, not a reskin.

## Files Changed

- `docs/superpowers/specs/2026-07-20-feature2-register-grid-design.md` —
  design spec.
- `app/registers/config.py`, `app/registers/router.py` — generic
  `RegisterConfig`/`FieldSpec` and `build_register_router` JSON API
  factory (list/create/patch/delete/bulk, optimistic concurrency via
  `expected_updated_at`, all-or-nothing bulk, audit logging).
- `app/deps.py` — added `verify_csrf_header` for JSON endpoints.
- `app/main.py` — JSON error branch (`/api/` paths) in the global
  exception handler instead of the HTML error page; mounts
  `controls_register_router`.
- `app/routers/controls.py` — `CONTROLS_REGISTER_CONFIG`, simplified
  `list_controls` (grid now fetches data itself).
- `app/static/vendor/tabulator-6.3.1/` — vendored, no CDN.
- `app/static/js/register-grid.js` — generic Tabulator wrapper.
- `app/templates/base.html` — added an opt-in `{% block scripts %}`.
- `app/templates/controls/list.html` — grid container + column config.
- `tests/test_register_api.py` — 15 contract tests for the generic API.

## Verification

- [x] Tests pass (`pytest` — 224 passed)
- [x] Lint/format clean (`ruff check .`, `ruff format --check .`)
- [x] Manually verified in a real browser (Chrome DevTools Protocol,
      isolated scratch data dir): grid loads seeded controls sorted by
      name; "Add control" creates a row via `POST`; double-click inline
      edit on a text cell saves via `PATCH` and persists across a full
      page reload; delete removes a row via `DELETE` after a confirm
      dialog (handled via the DevTools dialog API, not the extension
      bridge — see decision below); keyboard Tab navigation works.
      409/422 conflict and validation paths are covered by the backend
      contract tests rather than re-verified in-browser (same code path).

## Decisions & Alternatives Rejected

- Tabulator over AG Grid Community — see the design spec; no paid tier
  at all vs. AG Grid's one-flag-away Enterprise tier.
- No JS test runner introduced (no npm/node build step added) — frontend
  behavior covered by backend API contract tests + browser verification,
  consistent with the "no frontend build step" invariant from Feature 1.
- Global exception handler gained a `/api/` JSON branch rather than a
  Pydantic-model response schema — keeps the register router's error
  shapes simple (`dict`/`HTTPException.detail`) without adding a second
  serialization layer for what's currently one entity.
- Used the Chrome DevTools MCP plugin (not the `claude-in-chrome`
  extension) for delete-button verification, since it has a `handle_dialog`
  tool that safely intercepts the confirm() prompt — the extension bridge
  has no such mechanism and triggering an unhandled dialog there would
  block the session.

## Known Gaps / Follow-ups

- Server-side pagination not implemented (not needed at this data scale).
- Saved views/filters deferred — Tabulator's client-side filter/sort
  covers the immediate need.
- Framework requirements (nested case) and Risks/Assets migrate in
  Features 3 and 4, reusing this same framework.
