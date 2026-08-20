# Issue #27: Connector security hardening, permission review, verification, and compatibility lifecycle

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature/security

## Summary

Hardens the connector platform (#24/#25/#26) against the real gaps
repository state actually has today — registry identity confusion,
malformed checksum metadata, invisible provider-permission scope, a
missing upgrade/rollback path for installed connector instances, and
already-installed instances silently outliving their registry entry's
deprecation. See
`docs/superpowers/specs/2026-08-20-issue27-connector-security-hardening-design.md`
for the full threat model and design.

## A scope decision grounded in repository reality, not the issue's abstract threat list

The issue's execution prompt lists threats like "malicious connector
code," "supply-chain/package substitution," and "checksum/signature
verification" that assume connector *packages* are fetched and loaded
at runtime. Repository inspection confirmed this infrastructure does
not exist: every connector today is first-party Python code in
`app/connectors/adapters.py`, and `RegistryEntry.package_location` is
schema-present but never resolved/imported by anything (#26 §9,
reconfirmed unchanged). Building a real signature/checksum-of-
downloaded-bytes pipeline against a fetch step that doesn't exist would
be exactly the speculative infrastructure the issue's own execution
prompt says to avoid ("do not build a complex code-signing PKI if
repository/package reality does not justify it"). This issue instead
hardens the parts of the platform that are genuinely live today
(`registry.json` tampering, capability escalation, upgrade/downgrade,
permission visibility, deprecation) and documents the rest as residual
risk (design doc §7) rather than simulating protection against nothing.

## What changed

- `app/connector_registry.py`:
  - `load_registry` now rejects a `connector_id` claimed by more than
    one distinct `publisher` across entries (identity confusion) and a
    `checksum_sha256` that isn't a well-formed 64-char hex digest.
  - New `PermissionReview` + `build_permission_review(entry)` — a
    reviewable, risk-flagged summary of a registry entry's
    capabilities, required permissions, and a `broad_permission_warnings`
    subset flagged by a narrow, documented heuristic
    (`_BROAD_PERMISSION_MARKERS`).
  - New `list_deprecated_installed_instances(session, entries)` —
    reporting-only; finds installed `ConnectorInstance` rows whose
    `(connector_id, manifest_version)` now matches a `deprecated=True`
    registry entry. Never disables/removes anything.
- `app/connector_lifecycle.py`:
  - New `ConnectorUpgradePlan` + `plan_connector_instance_upgrade` /
    `apply_connector_instance_upgrade` — the first upgrade/rollback
    path for an installed `ConnectorInstance` (none existed before this
    issue; `manifest_version` was previously set once at install and
    never changed by any function). Plan is pure/no I/O and mirrors the
    existing `test_connector_instance` plan-then-apply shape. Apply
    fails closed on: identity confusion, core-contract incompatibility,
    a silent downgrade (unless `allow_downgrade=True`, which audits as
    `"rollback"` instead of `"upgrade"`), a stale plan whose
    `from_version` no longer matches the instance's current version,
    and a substituted `to_manifest` that doesn't match what the plan
    actually reviewed (version, capabilities, and required permissions
    all checked, not version alone).

## Findings from self-review (adversarial pass on this issue's own new code)

Both fixed before shipping; see design doc §6.1 for full detail:

1. **Stale-plan/concurrency gap** — `apply_connector_instance_upgrade`
   didn't verify the instance hadn't moved on since the plan was built,
   which could let a stale plan silently move an instance *backward*
   from its real current version while `plan.direction` still read
   `"upgrade"`. Fixed with the same optimistic-concurrency shape
   `app.registers`'s PATCH API already uses (`expected_updated_at`).
   Regression: `test_apply_rejects_stale_plan_after_concurrent_version_change`.
2. **Manifest-substitution gap** — nothing verified the `to_manifest`
   given to `apply_connector_instance_upgrade` was the same one
   `plan_connector_instance_upgrade` had reviewed; checking the version
   string alone would not have been enough, since two manifest objects
   can share a version while differing in capabilities. Fixed by
   capturing the full reviewed capabilities/permissions on the plan and
   verifying an exact match at apply time. Regression:
   `test_apply_rejects_substituted_manifest_not_matching_reviewed_plan`.

## Test strategy and results

- **Registry hardening** (`tests/test_connector_registry.py`, 6 new
  tests): identity confusion across entries rejected; malformed
  checksum rejected, well-formed one accepted; `build_permission_review`
  flags a broad permission and not a narrow one; deprecated-instance
  visibility finds only the instance whose version matches a deprecated
  entry.
- **Upgrade/rollback lifecycle** (`tests/test_connector_lifecycle.py`,
  8 new tests): plan surfaces new capabilities/permissions; apply moves
  the version and audits as `"upgrade"`; identity-confused and
  incompatible-core-version plans rejected; silent downgrade rejected,
  explicit `allow_downgrade=True` succeeds and audits as `"rollback"`;
  stale-plan-after-concurrent-change rejected; substituted-manifest
  rejected.
- **Adversarial verification against a fake malicious connector**: the
  issue's own execution prompt asks for this explicitly. The full-
  lifecycle-path version already exists and passes —
  `test_capability_overclaiming_result_records_error_without_ingesting`
  (from #25, run through the real `run_connector_instance` boundary,
  not just the isolated `validate_result` unit) — reverified as part of
  this issue's own targeted test run rather than re-implemented.
- **Full regression suite**: `pytest -q` — 998 passed, 6 skipped
  (Postgres-gated), 21 deselected (`uat` marker), 2 xfailed (issue #65,
  pre-existing/unrelated), 0 failed. Delta from the pre-#27 baseline
  (984, immediately post-#26) is exactly this issue's 14 new tests (6
  registry + 8 lifecycle, including the 2 added during self-review).
- **Lint/format**: clean (`ruff check .`, `ruff format --check .`,
  including this design doc's embedded code blocks).
- **No headless UAT / no HTTP route**: no user-visible surface in this
  issue, matching #24/#25/#26's own accepted precedent — every new
  function is backend service layer only.
- **No migration**: no schema change. `ConnectorUpgradePlan` and
  `PermissionReview` are plain dataclasses, not ORM models; the upgrade
  path mutates the existing `ConnectorInstance.manifest_version` column
  and writes ordinary `AuditEvent` rows, both already covered by CI's
  `test-postgres` job running the full suite against real PostgreSQL.

## Known deferred/untested paths

See design doc §7 (residual risk, documented rather than hidden):

- No cryptographic verification of connector code exists — every
  connector today is first-party, in-repo source; integrity rests on
  git history, PR review, and the `protect-main` ruleset, not an
  independent signature chain.
- No sandboxing — `verification_status` records what the registry
  asserts, never a code-safety guarantee (#26 §3, unchanged).
- No third-party/dynamic package loading exists yet, so package-
  substitution/tampered-artifact-bytes verification has no real target
  to check against; the `checksum_sha256` field's *shape* is now
  enforced, but verifying it against real bytes is future work once a
  real dynamic-import step exists.
- The broad-permission heuristic (`_BROAD_PERMISSION_MARKERS`) is a
  narrow, documented set of known-risky patterns, not an exhaustive
  risk classifier.
- No HTTP route/admin UI to review permissions, browse deprecation
  warnings, or trigger an upgrade/rollback interactively — same
  accepted backend-only precedent #24/#25/#26 established; deferred to
  whichever issue defines the connector platform's operator-facing
  surface (not #28 either — its own execution prompt only asks to
  demonstrate install/configure/test/capture through #24/#25, not to
  build that UI).
