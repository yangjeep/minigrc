# Phase 1: ADR-23 (architecture pivot) + Bootstrap application shell

**Date:** 2026-07-19
**Status:** Approved by user, ready for implementation
**Part of:** miniGRC "Trust Center + platform" multi-phase initiative (13 phases; see PR description for full list)

## Context

The user requested a large expansion of miniGRC: spreadsheet-style registers
(AG Grid), Postgres, external DB connector framework, worker/job queue,
Kubernetes/Helm, Trust Center. The existing `CLAUDE.md` and
`docs/decisions/architectural-decisions.md` explicitly reject several of
these (no queues/workers/k8s at this scale, no generic connector SDK
without a second concrete use, single-tenant, boring monolith). The user
confirmed a **deliberate full architecture pivot** rather than scoping the
request down to fit the old constraints.

This phase does two things:
1. Records the pivot as ADR-23, so the contradiction with prior ADRs is
   explicit and traceable rather than silently ignored.
2. Delivers the first concrete piece of the pivot: a Bootstrap-based
   application shell, replacing the current hand-rolled `style.css`
   sidebar layout, without touching page content or backend logic.

## ADR-23: Supersede single-scale/no-distributed-systems assumptions

New entry appended to `docs/decisions/architectural-decisions.md`:

- **Decision:** miniGRC's target scale and architecture assumptions widen.
  Decision #1's framing ("every distributed-systems concern... would be
  pure overhead here") and the single-tenant scale assumption in
  `docs/product-scope.md` are superseded for the specific subsystems named
  below. Everything else in `CLAUDE.md` and prior ADRs (Alembic migrations,
  explicit audit events, session-based auth, CSRF double-submit, binary
  admin/user roles, hex-UUID ids, immutable evidence/policy snapshots)
  **remains in force** — this is a scale/deployment pivot, not an
  auth/security/data-model pivot.
- **What changes:** Postgres becomes a first-class supported database
  alongside SQLite; a worker process is introduced for background jobs
  (imports, connection tests); Kubernetes/Helm becomes a supported
  deployment target; an external-database connector interface is
  introduced for read-only evidence/inventory collection from customer
  systems; AG Grid Community replaces hand-rolled tables for register-like
  entities (Frameworks, Controls, Risks, Assets).
- **What does not change:** single organization per deployment (no
  `org_id`, no org switching), binary admin/user roles, local
  session+Argon2 auth (no JWT, no hosted IdP beyond the existing optional
  Google OIDC), SQLite remains fully supported for local dev/small
  deployments (not deprecated).
- **Rationale:** explicit user decision, recorded here so future agents
  don't "fix" the codebase back toward decision #1's original framing.

This ADR entry ships in the **first commit** of this PR, before any code
changes, so every subsequent phase can cite it instead of re-litigating the
KISS/CLAUDE.md conflict.

## Bootstrap application shell

### Goals
- Replace `app/static/style.css`'s hand-rolled sidebar layout with
  Bootstrap 5.3 + Bootstrap Icons, vendored locally (no CDN, no JS build
  step — matches the self-hosted, offline-friendly deployment model).
- Responsive: sidebar always visible ≥992px; collapses to an
  offcanvas panel behind a navbar hamburger toggle <992px.
- Zero backend/router changes. `NAV_ITEMS` in `app/main.py` is reused
  as-is. Every template's `{% block content %}` body is untouched in this
  phase — only `base.html` (the shell) and shared partials change.
- Preserve the existing minigrc visual identity (current accent color,
  spacing) via Bootstrap CSS custom-property overrides, not a full
  Bootstrap-default reskin.

### File changes

- `app/static/vendor/bootstrap-5.3.x/bootstrap.min.css`,
  `bootstrap.bundle.min.js` — vendored, committed binary-ish assets (text,
  but not hand-edited).
- `app/static/vendor/bootstrap-icons-1.x/bootstrap-icons.css` + `fonts/`
  — vendored icon font.
- `app/static/theme.css` — new, small (~40-60 lines): CSS custom property
  overrides (`--bs-primary`, `--bs-body-bg`, etc.) plus the handful of
  minigrc-specific rules that aren't expressible as Bootstrap utilities
  (e.g. sidebar width, sticky positioning tweaks).
- `app/static/style.css` — deleted once `theme.css` covers everything it
  did; anything not migrated stays until superseded in a later phase
  (e.g. table styling addressed when the grid migration lands).
- `app/templates/base.html` — rewritten:
  - Top navbar: brand, hamburger toggle (mobile only), user
    email + logout as a Bootstrap dropdown.
  - Sidebar: Bootstrap `offcanvas` component, `d-lg-block` visible
    permanently on large screens, hidden/toggleable below.
  - Flash messages rendered as Bootstrap `.alert` (kind → `alert-success` /
    `alert-danger` / `alert-warning` mapping already exists in the flash
    query param; just changes the CSS classes emitted).
  - Skip-to-content link retained/added for accessibility.
- `app/templates/_states.html` — new, small macro partial:
  `empty_state(message, icon="inbox")` and `error_alert(message)`, built
  on Bootstrap markup. Adopted by templates opportunistically, not a
  forced rewrite of all templates this phase.

### Non-goals for this phase
- No AG Grid, no Postgres, no worker, no connector, no Trust Center changes
  — those are later phases.
- No per-page content restyling beyond what `base.html`/shared partials
  already control (e.g. individual list/detail page tables keep their
  current markup until the grid migration phase touches them).

## Testing

- Extend `tests/test_pages.py`: every nav-visible route already gets hit;
  add assertions that the rendered HTML contains the offcanvas sidebar
  markup (`id="sidebarOffcanvas"` or similar), a `<nav aria-label="Primary">`
  role, and a skip link — catches shell regressions across every page in
  one place.
- New small test asserting `app/static/vendor/...` files exist (so a
  future contributor can't silently break the app by deleting vendored
  assets without CI catching it) — simple `Path.exists()` checks, not a
  full asset-integrity test.
- Manual browser verification (claude-in-chrome): load dashboard, resize
  to mobile width, open the offcanvas via hamburger, tab through nav links
  and confirm focus order/visible focus ring, confirm logout still works.
  Screenshot desktop + mobile for the PR description.

## Commit sequence for this phase

1. `docs: add ADR-23 (architecture pivot to platform/production scale)`
2. `feat: vendor bootstrap and bootstrap-icons assets`
3. `feat: rewrite base template with bootstrap shell`
4. `test: cover bootstrap shell markup across nav routes`
5. (if manual browser pass finds issues) fix-up commit(s) before moving to
   phase 2 (grid framework).

## Known gaps / follow-ups

- Per-page tables/forms still look mostly unstyled-Bootstrap until later
  phases touch them directly (grid migration phase restyles
  Frameworks/Controls/Risks; other pages get incidental Bootstrap
  component polish only when touched for another reason).
- `style.css` deletion is contingent on `theme.css` actually covering
  every rule it had — if something is missed, keep the file until it's
  fully drained rather than losing a subtle style.
