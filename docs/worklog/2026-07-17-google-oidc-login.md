# Google OIDC login

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Fourth slice of `feat/startup-compliance-operations`: optional Google
OpenID Connect login (Authorization Code flow), disabled unless
`GRC_GOOGLE_OIDC_CLIENT_ID`/`_CLIENT_SECRET`/`GRC_PUBLIC_BASE_URL` are all
configured. Local email/password login is untouched and remains the
break-glass fallback. See ADR #16.

## Files Changed

- `app/google_oidc.py` — `build_authorization_url`,
  `exchange_code_for_id_token` (httpx POST to Google's token endpoint),
  `verify_identity` (delegates signature/issuer/audience/expiry to
  `google.oauth2.id_token.verify_oauth2_token`; validates nonce,
  `email_verified`, `sub` presence, and `hd` against configured allowed
  domains ourselves).
- `app/routers/google_oidc.py` — `/auth/google/login` (sets short-lived
  `state`/`nonce` cookies, redirects to Google), `/auth/google/callback`
  (state compare via `secrets.compare_digest`, exchanges code, verifies
  identity, finds-or-creates a `User` linked to an existing `Person` by
  normalized email if one exists, starts a session). Both 404 when OIDC
  isn't configured.
- `app/routers/auth.py` — extracted `start_user_session()` (session row +
  cookie + redirect) so local login and Google OIDC share identical
  session issuance; local `login_submit`/`login_form` now read settings
  from `request.app.state.settings` instead of the global
  `get_settings()`, for consistency with the rest of the app and so tests
  can configure OIDC per test app instance.
- `app/config.py` — `public_base_url`, `google_oidc_client_id/secret`,
  `google_oidc_allowed_domains` (+ `_set`, `_enabled`, `_redirect_uri`
  properties).
- `app/templates/login.html` — conditional "Sign in with Google" link.
- `pyproject.toml` — promoted `httpx` from dev-only to a main dependency
  (needed for the token exchange call); added `google-auth` and `requests`
  (a hard runtime dependency of `google.auth.transport.requests`).
- `tests/test_google_oidc.py` — disabled-by-default (404), state mismatch,
  provider error, wrong audience, nonce mismatch, unverified email,
  disallowed domain, new-user creation (first user → admin), linking to
  an existing local user by normalized email, linking to an existing
  `Person`, and a post-login authenticated request. All external Google
  calls are mocked (`exchange_code_for_id_token` and
  `verify_oauth2_token`) — no real network calls in tests.

## Verification

- [x] `pytest` — 120 passed
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] No schema change — confirmed via `alembic revision --autogenerate`
  producing an empty upgrade/downgrade (checked, then discarded).

## Decisions & Alternatives Rejected

- See ADR #16.
- Domain restriction (`GRC_GOOGLE_OIDC_ALLOWED_DOMAINS`) is opt-in: empty
  means no restriction (any verified Google account may sign in), not
  "reject everyone." An admin must explicitly configure it to lock sign-in
  to their Workspace domain.
- A first-time Google sign-in creates a `User` with `password_hash=""` —
  `verify_password` safely rejects any local-login attempt against that
  hash (caught by its existing `except Exception: return False`), so the
  account is Google-only until/unless a local password is separately set;
  no new column or account-type flag needed.
- ID tokens are never persisted past the callback request — no
  "id_token unnecessarily kept" surface to audit.

## Known Gaps / Follow-ups

- No route yet to set a local password for a Google-created account (not
  requested by this branch's scope) — such a user can only sign in via
  Google until an admin/future feature adds one.
- Google Drive OAuth (next commit) is a *separate* connection/consent
  flow from this login integration, even though both can use the same
  Google Cloud project — see ADR #16.
