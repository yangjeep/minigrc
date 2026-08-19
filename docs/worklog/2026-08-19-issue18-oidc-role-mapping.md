# Issue #18: OIDC claim/group-to-role mapping and authorization hardening

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Extends #17's generic OIDC login with explicit, admin-configured
claim/group-to-role mapping. Fully inert until an admin adds at least
one mapping row; the bootstrap first-admin and any admin-edited role are
always locally pinned and never touched by SSO login. See
`docs/superpowers/specs/2026-08-19-issue18-oidc-role-mapping-design.md`
for the full design/reasoning.

## What changed

- `app/models.py` — `OidcRoleMapping` (claim value -> role, unique on
  claim value); `OidcProviderSettings.role_claim_name` (default
  `"groups"`); `User.role_source` (`"local"` | `"oidc_mapped"`).
  Migration `c5f8e2a71d9b`.
- `app/oidc_role_mapping.py` (new) — `extract_claim_values` (never
  guesses a delimiter; caps at 1000 entries), `compute_mapped_role`
  (highest-precedence match, else least-privileged `reader`),
  `apply_role_mapping` (no-op unless mapping configured and
  `role_source != "local"`; audits only on an actual change).
- `app/oidc.py` — `OidcIdentity` gains `claims: dict` (the full verified
  claim set), so role mapping can read a configurable claim without a
  second JWT decode.
- `app/oidc_config.py`, `app/config.py` — `role_claim_name` resolution
  (admin row, then `GRC_OIDC_ROLE_CLAIM_NAME` env fallback).
- `app/routers/oidc.py` — bootstrap first-admin stays
  `("admin", "local")` unconditionally; every other new SSO user starts
  `("operator", "oidc_mapped")`; `apply_role_mapping` runs once any
  active user is resolved, before session creation.
- `app/routers/admin_users.py` — `update_user` now always stamps
  `role_source = "local"` on save; `role_source` added as a read-only
  register-grid column.
- `app/routers/admin_authentication.py` — `role_claim_name` field added
  to the OIDC settings form; new `/admin/authentication/oidc/roles`
  GET/POST (add) and `/roles/{id}/delete` (remove) mapping CRUD.
- `app/templates/admin/authentication/oidc.html`,
  `oidc_roles.html` (new), `admin/users/edit.html` — UI for the above.

## Test strategy and results

- **Unit** (`tests/test_oidc_role_mapping.py`, 16 tests): claim
  extraction from list/string/missing/oversized/non-string-item/dict-
  shaped claims (malformed shapes never crash — coerced to one opaque,
  non-matching value); role-precedence resolution across multiple
  matches; least-privilege floor on no match or empty mapping;
  `apply_role_mapping` no-ops when unconfigured or `role_source=="local"`,
  promotes/demotes and audits only on an actual change, is idempotent
  (two identical applications -> one audit event).
- **Route tests** (`tests/test_oidc_role_mapping_routes.py`, 8 tests):
  bootstrap first user stays admin/local regardless of mapping config;
  a second new user gets the mapped role at creation; a second new user
  gets the plain #17 default (operator) when mapping is unconfigured —
  explicit regression proof #17's behavior is unchanged; a returning
  user is promoted then demoted across three logins with different
  group claims, landing on `reader` when unmapped; an admin-pinned user
  is never touched by a subsequent OIDC login even though mapping would
  compute something different; unmapped group never escalates; role
  cannot be injected via callback query params; a real role change is
  audited.
- **Admin CRUD route tests** (`tests/test_admin_oidc_role_mapping.py`, 8
  tests): `require_admin` enforced; list/add/delete mappings; duplicate
  claim-value rejected; invalid role rejected; deleting a nonexistent
  mapping flashes an error, not a 500; `role_claim_name` save round-trip.
- **`tests/test_admin_users.py`** (+1 test): an admin edit always stamps
  `role_source = "local"`, including for a user that was previously
  `"oidc_mapped"`.
- **Headless UAT (required)** — `tests/uat/test_oidc_role_mapping.py`
  (2 scenarios): admin configures mapping -> new SSO employee gets the
  mapped role -> promoted on a second login with a different group ->
  admin manually pins the role via the real Users edit form -> a third
  SSO login asserting a group mapped to `admin` no longer overrides the
  pin; a second scenario proves a non-default `role_claim_name` (e.g.
  `"roles"` instead of `"groups"`) is honored end to end.
  `GRC_UAT_MODE=1 pytest -m uat tests/uat/test_oidc_role_mapping.py` —
  **2 passed**.
- **Migration**: clean `alembic upgrade head` (batch-mode `ALTER TABLE`
  for SQLite, matching the existing #37 RBAC migration's precedent);
  `alembic check` reports no model/migration drift.
- **Full regression suite**: `pytest -q` — **922 passed, 6 skipped**
  (Postgres-gated), **21 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**.
- **Full headless UAT**: `GRC_UAT_MODE=1 pytest -m uat tests/uat` —
  **20 passed, 1 skipped** (Postgres-gated), up from #17's 18/1 baseline
  — exactly this issue's 2 new scenarios, no regressions.
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  confirmed only `require_admin` can write mappings/settings; confirmed
  `apply_role_mapping` is idempotent and only ever mutates the single
  resolved user (no cross-user bleed); confirmed role is computed
  exclusively from already-signature-verified ID token claims — no
  client-supplied query/body param can inject a role (explicit test);
  confirmed a malformed/dict-shaped claim degrades to one harmless,
  non-matching value rather than crashing; confirmed the "at least one
  active admin must remain" guard in `admin_users.py::update_user` is
  untouched and still the only path that can demote the last admin, and
  that OIDC role sync never touches `role_source == "local"` users so
  it cannot create a self-lockout; confirmed the audit detail for a
  mapping change carries only group names, never token/secret material.

## Known deferred/untested paths

See design doc §8: no "un-pin" action to hand a `role_source="local"`
user back to OIDC-managed short of another admin edit; `reader`/
`auditor` remain functionally identical outside this feature (unrelated
to #18); no claim-name auto-discovery from provider metadata.
