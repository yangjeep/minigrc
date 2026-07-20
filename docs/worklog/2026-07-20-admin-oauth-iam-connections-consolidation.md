# Metabase-inspired Admin, Google OAuth, IAM, and Connections consolidation

**Date:** 2026-07-20
**Author:** agent (claude)
**Type:** feat

## Summary

Implements issue #7: Google OAuth is now the only first-class SSO path
(local email/password remains as break-glass), a dedicated Admin area
exists with server-side authorization on every route, IAM (user
management) moved into Admin (previously CLI-only), and Connections
(external DB connections, AWS, Google Drive) are unified into one
Metabase-style index. Nothing about the existing GRC domain model
changed.

## Files Changed

- `app/models.py` — `User.status`/`User.google_subject`, new
  `GoogleOidcSettings` model.
- `migrations/versions/8961da81a764_*.py`, `b8373eb767c4_*.py` — the
  above schema changes.
- `app/deps.py`, `app/routers/auth.py` — reject disabled/pending users
  at login and on every subsequent request.
- `app/routers/admin.py`, `admin_users.py`, `admin_jobs.py`,
  `admin_authentication.py`, `admin_connections.py` — the new Admin
  area (landing, Users, Jobs, Google OAuth config, unified Connections
  index), each `templates/admin/...` — all admin-gated.
- `app/routers/audit_log.py` — relocated to `/admin/audit-log`
  (previously open to any logged-in user); legacy path redirects.
- `app/routers/connections.py`, `app/routers/placeholders.py` — removed
  the now-superseded bare list route and the "Connectors" placeholder.
- `app/google_oidc_config.py` — resolves DB-backed (Admin UI) or
  legacy env-var Google OAuth config; any resolution failure degrades
  to "not usable" rather than raising.
- `app/routers/google_oidc.py` — callback hardening: match by stable
  `google_subject` first (survives an email change), fall back to
  email (links an existing user), reject a different subject claiming
  an already-linked email as a collision; first-login policy creates
  either an active user (auto-provision on) or a `pending` one
  requiring admin approval (auto-provision off).
- `app/templates/base.html`, `_page_header.html`,
  `dashboard.html`, `risks/list.html` — Admin nav link (admin-only),
  shared page-header/breadcrumb macro, applied to two representative
  pages.
- `docs/architecture.md`, `docs/product-scope.md`,
  `docs/deployment/authentication.md` — updated/new.

## Verification

- [x] Tests pass (`pytest` — 382 passed, 1 skipped)
- [x] Lint/format clean (`ruff check .`, `ruff format --check .`)
- [x] Both new migrations verified upgrade → downgrade → downgrade →
      upgrade on a scratch SQLite DB
- [x] Live-server (uvicorn) verification: logged in, hit every new
      Admin route (200s), created a real external DB connection
      end-to-end (secret encrypted, card renders, no leak), saved
      Google OAuth config end-to-end (secret write-only, status badge
      correct), confirmed anonymous/non-admin access is blocked
- [x] Credential-leak regression tests (AWS `external_id`, Google
      Drive refresh token, Google OAuth client secret, unified
      Connections index)
- [ ] Live browser screenshot verification — blocked by a
      `chrome-extension://` tool error in this session's browser
      automation environment (reproduced across 2 tabs and a bare
      keypress); substituted with HTTP-level verification against the
      real running server (see above) plus the existing responsive
      markup (Bootstrap offcanvas sidebar, viewport meta) confirmed
      present in rendered output

## Key decisions

- `User.status` is three-valued (`active`/`disabled`/`pending`), not
  binary — `pending` is the concrete mechanism behind "administrator-
  approved account" in the first-login policy: a first-time Google
  sign-in with auto-provision off creates a real, visible-to-admins
  pending row rather than silently rejecting or requiring pre-CLI
  provisioning.
- Connections consolidation is a presentation layer over three
  existing, structurally different models (`ExternalConnection`,
  `AwsConnection`, `GoogleDriveConnection`) — no generic connector SDK,
  no new connector types (GitHub/Azure/Asana/Aikido) built, per
  explicit non-goal in the issue.
- Audit Log moved from "any logged-in user" to admin-only, matching
  the issue's IA and ADR #12's credential/admin-surface framing — a
  deliberate tightening, not an oversight.

## Known limitations / follow-ups

- AWS and Google Drive connections don't yet have an enable/disable
  toggle or per-connection job history (only `ExternalConnection`
  does) — the unified index surfaces what each type already exposes
  rather than adding new columns to every model for this pass.
- The UI-consistency pass (shared page-header/breadcrumb) covers the
  Admin area plus Dashboard and Risks as a representative sample, not
  every existing GRC page — the pattern is established and reusable
  for a follow-up pass.
- `app/routers/connections.py::connections_register_router`'s list
  endpoint (`/api/registers/connections`) has no remaining UI consumer
  after the unified index replaced Tabulator-based rendering there;
  left in place (admin-gated, no leak) rather than removed, to avoid
  unrelated churn in an already-large diff.
