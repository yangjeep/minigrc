# Issue #17: Generic, provider-neutral OIDC login and identity linking

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a second, standards-based OIDC login path (`/auth/oidc/*`) driven by
an admin-configured **issuer URL** via discovery
(`{issuer}/.well-known/openid-configuration`), so any standards-compliant
provider (Authentik, Keycloak, Okta, Entra ID, etc.) can authenticate
against miniGRC without provider-specific code. It coexists with the
pre-existing, unrelated Google-specific OIDC login
(`app/google_oidc.py`) — see the design doc §1 for why that repository
reality diverges from a literal reading of the issue text, and why both
paths are kept rather than merged in this slice.

## What changed

- `app/oidc.py` (new) — provider-neutral client: discovery, authorization
  URL construction, code exchange, and `verify_identity` (JWKS fetch +
  `joserfc` signature/`exp`/`iss`/`aud` validation, then manual
  nonce/email-verified/domain checks — same split Google's own module
  already uses, generic library underneath).
- `app/oidc_config.py` (new) — `resolve_oidc_config`: admin-DB row first,
  `GRC_OIDC_*` env vars fallback, else unconfigured. Mirrors
  `app/google_oidc_config.py` exactly.
- `app/models.py` — `OidcProviderSettings` (admin config row) and
  `ExternalIdentity` (`(issuer, subject)` -> user, unique-constrained) —
  new tables, migrated via `b4e7d1a92c6f`.
- `app/routers/oidc.py` (new) — `/auth/oidc/login`, `/auth/oidc/callback`.
  Identity resolution: a returning `(issuer, subject)` logs straight in;
  a first-time `(issuer, subject)` whose email collides with an existing
  local user is **rejected, not merged** — deliberately stricter than
  Google's existing auto-link-by-email behavior, per the issue's explicit
  instruction (design doc §2.2).
- `app/routers/admin_authentication.py` — new `/oidc` GET/POST pair,
  mirroring the existing `/google` pair's masked-secret-on-save
  discipline.
- `app/templates/admin/authentication/oidc.html` (new); cross-link added
  to `google.html`.
- `app/routers/auth.py` — login page now also resolves generic-OIDC
  config; `app/templates/login.html` — a second conditional SSO button.
- `app/config.py` — `oidc_issuer`/`oidc_client_id`/`oidc_client_secret`/
  `oidc_allowed_domains`/`oidc_display_name` + derived properties.
- `app/main.py` — registers `oidc.router`.
- `pyproject.toml` — new dependency `joserfc>=1.0` (the actively
  maintained JOSE/JWT library; `authlib`'s own `authlib.jose` is
  deprecated in favor of it as of authlib 1.7 — see design doc §2.1).
- `migrations/versions/b4e7d1a92c6f_...py` — `oidc_provider_settings`,
  `external_identities`. Both brand-new tables, no backfill. Verified
  clean `alembic upgrade head` from the current head and `alembic check`
  reports no model/migration drift.

## A deliberate, documented divergence from the issue's literal text

Issue #17 is written as if no OIDC login exists yet. Repository reality
disagrees: Google-specific OIDC login was already shipped, tested, and
merged before this session. Per `.agent/RULES.md` §2 / `.agent/LOOP.md`
§3, this is resolved as a repository-reality correction, not a silent
reinterpretation — full reasoning and the specific behavioral
requirements preserved (issuer+subject linking, no silent email merge,
discovery, mature-library validation, break-glass preservation, no
auto-role-grant) are in the design doc §1.

## Test strategy and results

- **Unit** (`tests/test_oidc.py`, 18 tests): discovery issuer-mismatch/
  missing-field/network-failure rejection; authorization URL shape; code
  exchange success/missing-token rejection; `verify_identity` accepting a
  valid token and rejecting wrong audience/issuer/expired/nonce-mismatch/
  unverified-email/disallowed-domain/malformed-token/bad-signature — all
  against **real** RSA-signed JWTs via `joserfc`, no mocking of the
  crypto itself. One test (`test_end_to_end_against_a_real_standards_shaped_provider`)
  stands up a real local HTTP server that behaves like a minimal
  standards-compliant OIDC provider (real discovery + real JWKS +
  real-RSA-signed ID token) with **no mocking anywhere** — the concrete
  "one standards-compliant test provider" demonstration the issue's
  verification section asks for.
