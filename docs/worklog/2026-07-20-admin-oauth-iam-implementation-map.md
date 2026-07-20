# Admin/OAuth/IAM/Connections consolidation — implementation map

**Date:** 2026-07-20
**Author:** agent (claude)
**Type:** docs (planning; kicks off issue #7)

## Summary

Issue #7 asks for a Metabase-inspired Admin area, Google OAuth as the only
SSO path, IAM relocated into Admin, and a consolidated Connections
experience. This entry inventories what already exists on `main` (a lot),
what's a straight relocation, and what's a genuine gap, before any code
changes land on this branch.

## What already exists (reuse, don't rebuild)

- **Google OAuth login** — `app/google_oidc.py` + `app/routers/google_oidc.py`
  already implement the Authorization Code flow via the maintained
  `google-auth` library: signature/issuer/audience/expiry via
  `google.oauth2.id_token.verify_oauth2_token`, plus our own state/nonce
  (replay protection), `email_verified`, and `hd` domain-allowlist checks.
  Local email/password login is the existing break-glass path (ADR #16).
- **Admin authorization** — `app/deps.py::require_admin` (binary
  `User.role` — ADR #12), already gating Connections, AWS connector,
  Google Drive connect, Policies' Drive-linking actions, People CSV
  linking, Trust Center admin, and vendor destructive ops.
- **Connections (external DB connections)** — `app/routers/connections.py`
  is already a Metabase-style index/add/edit/test/delete flow: register-
  grid list, admin-only, write-only credentials via `app/secrets.py`
  (`create_encrypted_secret`), sanitized test errors
  (`app/connections.py::_sanitized_error`), audited every mutation.
- **Credential encryption** — `app/crypto.py` (Fernet, `GRC_ENCRYPTION_KEY`)
  + `app/secrets.py`'s `Secret` model (encrypted or env-ref), already used
  by DB connections, Google Drive refresh tokens, and AWS `external_id`.
- **Jobs** — `app/jobs.py` DB-backed queue (claim/run/retry, stale-reclaim),
  already used by connection tests and Drive sync; audited on enqueue.
- **Register-grid** — `app/registers/` (`RegisterConfig`/`FieldSpec`,
  `build_register_router`) already provides list/create/edit/delete/bulk
  with per-action admin gating — the shared CRUD/table infrastructure
  `CLAUDE.md` requires reusing.
- **Audit log** — `app/audit.py::record_audit_event`, `/audit-log` page,
  already login-gated (not currently admin-only — see gap below).

## Genuine gaps (new work)

1. **No admin-facing user management UI at all.** Only
   `python -m app.cli create-user`/`promote-admin`. Issue #7's
   People > Users/Roles needs a real Admin screen: list, view, change
   role, enable/disable, approve pending signups.
2. **`User` has no status/lifecycle field.** Only `role` (`user`/`admin`).
   Need `status` (`active`/`disabled`/`pending`) to support disabled and
   unapproved-user handling — a migration.
3. **No stable Google subject persisted.** `google_oidc.py` verifies
   `sub` but the callback only ever matches by email; a changed email or
   an identity collision can't be detected. Need `User.google_subject`
   (nullable, unique) — a migration — and collision/link logic in the
   callback.
4. **Google OAuth config is env-var only**, not admin-editable, not
   using the `Secret`/encryption mechanism, and has no explicit
   auto-provision toggle — first Google sign-in for any allowed domain
   silently creates a user today. Issue #7 wants this in Admin, secrets
   write-only after save, and an explicit "auto-create on first login"
   policy switch. Needs a small DB-backed settings model (same shape as
   `AwsConnection` — plain config updated in place, not append-only).
5. **No dedicated Admin area/IA.** Nav is one flat list
   (`app/main.py::NAV_ITEMS`); Connections/Audit Log/Jobs have no common
   shell, breadcrumbs, or grouping.
6. **No standalone Jobs list page.** Job history is only visible
   embedded in connection/connector detail views.
7. **Connections index only covers external DB connections.** AWS and
   Google Drive are separate, differently-styled pages
   (`connectors/aws.html`, `connectors/google_drive.html`) outside the
   Metabase-style index. Issue #7 wants one Connections index covering
   all integration types. Plan: add a thin presentation layer that lists
   DB connections + AWS + Google Drive as cards in one index (reusing
   each type's existing add/edit/test routes) — not a new connector SDK.

## Proposed vertical slices (small, independently shippable)

1. `User.status` + `User.google_subject` migration (schema only, no
   behavior change yet).
2. Admin shell: `/admin` landing page, admin-only nav section, shared
   breadcrumb/page-header partial reused by every Admin screen.
3. Admin > People > Users: list/detail/edit (role, status) — new.
4. Move Audit Log under `/admin/audit-log` (make it admin-only, matching
   ADR #12's credential/admin-surface framing) with a redirect from the
   legacy path.
5. Admin > Jobs: a standalone list/detail page over the existing `Job`
   model.
6. Google OAuth Admin config model + `/admin/authentication/google` page
   (write-only secret, explicit enabled + auto-provision toggles,
   allowed-domains editor).
7. Google OAuth callback hardening: persist `google_subject`, enforce
   `status`, enforce the new auto-provision policy, handle collisions/
   email changes, audit every branch distinctly.
8. Break-glass documentation + a regression test proving local login
   still works when Google OAuth is misconfigured/disabled.
9. Connections index consolidation: one `/admin/connections` index
   listing DB connections + AWS + Google Drive as cards; redirect legacy
   `/connections`, `/connectors/aws`, `/connectors/google-drive` index
   paths into it (detail/edit stay on their existing type-specific
   routes).
10. UI consistency pass over the touched Admin pages (status badges,
    empty states, destructive-action confirmation) — no unrelated page
    migrations.
11. Credential-leak regression tests + full test/lint/migration pass.
12. Docs (architecture.md, product-scope.md) + final worklog entry.

## Explicitly out of scope (per issue #7)

SAML, generic OIDC providers, SCIM, multiple social login providers, a
generic connector SDK, a policy/RBAC engine beyond the existing binary
role, an SPA rewrite.
