# Authentication: Google OAuth and break-glass recovery

## Two ways to sign in, always both available

- **Local email/password** (`app/routers/auth.py`) — the break-glass path.
  Always available at `/login`, regardless of Google OAuth's configuration
  or health. There is no self-registration; accounts are created via
  `python -m app.cli create-user` (the very first user becomes admin) or
  by an admin through Admin > Users.
- **Google OAuth** (`app/routers/google_oidc.py`) — optional. Configure it
  in Admin > Authentication > Google OAuth, or via the legacy
  `GRC_GOOGLE_OIDC_*` environment variables if you're not using the Admin
  UI. Disabled (404 on `/auth/google/login`) until a usable configuration
  exists.

Because these are two independent code paths sharing only session
issuance (`app/routers/auth.py::start_user_session`), a broken or
misconfigured Google OAuth setup can never lock out local login — see
`tests/test_break_glass.py` for the regression tests proving this
(configuration never set, explicitly disabled, and broken encryption key
all leave local login unaffected).

## Setting up Google OAuth

1. In Google Cloud Console, create an OAuth 2.0 Client ID (Web
   application) with an authorized redirect URI of
   `https://<your-domain>/auth/google/callback`.
2. Set `GRC_PUBLIC_BASE_URL` to your deployment's public URL — this is
   the only source used to build the redirect URI (the request's `Host`
   header is never trusted for this).
3. As an existing admin, go to Admin > Authentication > Google OAuth and
   enter the client ID and secret, tick **Enabled**, and optionally set
   allowed Workspace domains (comma-separated `hd` values — leave blank
   to allow any verified Google account).
4. Decide the **first-login policy**:
   - **Auto-provision on** — a first-time sign-in from an allowed domain
     immediately creates an active account.
   - **Auto-provision off** (default) — a first-time sign-in creates a
     `pending` account and rejects the login; an admin approves it by
     setting its status to `active` under Admin > Users.

## Identity handling

- Google's stable, non-reassignable `sub` claim (`User.google_subject`)
  is the primary match key, not email — so a user renaming their Google
  Workspace email address keeps the same account (the stored email is
  updated to match, and this is audited as `google_identity_email_changed`).
- If an email is already linked to a *different* Google subject, the
  sign-in is rejected as an identity collision rather than silently
  reassigning the account — resolve this manually via Admin > Users if
  it happens.
- `disabled` and `pending` users are rejected at every login path
  (local and Google), and `require_login` re-checks status on every
  request, so disabling a user takes effect immediately even against an
  already-issued session cookie.

## Break-glass recovery

If Google OAuth is misconfigured, its client secret can't be decrypted
(e.g. after rotating `GRC_ENCRYPTION_KEY` without re-saving it), or you
simply need to disable it: local login at `/login` is unaffected. To
recover admin access without Google at all:

```bash
python -m app.cli create-user --email you@example.com   # only if no users exist yet
python -m app.cli promote-admin --email you@example.com  # grant admin to an existing user
```

Both commands run directly against the configured database and require
no password for `promote-admin` — they're intended as the operator's
last-resort recovery path and should be run from a trusted shell with
direct database access (e.g. `kubectl exec` into the web pod), not
exposed as an HTTP endpoint.

## Secret rotation

- **Google OAuth client secret**: re-enter it in Admin > Authentication >
  Google OAuth — a blank submission always keeps the existing value, so
  only fill it in when actually rotating. The previous `Secret` row is
  left in place (never deleted) for audit history; only the active
  `GoogleOidcSettings` row's pointer changes.
- **`GRC_ENCRYPTION_KEY`**: rotating this without re-encrypting existing
  secrets (Google OAuth client secret, Google Drive refresh token, AWS
  `external_id`, external DB connection credentials) makes them
  unresolvable — each affected feature degrades gracefully (Google OAuth
  becomes "not configured" rather than crashing) but will need its
  secret re-entered through its Admin page after a key rotation.