- **Route tests** (`tests/test_oidc_routes.py`, 11 tests): disabled->404;
  login redirect sets state/nonce cookies; discovery-failure flash;
  callback rejects state mismatch/provider error/wrong audience/nonce
  mismatch/unverified email/disallowed domain (mocking only the
  discovery/exchange network calls — `verify_identity`'s own real
  signature verification still runs, against a locally-signed test JWT);
  new-user creation and login; logout revokes the session.
- **DB-config + admin route tests** (`tests/test_oidc_db_config.py`, 13
  tests): admin-row config takes priority over env fallback; auto-
  provision on/off (active vs. pending); pending user approved then
  signs in; disabled user rejected; **returning identity logs in without
  re-creating a user** (only one `ExternalIdentity`/`User` row after two
  logins); **first-login email collision is rejected, not merged** — the
  regression-relevant divergence from Google's own looser precedent,
  with an explicit assertion that the original user's identity is
  untouched; broken encryption key degrades to not-configured (never
  crashes); admin page reflects DB config and never leaks the secret;
  admin can save settings (trailing slash on issuer stripped); saving
  with a blank secret keeps the existing one; non-admin roles get
  401/403 on the settings page.
- **Headless UAT (required)** — `tests/uat/test_oidc_login.py`: an admin
  configures generic OIDC through the real Admin UI form -> a new
  employee signs in via SSO and reaches the dashboard as an active user
  -> signs out and signs back in with the same external identity,
  recognized as the same returning user (not re-created) -> a second
  identity asserting the same already-registered email is rejected, not
  merged. Runs against the real app over a real socket
  (`GRC_UAT_MODE=1 pytest -m uat tests/uat/test_oidc_login.py` — **1
  passed**). The provider round trip (discovery/token exchange) is
  mocked at the same boundary the route tests use;
  `verify_identity`'s real signature/claims validation still executes
  against a locally-signed JWT over the running server's real
  socket/session/CSRF/DB boundary — a fully unmocked real-HTTP fake
  provider is exercised separately at the unit layer (§ above); see
  design doc §7 for why duplicating that HTTP server inside the UAT
  harness would add complexity without adding new proof.
- **Migration check**: `alembic upgrade head` clean from the pre-#17 head
  on a fresh SQLite DB; `alembic check` reports "No new upgrade
  operations detected" (no model/migration drift).
- **Full regression suite**: `pytest -q` — **888 passed, 6 skipped**
  (Postgres-gated), **19 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#17 baseline (847/6/18/2) is exactly this issue's 42 new tests
  (18 + 11 + 13 unit/route/db-config, +1 new UAT scenario file) — no
  regressions elsewhere.
- **Full headless UAT**: `GRC_UAT_MODE=1 pytest -m uat tests/uat` —
  **18 passed, 1 skipped** (Postgres-gated), up from the pre-#17
  baseline of 17/1 — exactly this issue's new scenario, no regressions.
- **Migration/backend check**: no local PostgreSQL instance available in
  this environment (`TEST_DATABASE_URL` unset, matching the 6/1
  Postgres-gated skips above) — the new tables use only standard
  String/Text/Boolean/DateTime/ForeignKey/UniqueConstraint column types,
  identical in shape to every other admin-settings/identity table in
  this schema; CI's Postgres job (see #64) exercises the
  backend-neutral path this environment cannot.
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  confirmed only `require_admin` can write `OidcProviderSettings` (login/
  callback routes only ever create ordinary default-role users, matching
  Google's existing precedent — no privileged-role auto-grant); confirmed
  no client secret/token is ever logged or included in an audit `detail`
  field; narrowed `verify_identity`'s exception handling from a bare
  `except Exception` to `except JoseError` after confirming (via a
  throwaway script) that joserfc raises `DecodeError` — a `JoseError`
  subclass — for malformed/non-JWT input, not some other exception type,
  and added a regression test for that malformed-token case; confirmed
  the issuer URL's SSRF exposure is admin-only deployment configuration,
  the same accepted trust boundary already used for `GRC_S3_ENDPOINT_URL`;
  identified and documented (rather than silently fixing with new
  locking complexity) a rare concurrent-first-login race on the new
  `external_identities` unique constraint — the same pre-existing
  exposure Google's own `google_subject` unique constraint already has,
  degrading to the app's existing generic 500 handler rather than a
  crash or a leak.

## Known deferred/untested paths

See design doc §8: no claim/group-to-role mapping (#18 — every
generic-OIDC user gets the ordinary default-provisioning role, same as
any new user today); no Authentik-specific deployment doc (#19); no
IdP-population-as-evidence integration (#20); no admin-mediated "link
this external identity to that existing user" UI for the email-collision
case (documented, not silently left as a dead end); the rare concurrent-
first-login race noted above.
