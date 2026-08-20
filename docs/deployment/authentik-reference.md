# Authentik reference: generic OIDC login

Authentik is a **reference** self-hosted OIDC provider for miniGRC's
generic OIDC login (issues #17/#18) — miniGRC contains no Authentik-
specific code. `app/oidc.py` drives everything through standards-based
discovery (`{issuer}/.well-known/openid-configuration`) and validates ID
tokens with `joserfc` against the provider's own published JWKS, so any
standards-compliant OIDC provider (Keycloak, ZITADEL, Entra ID, Okta,
...) configures through the same miniGRC-side fields. This page is the
Authentik-specific half of that pairing: what to configure on
Authentik's side, and what's actually verified vs. what needs local
confirmation.

**Verification performed for this page:** live documentation lookup
against `docs.goauthentik.io` (2026-08). **Not performed:** an
interactive interop test against a real running Authentik instance — no
Authentik deployment was available in this implementation environment.
Everything below is documentation/config-level verification; the "known
limitations" section is explicit about which claims are confirmed by
Authentik's own docs and which are a documented-but-unverified starting
point.

## miniGRC-side configuration variables

Set via Admin > Authentication > Generic OIDC (`/admin/authentication/oidc`,
DB-backed — preferred) or the legacy `GRC_OIDC_*` environment variables
(pre-Admin-UI deployments; see
[`configuration-contract.md`](configuration-contract.md) for the
resolution precedence between the two):

| miniGRC field | Value for Authentik |
|---|---|
| Issuer | `https://<your-authentik-host>/application/o/<application-slug>/` — Authentik's per-application issuer URL, **not** the bare host. Confirm the exact value from Authentik's Application page ("OpenID Configuration URL" minus the `.well-known` suffix). |
| Client ID | The OAuth2 Provider's generated Client ID |
| Client Secret | The OAuth2 Provider's generated Client Secret (confidential client) |
| Redirect URI (Authentik-side field) | `https://<your-minigrc-host>/auth/oidc/callback` — must exactly match `GRC_PUBLIC_BASE_URL` + this fixed path; `app/config.py::oidc_redirect_uri` builds it from `GRC_PUBLIC_BASE_URL` only, never from the request's `Host` header |
| Role/group claim name | `groups` (default; configurable) — see "Group-claim mapping" below |
| Allowed domains | Optional comma-separated email-domain allowlist, miniGRC-side |

miniGRC always requests exactly `openid email profile` scopes
(`app/oidc.py::LOGIN_SCOPES` — a fixed constant, not admin-configurable)
and rejects a sign-in whose `email_verified` claim is falsy or whose
`nonce` doesn't match. Authentik's OAuth2 Provider must be able to issue
`email_verified=true` for the accounts that will sign in (true for
Authentik's own built-in user accounts using email verification, or any
upstream federated source Authentik itself trusts).

## Authentik-side setup

1. **Create an OAuth2/OpenID Provider** (Applications > Providers > Create):
   - **Client type:** Confidential (miniGRC exchanges the auth code
     server-side with a client secret — never a public/PKCE-only client).
   - **Redirect URIs:** `https://<your-minigrc-host>/auth/oidc/callback`,
     strict match (Authentik supports regex redirect URIs; a strict
     literal match is safer than a wildcard here).
   - **Signing Key:** select an RSA/EC key (asymmetric signing) — miniGRC
     verifies signatures via the provider's published JWKS
     (`app/oidc.py::_fetch_key_set`), which requires asymmetric signing;
     do not leave this on symmetric (client-secret-based) signing.
   - **Scopes:** at minimum `openid`, `email`, `profile` (these must be
     available to the provider for the client to request them — see
     "Group-claim mapping" below for why `profile` alone may not be
     sufficient for role mapping).
2. **Create an Application** (Applications > Applications > Create) bound
   to the provider above, with a policy/group binding controlling which
   Authentik users may authenticate to it.
3. **Confirm the discovery document** resolves correctly before wiring up
   miniGRC: `curl https://<host>/application/o/<slug>/.well-known/openid-configuration`
   should return `issuer`, `authorization_endpoint`, `token_endpoint`, and
   `jwks_uri` — `app/oidc.py::discover_provider_metadata` rejects the
   document outright if its own `issuer` field doesn't exactly match the
   issuer miniGRC was configured with (an issuer-confusion guard), so
   this must match byte-for-byte what you enter in miniGRC's Issuer field.

## Group-claim mapping (issue #18)

**Confirmed from Authentik's official documentation:** the built-in
`profile` scope is documented as including "basic profile information,
such as the user's username, name, and group membership" —
`docs.goauthentik.io`'s OAuth2 provider page states this explicitly.

**Not confirmed from official docs alone:** the exact claim key name
`profile`'s built-in group data appears under, or whether it is
guaranteed present in every Authentik version/configuration. Rather than
depend on unverified default behavior for something as consequential as
role mapping, the recommended, verifiable path is an **explicit custom
Scope Mapping**, which puts the claim key entirely under your control and
matches miniGRC's own default expectation exactly:

1. Customization > Property Mappings > Create > **Scope Mapping**.
2. **Name:** any descriptive label (e.g. `miniGRC Groups Claim`) — this
   is just Authentik's internal display name for the mapping and has no
   effect on activation. **Scope name:** `profile` — **not** `groups`.
   Authentik only includes a scope mapping's claims when its **Scope
   name** field is actually requested by the client, and miniGRC's
   requested scopes are fixed to `openid email profile`
   (`app/oidc.py::LOGIN_SCOPES`); a mapping whose Scope name is `groups`
   would silently never activate, since miniGRC never requests a
   `groups` scope. Set Scope name to `profile` (or `email`) — one of the
   three scopes miniGRC already requests — so this mapping's claims ride
   along with it.
3. **Expression** — a starting point to verify/adjust against your
   deployed Authentik version's actual user/group model before relying
   on it (obtained via documentation lookup, not confirmed by direct
   source inspection in this session):
   ```python
   return {
       "groups": [group.name for group in request.user.ak_groups.all()],
   }
   ```
4. Attach this mapping to the OAuth2 Provider's **Scope Mappings** field
   (alongside the built-in `openid`/`email`/`profile` mappings).
5. In miniGRC, set the **role/group claim name** to `groups` (the
   default) at `/admin/authentication/oidc`, then define claim-value →
   role rows at `/admin/authentication/oidc/roles`
   (`app/oidc_role_mapping.py`) — e.g. map an Authentik group named
   `minigrc-admins` to miniGRC's `admin` role. An unmapped claim value
   defaults to the least-privileged role (`reader`) rather than blocking
   login outright (fail-safe/least-privilege — see
   `LEAST_PRIVILEGED_ROLE` and `app/oidc_role_mapping.py::compute_mapped_role`).

`app/oidc.py::OidcIdentity.claims` carries the full verified claim set
through unmodified — `app/oidc_role_mapping.py::extract_claim_values`
reads whichever key the admin configured as-is (a list claim is used
item-by-item; a bare string claim is treated as one value; nothing is
split on a guessed delimiter), so this mapping approach works regardless
of the exact shape Authentik's own default `profile` scope might already
provide, verified or not.

## Local break-glass path remains independent

Authentik being misconfigured, unreachable, or having its provider
deleted never affects local email/password login at `/login` — the two
paths share only session issuance
(`app/routers/auth.py::start_user_session`), and `app/oidc_config.py`'s
`resolve_oidc_config` degrades to "unusable" rather than raising when
Authentik's discovery document or JWKS can't be fetched (see
[`configuration-contract.md`](configuration-contract.md) for this
shared graceful-degradation pattern across every optional auth mode).
Disabling/removing the Authentik `OidcProviderSettings` row (or leaving
it disabled) makes `/auth/oidc/login` return 404, exactly as if generic
OIDC had never been configured — confirmed by reading
`app/routers/oidc.py`'s login route directly.

## Known limitations / provider differences relevant to miniGRC's contract

- **Group-claim location is Authentik-version/config-dependent, not a
  fixed default miniGRC can assume** — this is the one genuine
  uncertainty this reference could not fully close via documentation
  alone (see above); the explicit custom Scope Mapping sidesteps it by
  putting the claim key under direct operator control.
- **Symmetric vs. asymmetric ID token signing:** unlike some providers
  that only support HMAC/symmetric signing by default, Authentik
  supports (and this guide requires) asymmetric signing verified via
  JWKS — a provider that only offers symmetric signing would not work
  with `app/oidc.py`'s current JWKS-only verification path.
- **Per-application issuer URLs:** Authentik's issuer is scoped per
  Application (`/application/o/<slug>/`), not one fixed host-level
  issuer — a detail that differs from some other providers (e.g.
  Keycloak's per-realm issuer, which is also not the bare host but has a
  different path shape) and is easy to configure incorrectly by pasting
  just the Authentik hostname.
- **No SAML, no SCIM** — out of scope for both this reference and
  miniGRC's generic OIDC contract (see issue #19's explicit non-goals).

## Non-goals of this page

- No Authentik-specific application code — none was written; this page
  is purely configuration documentation.
- No bundled/managed Authentik deployment — self-hosting Authentik
  itself is the operator's own responsibility.
- No broad IdP comparison matrix beyond the interoperability facts above
  that actually matter to miniGRC's contract.
- No SOC 2 evidence integration from IdP metadata — that's issue #20,
  explicitly out of scope here.
