# Issue #20: IdP identity metadata as access-control evidence

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a provider-neutral capture of miniGRC's own already-collected
record of who has authenticated via the configured generic OIDC
provider (#17/#18) and their currently-mapped role, as access-control
evidence for a quarterly access review — reusing `EvidenceSnapshot`
(#32) directly rather than a new evidence model. See
`docs/superpowers/specs/2026-08-20-issue20-idp-identity-evidence-design.md`
for the full repository-reality investigation.

## Two scope-reshaping findings from direct inspection

1. **No standards-based OIDC directory/admin API exists.** Discovery/
   token/userinfo endpoints only ever return the currently-authenticating
   user's own claims — there is no generic "list every user/group"
   endpoint. Building one would mean a provider-specific admin API per
   IdP, exactly what the issue's own non-goals rule out. The identity
   population this issue captures instead is miniGRC's own record
   (`ExternalIdentity` + each linked `User`'s currently-mapped role) —
   which directly matches the issue's own first two listed use cases and
   needs zero provider-specific connector code.
2. **`ControlTestPopulationItem` (#13) is hardcoded to
   `control_occurrence_id`**, not a generic/polymorphic item reference —
   reusing it for an identity population would mean a real schema change
   to #13's already-shipped model. The fourth listed use case ("use a
   frozen snapshot as a #13 sampling population") is explicitly deferred
   as a documented gap, not silently forced into an ill-fitting schema.

## What changed

- `app/oidc_identity_evidence.py` (new): `capture_oidc_identity_population_snapshot`
  — a second producer of `EvidenceSnapshot`, alongside
  `app/aws_connector.py::build_evidence_snapshot`, following its exact
  construction pattern. Captures issuer/subject/user_id/email/role/
  role_source/user_status/linked_at per linked identity — deliberately
  never raw provider group claims (only the derived role persists
  durably anywhere in this codebase) and never any token/secret (neither
  source model has such a field to begin with).
- `app/routers/admin_authentication.py`: `POST /admin/authentication/oidc/identity-snapshot`
  — admin-only (stricter than the general `require_write_access` bar
  most evidence-linking actions use, given the sensitivity of identity/
  access-population data).
- `app/templates/admin/authentication/oidc.html`: a capture button.
- No new readiness code: `app/readiness.py`'s existing
  occurrence-missing-evidence check already covers an access-review
  occurrence using this evidence type, once linked through the existing
  `ControlOccurrenceEvidence` mechanism — proved by a test, not assumed.

## Test strategy and results

- **Unit** (`tests/test_oidc_identity_evidence.py`, 7 tests): payload
  correctness and exact key-set (nothing extra, nothing missing); zero-
  identities case; no secret/token substring ever present; hash
  determinism; historical immutability (a role change after capture
  never touches the already-stored snapshot); linking evidence never
  marks an occurrence performed; the #14 readiness "comes for free"
  proof (gap appears when unlinked, disappears once linked).
- **Browser/E2E** (`tests/test_admin_oidc_identity_snapshot_route.py`,
  3 tests): admin-only enforcement (403 for a non-admin), a real capture
  POST creating the `EvidenceSnapshot` row, and the captured row being
  visible on the real `/evidence` list page.
- **Headless UAT** (`tests/uat/test_oidc_identity_evidence.py`, 3
  scenarios, PASS): this issue *does* add a real new user-visible POST
  route and a new button, whose resulting evidence is later selected
  through a different real route's form field
  (`/occurrences/{id}/perform`'s dropdown) — not the "no route at all"
  shape that would have justified skipping headless UAT. Scenario 1: an
  admin captures the snapshot, an operator sees it in the evidence
  dropdown while performing a real access-review occurrence and links
  it — the full real HTTP flow, not just the service layer. Scenario 2:
  capturing a snapshot alone never touches any `ControlOccurrence` row.
  Scenario 3: a non-admin is rejected (403). **A real bug in my own
  first-draft UAT scenario, not application code**, was found and fixed
  during this: `create_control_occurrence` redirects to the control
  detail page, not a standalone occurrence page — the scenario's first
  draft assumed the wrong redirect target and was corrected after
  actually running it, per this session's habit of never trusting an
  E2E test until it's actually been executed against the real server.
- **Regression**: full `pytest -q` — see PR for final count.
- **Lint/format**: clean.
- **SQLite/PostgreSQL**: no migration in this issue — pure reuse of the
  existing `EvidenceSnapshot` table with plain SQLAlchemy Core/ORM
  queries throughout.

## Known deferred/untested paths

- #13 population/sampling reuse of an identity snapshot: explicitly
  deferred, requires its own schema decision.
- No live external-IdP directory sync of any kind, by design — this was
  never the goal.
- Claude Desktop UAT: PENDING — requires a running deployment with a
  real generic-OIDC-linked account, not runnable from this
  implementation session.
