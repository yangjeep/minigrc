# Phase 1: ADR-23 architecture pivot + Bootstrap application shell

**Date:** 2026-07-19
**Author:** Claude (agent)
**Type:** feat

## Summary

First phase of a larger, user-approved architecture pivot (spreadsheet
registers, Postgres, external DB connectors, worker, Kubernetes/Helm,
Trust Center). This phase records the pivot as ADR-23 and replaces the
hand-rolled sidebar layout with a vendored Bootstrap 5.3 shell
(responsive navbar + offcanvas sidebar), touching only `base.html` and
shared partials — no router or content-block changes.

## Files Changed

- `docs/decisions/architectural-decisions.md` — added ADR #23.
- `docs/product-scope.md` — cross-referenced the pivot.
- `docs/superpowers/specs/2026-07-19-phase1-bootstrap-shell-design.md` —
  design spec written before implementation.
- `app/static/vendor/bootstrap-5.3.3/`, `app/static/vendor/bootstrap-icons-1.11.3/`
  — vendored assets, no CDN.
- `app/templates/base.html` — Bootstrap navbar + offcanvas sidebar,
  Bootstrap-alert flash messages, skip-to-content link.
- `app/templates/_states.html` — new opt-in `empty_state`/`error_alert`
  macros, not yet adopted by any template.
- `app/static/theme.css` — new, Bootstrap CSS variable overrides + shell
  layout classes.
- `app/static/style.css` — removed shell-only rules (`.shell`,
  `.sidebar*`, `.content`, `.brand`, `.link-button`) now superseded by
  Bootstrap; kept everything content templates still use (badges,
  tables, forms, stat cards, flash, notice, placeholder-card).
- `tests/test_pages.py` — added shell-markup coverage across 6 nav
  routes and vendored-asset-existence checks.

## Verification

- [x] Tests pass (`pytest` — 200 passed)
- [x] Lint/format clean (`ruff check .`, `ruff format --check .`)
- [x] Manually verified: ran the app against an isolated scratch data
      dir (never touched `./data`), logged in as the seeded admin user,
      and checked both breakpoints in a real browser (Chrome DevTools
      Protocol, viewport-emulated — the extension-based browser tool
      was flaky mid-session and a second tool was used instead):
      - Desktop (1440px): sidebar permanently visible, active nav
        state, dismissible Bootstrap alert for the sign-in flash.
      - Mobile (390px): navbar collapses to a hamburger; offcanvas
        opens as a focus-trapped modal dialog (`role="dialog" modal`);
        skip-to-content link present; full login → dashboard →
        logout round trip confirmed via the audit log.
      - Found and fixed a real bug during this pass: `flex-grow-1` on
        the primary nav pushed the account/logout dropdown off-screen
        on mobile with 12 nav items — fixed by removing it (separate
        commit).

## Decisions & Alternatives Rejected

- Vendored Bootstrap assets instead of a CDN, to preserve the
  self-hosted/offline-friendly deployment model this app already
  commits to (see ADR #23's rationale).
- Left `style.css` in place (trimmed, not deleted) since most of its
  rules are still used directly by content templates this phase
  deliberately doesn't touch (grid migration phase will retire more of
  it as it restyles Frameworks/Controls/Risks tables).
- `resize_window` on the `claude-in-chrome` extension tool did not
  actually change the tab's viewport in this session (confirmed via
  `window.innerWidth`); switched to the Chrome DevTools MCP plugin's
  `emulate` tool (CDP device-metrics override), which worked correctly,
  for all responsive verification.

## Known Gaps / Follow-ups

- Per-page tables/forms/stat-rows are not yet Bootstrap-restyled and
  can overflow horizontally on narrow viewports (visible on the mobile
  dashboard screenshot) — explicitly deferred to the grid-migration
  phase per the design spec's non-goals.
- `_states.html` macros are defined but not yet adopted by any
  template; adoption is opportunistic in later phases.
- This is phase 1 of 12+ planned phases (grid framework, Postgres,
  external DB connectors, worker, imports, Kubernetes/Helm, Trust
  Center, final security/a11y pass) — see the design spec and PR
  description for the full sequence.
