# Issue #17: Generic OIDC login, session handling, and external identity linking

Parent epic: #16. See also #18 (claim/role mapping, next slice), #19 (Authentik
reference deployment), #20 (IdP evidence integration) — none implemented yet.

## 1. Repository-reality check (issue assumption vs. what actually exists)

Issue #17's execution prompt is written as if no OIDC login exists yet
("inspect the current local session authentication implementation... add
generic provider configuration"). That is stale: **Google OIDC login already
exists and is merged** (`app/google_oidc.py`, `app/google_oidc_config.py`,
`app/routers/google_oidc.py`, `app/routers/admin_authentication.py`,
`GoogleOidcSettings` in `app/models.py`, `User.google_subject`), fully
tested (`tests/test_google_oidc.py`, `tests/test_google_oidc_db_config.py`),
and wired into `login.html`/`main.py`. It predates this session.

That implementation is **provider-specific, not generic**: it hardcodes
Google's endpoints (`accounts.google.com`), uses Google's own
`google.oauth2.id_token.verify_oauth2_token` helper (not issuer discovery +
a general JWKS/JWT library), and links identity via a single
`User.google_subject` column — there is no way to configure a second,
different OIDC provider (Authentik, Keycloak, Okta, Entra) through it.

Per `.agent/RULES.md` §2 ("repository reality beats stale docs") and
`.agent/LOOP.md` §3 ("challenge issue assumptions when repository reality
disagrees"), the resolved plan is:

- **Keep the existing Google OIDC flow exactly as-is.** It is shipped,
  tested, and working; RULES.md §11 says keep diffs focused and don't
  refactor unrelated working code. Migrating Google's own login onto a new
  generic mechanism is a real project (touching a live, tested auth path)
  that neither this issue nor the PRD requires in this slice — PRD §5.11
  explicitly lists Google Workspace as *one of several* provider classes
  the generic foundation should be *compatible with*, not a mandate to
  rebuild Google's existing integration on day one.
- **Add a second, genuinely generic OIDC login path alongside it**,
  addressed at `/auth/oidc/*`, driven by an admin-configured **issuer URL**
  (discovery-based) rather than a hardcoded provider. This is the path
  Authentik/Keycloak/Okta/Entra deployments will use, and is what #19 will
  document/verify against a real self-hosted Authentik instance.
- Both paths converge on the same `start_user_session` helper
  (`app/routers/auth.py`) — session issuance is already provider-agnostic.
- Introduce a **new, provider-neutral identity-linking table**
  (`ExternalIdentity`, keyed by `(issuer, subject)`) for the generic path,
  rather than extending `User.google_subject` — the issue explicitly asks
  for "deterministic external identity linking by issuer + subject" as the
  general mechanism, and a single nullable column can't represent more than
  one non-Google provider per user.

This is a deliberate, documented divergence from a literal reading of the
issue text, not a silent reinterpretation: the issue's *behavioral*
requirements (issuer+subject linking, no silent email merge, discovery,
mature-library validation, break-glass preservation, no auto-role-grant)
are all implemented; only the "there is no existing OIDC" framing is
corrected against reality.

## 2. Scope decisions

1. **New dependency: `joserfc>=1.0`.** The issue requires "a mature
   library rather than hand-rolling protocol validation." Google's flow
   already gets this from `google-auth`, but that library is Google-only.
   `joserfc` (the actively-maintained JOSE/JWT library — `authlib`'s own
   `authlib.jose` module is deprecated in favor of it as of authlib 1.7)
   provides JWKS import (`KeySet.import_key_set`) and JWT signature/claims
   validation (`jwt.decode` + `JWTClaimsRegistry.validate`, checking
   signature, `exp`/`iat`, `iss`, `aud`) for arbitrary standards-compliant
   providers. `nonce` and email-domain checks stay manual, mirroring
   exactly how `app/google_oidc.py::verify_identity` already splits
   library-verified vs. manually-verified claims — same shape, generic
   library underneath.
2. **No silent email-based account merge — stricter than the existing
   Google behavior, per the issue's explicit instruction.** Google's flow
   auto-links an existing local user by normalized-email match on first
   Google sign-in (`app/routers/google_oidc.py::_resolve_user`). Issue #17
   states plainly: "Do not silently merge two existing users solely
   because email matches." For the new generic path, a first-time
   `(issuer, subject)` whose asserted email matches an existing local user
   is **rejected** with a clear "contact an administrator" message, not
   linked. Only a returning `(issuer, subject)` that was already linked
   (via a prior successful generic-OIDC first-login with no email
   collision) logs straight in. This is a deliberate, more conservative
   policy for the new provider-neutral surface — documented here rather
   than silently copying Google's older, looser precedent.
3. **Issuer URL is admin-only deployment configuration, not per-request
   attacker input** — same SSRF posture already accepted for
   `GRC_S3_ENDPOINT_URL` (`app/config.py`). Only `require_admin` can set
   it; ordinary users never influence the discovery target.
4. **Email claim is required.** Like Google's flow, a provider response
   with no `email` claim is rejected — `User.email` is unique/non-null and
   auto-provisioning needs one. Providers must be configured with a scope
   that returns email (we always request `openid email profile`).
5. **`allowed_domains`** (optional, comma-separated) restricts sign-in by
   the email claim's domain — the same operator-facing knob Google's
   config already has, generalized (no `hd` claim assumption; any OIDC
   provider's asserted `email` can be domain-checked).
6. **Role/status provisioning policy matches Google's existing default
   exactly** (first user + auto-provision -> `admin`, else `operator`;
   `active` if auto-provision else `pending` awaiting admin approval) —
   this is the ordinary new-user default, not a role grant "because of
   OIDC," so it does not violate "OIDC login must not automatically grant
   privileged roles." Claim/group-driven role mapping is explicitly out of
   scope here (#18).
7. **Logout stays unchanged.** The existing `/logout` route only clears
   miniGRC's own session; the issue only requires failing safely when
   provider-side logout isn't supported, which the current behavior
   already satisfies by never depending on it.
8. **Admin config UI mirrors the Google settings page exactly** — a new
   `/admin/authentication/oidc` GET/POST pair with the same
   masked-secret-on-save discipline (`app/routers/admin_authentication.py`).

## 3. Model

New tables (`app/models.py`), Alembic-migrated:

```python
class OidcProviderSettings(Base):
    """Admin-configured generic OIDC login settings — the single most
    recently updated row is the active configuration (same shape as
    GoogleOidcSettings). issuer drives discovery; no provider-specific
    endpoints are ever hardcoded here."""

    __tablename__ = "oidc_provider_settings"
    id: str
    enabled: bool
    display_name: str  # shown on the login button, e.g. "Company SSO"
    issuer: str
    client_id: str
    secret_id: str | None  # -> Secret, encrypted client secret
    allowed_domains: str  # comma-separated, "" = no restriction
    auto_provision_enabled: bool
    updated_by: str
    created_at, updated_at


class ExternalIdentity(Base):
    """A provider-neutral (issuer, subject) -> user link, generic-OIDC
    only (Google keeps its own User.google_subject column). Deterministic
    identity authority per PRD §5.11 — email is never the link key."""

    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: str
    user_id: str  # -> users.id
    issuer: str
    subject: str
    created_at: datetime
```

## 4. Modules

- `app/oidc.py` (new, mirrors `app/google_oidc.py`'s shape):
  `OidcError`, `ProviderMetadata`, `OidcIdentity`, `new_state`/`new_nonce`,
  `discover_provider_metadata(issuer)` (fetches
  `{issuer}/.well-known/openid-configuration`, verifies the document's own
  `issuer` field matches the configured issuer exactly — rejects issuer
  confusion), `build_authorization_url(...)`,
  `exchange_code_for_id_token(...)`, `verify_identity(...)` (JWKS fetch +
  authlib signature/`exp`/`iss`/`aud` validation, then manual
  nonce/email/domain checks).
- `app/oidc_config.py` (new, mirrors `app/google_oidc_config.py`):
  `ResolvedOidcConfig`, `resolve_oidc_config(db, settings)` — admin row
  first, else `GRC_OIDC_*` env vars (new, same fallback shape as the
  existing `GRC_GOOGLE_OIDC_*`), else unconfigured.
- `app/config.py`: `oidc_issuer`, `oidc_client_id`, `oidc_client_secret`,
  `oidc_allowed_domains`, `oidc_display_name` (default `"SSO"`), plus
  `oidc_enabled`/`oidc_allowed_domains_set`/`oidc_redirect_uri` properties.
- `app/routers/oidc.py` (new): `/auth/oidc/login`, `/auth/oidc/callback` —
  same state/nonce cookie discipline as `app/routers/google_oidc.py`;
  `_resolve_user` implements the identity-linking/no-merge policy in §2.2.
- `app/routers/admin_authentication.py`: add `/oidc` GET/POST pair next to
  the existing `/google` pair, same masked-secret pattern.
- `app/routers/auth.py`: login GET also resolves `resolve_oidc_config` and
  passes `oidc_enabled`/`oidc_display_name` to `login.html`.
- `app/templates/login.html`: a second conditional SSO button.
- `app/templates/admin/authentication/oidc.html` (new, mirrors `google.html`).
- `app/main.py`: register `oidc.router`; add an "Authentication" admin nav
  entry pointing at `/admin/authentication/oidc` is unnecessary (the
  existing single "Authentication" nav item already lands on the Google
  page; the OIDC page is linked from there, and vice versa, via a small
  cross-link in each template) — no new top-level nav item needed.

## 5. Identity-linking algorithm (`app/routers/oidc.py::_resolve_user`)

```
verified identity = (issuer, subject, email, email_verified)

existing = ExternalIdentity where (issuer, subject) == (identity.issuer, identity.subject)
if existing:
    return existing.user   # deterministic returning-user path

if email domain not allowed: reject (clear message)
if not email_verified: reject

user_by_email = User where email == normalized(identity.email)
if user_by_email is not None:
    reject with "already associated with a different account" (NO MERGE)

create User(role=..., status=..., password_hash="")
create ExternalIdentity(issuer, subject, user_id=new_user.id)
record_audit_event(create_via_oidc[_pending])
return new_user (or None if pending, matching Google's pending-return shape)
```

Every branch is covered by a named test in §7.

## 6. Security review (adversarial, performed at design time; re-verified after implementation)

- **Issuer confusion**: `discover_provider_metadata` rejects a discovery
  document whose own `issuer` claim doesn't match the configured issuer.
- **Token substitution**: `aud` (must equal our `client_id`) and `iss`
  (must equal the configured issuer) are both enforced by authlib's
  claims validation before any identity is trusted.
- **Nonce replay**: nonce is single-use per login attempt (stored only in
  an HttpOnly cookie scoped to the OAuth round-trip, compared exactly once).
- **State/CSRF on the callback**: `secrets.compare_digest` state check,
  identical to the Google flow.
- **Account takeover via email collision**: explicitly rejected, not
  merged (see §2.2) — the primary reason this design deviates from
  Google's existing looser precedent.
- **Secret handling**: client secret follows the exact
  `create_encrypted_secret`/masked-on-redisplay pattern already used for
  Google and S3 credentials; never logged, never in audit details.
- **SSRF via issuer URL**: admin-only configuration input, same accepted
  posture as `s3_endpoint_url` (§2.3).
- **Break-glass preservation**: local email/password login
  (`app/routers/auth.py`) is untouched by this change.

## 7. Test strategy

- **Unit** (`tests/test_oidc.py`): `discover_provider_metadata` accepts a
  valid document and rejects a mismatched-issuer/malformed one (mocked
  HTTP); `build_authorization_url` shape; `exchange_code_for_id_token`
  error handling; **one genuine end-to-end integration test with no
  mocking at all** — a real `http.server.ThreadingHTTPServer` fake IdP
  (real HTTP, a real RSA keypair, a real authlib-signed JWT served from a
  real JWKS endpoint) proving discovery + code exchange + signature/`exp`/
  `iss`/`aud` verification genuinely work against a standards-shaped
  provider end to end — this is the concrete "one standards-compliant
  test provider" demonstration the issue's verification section asks for.
  `verify_identity` unit cases for bad signature/expired/wrong nonce/
  unverified email/disallowed domain using that same fake-signed-JWT
  machinery.
- **Route tests** (`tests/test_oidc_routes.py`): mirrors
  `tests/test_google_oidc.py`'s structure — disabled->404, login redirect
  sets state/nonce cookies, callback rejects state mismatch/provider
  error/wrong nonce/unverified email/disallowed domain (mocking
  `app.routers.oidc.verify_identity`'s return value, the same boundary
  Google's own accepted tests mock at), new-user creation
  (active/pending), returning-user login via `ExternalIdentity`, **email
  collision is rejected, not merged** (the regression-relevant case this
  design most wants covered), logout after OIDC login revokes the session.
- **Config tests** (`tests/test_oidc_config.py`): admin-row vs. env-var
  fallback vs. unconfigured, mirroring `tests/test_google_oidc_db_config.py`.
- **Admin route tests** (`tests/test_admin_authentication_oidc.py`):
  `require_admin` enforcement, secret masked-on-save, save round-trip.
- **Headless UAT** (`tests/uat/test_oidc_login.py`, required): drives the
  real app over a real socket exactly like the other UAT scenarios. The
  outbound call to the *provider* is mocked at the same `verify_identity`
  boundary the route tests use — patching a Python function is safe here
  because the UAT harness runs uvicorn in a background thread inside this
  same process, so the patch is visible process-wide for the duration of
  the request. (`.agent/TESTING.md` §1.3 explicitly allows fakes for
  "remote vendor behavior" as long as miniGRC's own boundary executes for
  real — session/CSRF/cookies/DB all execute genuinely here; only the
  simulated far end of the OAuth round trip is a double, exactly as
  `tests/test_google_oidc.py` already does one layer down.) A full fake
  HTTP IdP already exists as the *unit-layer* integration proof in §7.1 —
  duplicating a real HTTP server inside the UAT harness would add
  complexity without adding new proof. Scenario: admin enables generic
  OIDC -> a new user signs in and lands on the dashboard -> signs out ->
  signs back in and is recognized as the same (returning) identity ->
  a second identity whose email collides with the first is rejected.

## 8. Known deferred/untested paths

- No claim/group-to-role mapping (#18) — every generic-OIDC user gets the
  same default-provisioning role as any new user today.
- No Authentik-specific deployment doc/verification (#19) — this issue
  only proves the generic mechanism against a standards-shaped fake
  provider.
- No IdP-population-as-evidence integration (#20).
- No admin-mediated "link this external identity to that existing local
  user" UI for the email-collision case — an admin must resolve it
  manually today (e.g., change the local account's email, or accept a
  duplicate account and reconcile roles). Documented rather than silently
  left as a dead end; a small additive follow-up if real usage needs it.
- Google OIDC's own looser (auto-link-by-email) behavior is untouched —
  not revisited by this issue (see §1).
- **No explicit lock/retry around first-time identity creation.** Two
  genuinely concurrent first-logins for the same brand-new `(issuer,
  subject)` could both pass the "no existing identity" check and race on
  `external_identities`' unique constraint; the loser surfaces as a
  generic 500 (`app/main.py`'s existing catch-all handler), not a crash
  and not a security leak. This is the same exposure Google's own
  `_resolve_user` already has on `google_subject`'s unique constraint
  today — not a new gap introduced by this issue, and genuinely rare
  (two simultaneous first sign-ins of the identical identity within
  milliseconds). Left unmitigated rather than adding new locking
  complexity neither the issue nor Google's existing precedent requires.
