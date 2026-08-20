# Issue #19: Authentik reference deployment and OIDC interoperability verification

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** documentation

## Goal

Provide a documented, verified Authentik reference configuration for
self-hosted miniGRC deployments while keeping the application
implementation generic OIDC — no Authentik-specific code. See issue #19
for the full framing; this issue is deliberately placed before #20
(IdP-metadata-as-evidence) in parent epic #16's own recommended order
("OIDC foundation -> claim mapping -> Authentik reference -> evidence
integration"), which this session follows rather than jumping ahead.

## Inspection performed before writing

Direct code inspection of the already-merged generic OIDC implementation
(#17/#18) — `app/oidc.py`, `app/oidc_config.py`, `app/oidc_role_mapping.py`,
`app/routers/oidc.py`, `app/routers/admin_authentication.py`, and the
`OidcProviderSettings`/`ExternalIdentity`/`OidcRoleMapping` models —
confirmed:

- Discovery is fully standards-based (`{issuer}/.well-known/openid-configuration`),
  no hardcoded provider endpoints.
- Requested scopes are a **fixed constant**, `"openid email profile"`
  (`app/oidc.py::LOGIN_SCOPES`) — not admin-configurable. This matters
  directly for group-claim mapping (see below).
- ID tokens are verified via `joserfc` against the provider's own
  published JWKS — asymmetric signing only; there is no support for a
  symmetric/HMAC-signed ID token in the current implementation.
- The issuer in the discovery document is checked to exactly match the
  configured issuer (`discover_provider_metadata`) — an issuer-confusion
  guard that makes getting Authentik's per-application issuer URL exactly
  right a real, not cosmetic, configuration requirement.
- Admin config lives at `/admin/authentication/oidc` (provider settings)
  and `/admin/authentication/oidc/roles` (claim-value → role mapping),
  both DB-backed with a documented legacy-env-var fallback
  (`resolve_oidc_config`).
- The local break-glass login path is provably independent — confirmed
  by reading `resolve_oidc_config`'s graceful-degradation behavior and
  `app/routers/oidc.py`'s login route (404s cleanly when unconfigured).

## Primary-source verification performed

Live documentation lookups against `docs.goauthentik.io` (2026-08),
specifically the OAuth2/OIDC provider and property-mappings pages. Key
finding: Authentik's own documentation states its built-in `profile`
scope "includes basic profile information, such as the user's username,
name, and group membership" — but does **not** specify the exact claim
key name that group data appears under, and I could not confirm this
further from official docs alone (source-level verification of
Authentik's Django migrations/fixtures was attempted but did not locate
the default scope-mapping expression text within the tool budget for
this issue).

**Decision:** rather than assert an unverified claim-key assumption for
something as consequential as role mapping, the reference guide
recommends an explicit custom Scope Mapping that puts the claim key
under direct operator control (`groups`, matching miniGRC's own default
`role_claim_name`) — correct regardless of what Authentik's built-in
`profile` mapping turns out to expose in a given version. The custom
mapping's example expression was sourced via documentation lookup, not
confirmed against Authentik's actual source, and the reference guide
says so explicitly rather than presenting it as verified fact.

## No live Authentik interoperability test performed

No Authentik deployment was available in this implementation
environment. Per the issue's own explicit allowance ("If practical in
the available environment, run an interoperability test against a
real/dev Authentik deployment; otherwise clearly distinguish
documentation/config verification from live runtime verification"),
this is disclosed plainly in the reference doc's own header rather than
implied as verified. A future session with cluster/Docker access to spin
up a real Authentik instance should complete that live verification and
update the doc's confidence level for the group-claim question above.

## What changed

- `docs/deployment/authentik-reference.md` (new): the reference
  configuration guide — miniGRC-side variables, Authentik-side OAuth2
  Provider/Application setup, the group-claim mapping question and its
  recommended resolution, break-glass independence (re-confirmed by
  reading the actual login-route code, not assumed from #17/#18's own
  docs), and known limitations/provider differences.
- `docs/deployment/authentication.md`: one cross-link added, since that
  page covers Google OAuth only and had no prior pointer to the generic
  OIDC path at all.
- No application code changed. No new tests: the doc's factual claims
  about miniGRC-side behavior (`LOGIN_SCOPES`, `oidc_redirect_uri`, the
  admin route paths) are already pinned by existing test suites
  (`tests/test_oidc.py`, `tests/test_oidc_routes.py`,
  `tests/test_oidc_role_mapping_routes.py`, `tests/test_oidc_db_config.py`
  — 50 tests, all still passing unchanged) — duplicating that coverage
  for a documentation-only change would be redundant, not thorough.

## Verification

- Existing OIDC test suites re-run and confirmed green (50 passed, 0
  failed) — proves this documentation-only change didn't accidentally
  touch or need to touch application behavior.
- Every miniGRC-side route/property/constant cited in the new doc was
  read directly from current source, not assumed from the issue body or
  from memory of how generic OIDC "typically" works.
- No headless/Desktop UAT: documentation-only change, no user-visible
  route/behavior change, per `.agent/TESTING.md` §2.

## Known deferred/untested paths

- Live Authentik interoperability test (discovery, login, group-claim
  mapping against a real instance) — explicitly disclosed as not
  performed, not silently skipped.
- The exact default claim key for Authentik's built-in `profile` scope's
  group data remains unconfirmed from documentation alone; the reference
  guide's custom-mapping recommendation sidesteps needing that answer.
- #20 (IdP metadata as SOC 2 evidence) remains a separate, not-yet-started
  issue this reference intentionally does not build toward yet.
