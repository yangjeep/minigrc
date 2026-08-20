# Issue #19: Authentik reference deployment and OIDC interoperability verification

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** documentation

## Summary

Adds `docs/deployment/authentik-reference.md`, a documented Authentik
OAuth2/OIDC provider configuration for miniGRC's generic OIDC login
(#17/#18), with no Authentik-specific application code. See
`docs/superpowers/specs/2026-08-20-issue19-authentik-reference-design.md`
for the full inspection/verification record.

## Authentik configuration summary

- OAuth2 Provider: confidential client, asymmetric (RSA/EC) signing key
  required (miniGRC verifies signatures via JWKS only — no symmetric-key
  support), strict redirect URI `https://<minigrc-host>/auth/oidc/callback`.
- Application bound to the provider, gating which Authentik users/groups
  may authenticate.
- Per-application issuer URL (`/application/o/<slug>/`) — not the bare
  Authentik host; miniGRC rejects a discovery document whose own `issuer`
  field doesn't exactly match what was configured.

## Official capabilities verified (docs.goauthentik.io, 2026-08)

- Standard OIDC discovery document shape confirmed.
- Built-in `profile` scope documented as including "group membership" —
  confirmed via direct documentation quote.
- Exact claim key name for that group data: **not confirmed** from
  official docs alone.

## Interoperability result

**Documentation/config-level verification only.** No live Authentik
instance was available in this implementation environment to run an
actual discovery/login/group-claim round-trip — disclosed explicitly in
the reference doc itself, per the issue's own allowance for this case.

## Group-mapping example

A custom Scope Mapping (`groups` scope name, returning
`{"groups": [group.name for group in request.user.ak_groups.all()]}`)
is recommended over relying on the unverified default `profile` scope
claim key — sourced via documentation lookup, flagged in the doc as a
starting point to verify against the operator's actual deployed
Authentik version, not asserted as confirmed fact.

## Security/deployment notes

- No secrets embedded in the guide — every credential field is described
  by name/purpose only ("the OAuth2 Provider's generated Client Secret"),
  never a real value.
- Confirmed by direct code reading (not assumed) that local break-glass
  login remains fully independent of Authentik's configuration state —
  `resolve_oidc_config` degrades to "unusable" rather than raising on any
  discovery/JWKS failure, and the login route 404s cleanly when
  unconfigured.

## What changed

- `docs/deployment/authentik-reference.md` (new).
- `docs/deployment/authentication.md`: one cross-link added (that page
  covered Google OAuth only, with no prior pointer to the generic OIDC
  path).
- No application code changed, no new tests — the doc's miniGRC-side
  factual claims (`LOGIN_SCOPES`, `oidc_redirect_uri`, admin route paths)
  are already pinned by existing suites.

## Test report

- **Regression**: `tests/test_oidc.py`, `tests/test_oidc_routes.py`,
  `tests/test_oidc_role_mapping_routes.py`, `tests/test_oidc_db_config.py`
  — 50 passed, 0 failed, confirming zero impact from this
  documentation-only change.
- **Full suite**: see PR for final count.
- **Lint/format**: clean (no code changes; ruff has nothing new to check
  beyond formatting, which doesn't apply to markdown).
- **No headless/Desktop UAT**: documentation-only change, no user-visible
  route/behavior change, per `.agent/TESTING.md` §2.

## Remaining blockers / known gaps

- Live Authentik interoperability test not performed — explicitly
  disclosed, not silently skipped. A future session with cluster/Docker
  access should complete it and firm up the group-claim-key finding.
- Cross-reference note: this doc links to
  `docs/deployment/configuration-contract.md` (issue #48) and that
  page's own authentication-mode section links back here — both PRs are
  currently open/unmerged on separate branches (#48 is PR #91, this is a
  new branch for #19). The links resolve correctly once both merge to
  `main`; until then, whichever merges first will have a temporarily
  dead relative link in the other's direction. Purely cosmetic (markdown
  cross-reference, not a functional dependency) — noted for the record,
  not a blocker.
- #20 (IdP metadata as SOC 2 evidence) remains open, intentionally not
  started by this issue.
