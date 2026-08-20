# Issue #48: unified self-hosted deployment/configuration contract

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** documentation

## Summary

Ties the already-tracked configuration pieces — DB backend (#22),
evidence storage (#32), auth mode(s) (#16-20), BYOK (#15) — into one
document, `docs/deployment/configuration-contract.md`. See
`docs/superpowers/specs/2026-08-20-issue48-deployment-configuration-contract-design.md`
for the full repository-reality investigation.

## The central finding: no new validation needed

Direct inspection of every axis' actual merged code found that every
optional-feature axis (evidence storage, Google OAuth, generic OIDC,
Google Drive) already implements the **same deliberate, tested pattern**:
an incomplete config group degrades gracefully to "disabled" at the point
of use (404/503/"unconfigured"), never a startup crash — each module's
own docstring says so explicitly (`app/google_oidc_config.py`:
"a broken Google OAuth config must never crash a request or lock out
local/break-glass login").

The database backend axis (#22) is genuinely different in kind: two
settings (`DATABASE_URL`, `GRC_DATABASE_PATH`) can each independently
and *completely* select a database, so coexistence is a real "which one
did you mean?" ambiguity with no safe default — exactly why it alone
gets a hard startup failure
(`app/config.py::reject_ambiguous_database_config`).

**Retrofitting a new hard-fail check onto the already-graceful axes would
contradict, not complete, an existing architectural pattern spanning four
independent modules.** No new runtime validation was added. The
acceptance criterion "any genuinely ambiguous/invalid combination newly
identified gets explicit startup-time validation" is satisfied by an
empty set, stated explicitly in the design doc rather than silently
assumed or fabricated.

## Also confirmed: BYOK/AI (#15) has not landed

`app/config.py`'s complete `Settings` class has zero AI-provider/BYOK
fields. The issue's own execution prompt assumed "#15 defines its own
config requirements" — that describes a future state, not current
repository reality. The new doc says so plainly and points at #15 rather
than fabricating settings that don't exist.

## What changed

- `docs/deployment/configuration-contract.md` (new): the four-axis
  contract, the shared graceful-degradation pattern named explicitly for
  the first time, and the one real cross-cutting operational constraint
  (SQLite-file deployments must run with exactly one web replica —
  already documented in `kubernetes.md`, cross-linked here rather than
  duplicated).
- `docs/architecture.md`: one cross-link added near the existing
  authentication section.
- `tests/test_deployment_configuration_contract.py` (new, 11 tests): a
  living-proof suite exercising the actual `Settings` properties the doc
  describes — the DB-backend XOR (both-set rejected, either-alone valid,
  neither-set falls back to local SQLite), evidence storage
  all-or-nothing (including a partial-config case proving it degrades
  rather than crashes), auth modes being independently stackable (both
  Google OAuth and generic OIDC configured simultaneously is valid, not
  ambiguous), and a test that fails loudly the moment BYOK/AI settings
  fields are ever added (a deliberate tripwire to keep this doc honest).

## Test strategy and results

- **Unit** (`tests/test_deployment_configuration_contract.py`, 11
  tests): PASS, see above.
- **Regression**: full `pytest -q` — see PR for final count; no
  application code was changed, only documentation and one new isolated
  test file.
- **Lint/format**: clean.
- **No headless/Desktop UAT**: documentation-only change with a
  verification test, no user-visible route/behavior change — per
  `.agent/TESTING.md` §2's "Documentation-only change: no headless or
  Desktop UAT required unless it changes the operating/test contract
  itself," which it doesn't.

## Known deferred/untested paths

- The BYOK/AI section of the contract doc is a placeholder pointing at
  #15 — will need real content once that issue lands.
- #19 (Authentik reference deployment) and #20 (IdP identity metadata as
  evidence) remain open; the contract doc cross-links them rather than
  duplicating their scope.
