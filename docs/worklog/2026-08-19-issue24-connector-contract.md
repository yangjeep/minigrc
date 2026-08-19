# Issue #24: Connector manifest, capability, runtime, and result contracts

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Defines the provider-neutral connector contract for the connector
platform epic (#23): a versioned manifest, a capability vocabulary
validated against the three real integration shapes already in this
repository, a normalized result envelope with first-class provenance,
and generalized ingestion functions proving the contract fits Google
Drive (`file.capture`), AWS CloudTrail/IAM (`configuration.snapshot`),
and Google Workspace Directory (`identity.population`) without
rewriting any of their provider-calling logic. See
`docs/superpowers/specs/2026-08-19-issue24-connector-contract-design.md`
for the full inventory/reasoning.

## What changed

- `app/connectors/manifest.py` (new) — `ConnectorManifest`,
  `ConfigFieldSpec`, `CAPABILITIES` vocabulary, `validate_manifest`,
  `check_compatibility` (pure version-bounds check against
  `CORE_CONTRACT_VERSION`).
- `app/connectors/result.py` (new) — `ConnectorProvenance`,
  `ConnectorResult`, `validate_result` (capability-overclaiming check,
  status vocabulary, payload size bound, secret-shaped-key scan as
  defense-in-depth).
- `app/connectors/ingestion.py` (new) — the only path from a result
  into domain state: `ingest_configuration_snapshot` (generalizes
  `app.aws_connector.build_evidence_snapshot`),
  `ingest_identity_population` (generalizes
  `app.google_workspace_directory.sync_directory_users`),
  `ingest_file_capture` (thin wrapper around the existing
  `app.evidence_repository.capture_evidence_version`).
- `app/connectors/adapters.py` (new) — reference manifests + adapter
  functions for the three existing integrations, proving the contract
  without touching any of `app/google_drive.py`,
  `app/aws_connector.py`, or `app/google_workspace_directory.py`'s
  actual provider-calling logic.

## Deliberate, documented scope decisions

- **The manifest is a Python object, not a database table** —
  connector installation/config/health persistence (#25) and a
  registry (#26) don't exist yet; a DB-backed manifest would be
  premature. No migration in this issue.
- **Live production routes are not rewired to call through this
  contract.** `app/routers/aws_connector.py`,
  `app/routers/google_drive.py`, and the Workspace Directory sync entry
  point keep their existing, already-shipped, already-tested behavior
  unchanged. "Model Google Drive file capture... using the same
  contract" (the issue's own verification wording) is satisfied by
  real, tested adapters proving the shape fits — not by taking on
  regression risk against already-working integrations in this slice.
- **Two result shapes, not one forced-generic shape**: `ConnectorResult`
  (JSON-fact, for `configuration.snapshot`/`identity.population`) vs.
  `ingest_file_capture`'s file-bytes parameters (for `file.capture`) —
  forcing file bytes through a JSON payload field would have been a
  false unification. `ConnectorProvenance` is the piece that genuinely
  unifies both.

## A defect found and fixed during adversarial review

The first draft of `validate_result`'s secret-shaped-key scan used a
plain substring match (`"password" in key`), which would have rejected
the real, already-shipped AWS field name `password_policy_present`
(whether a password policy exists — a legitimate compliance fact, not
a password value). Caught before any route was ever wired to this
function, by testing the scan against the actual field names inventoried
from `app/aws_connector.py` rather than only synthetic examples.
**Fixed** by anchoring the regex on the key's final segment
(`(^|_)(token|secret|password|...)$`) — a key *naming* the secret as its
last segment is flagged; a key merely mentioning the term earlier is
not. Verified against 11 realistic key names (7 that should pass, 4
that should still correctly reject) plus a dedicated regression test
(`test_password_policy_present_is_not_secret_shaped`).

## Test strategy and results

- **Unit** (`tests/test_connector_manifest.py`, 12 tests): valid
  manifest passes; empty/malformed `connector_id`/`version`/
  `capabilities` rejected; unknown capability rejected; duplicate
  config-field name rejected; unsupported config-field type rejected;
  compatible version accepted; below-min and above-max core-version
  both rejected; no upper bound when `max_core_contract_version` is
  `None`.
- **Unit** (`tests/test_connector_result.py`, 8 tests): valid result
  passes; capability overclaiming rejected; invalid status rejected;
  oversized payload rejected; secret-shaped key (top-level and nested)
  rejected; benign payload keys accepted; the `password_policy_present`
  regression above.
- **Integration** (`tests/test_connector_ingestion.py`, 7 tests, moto-
  backed for the file-capture case): AWS check result ingests as an
  `EvidenceSnapshot` with correct source_type/check_key/status/hash;
  undeclared-capability result rejected; directory users ingest as
  `Person` upserts with correct create/update counts; **never deletes
  a person missing from a population sync** (same invariant
  `sync_directory_users` already guarantees); updates an existing
  person's fields correctly; file-capture ingestion forwards
  provenance into `capture_evidence_version` and preserves its
  idempotent-by-hash/no-orphaned-upload behavior; file-capture rejected
  for a manifest that doesn't declare the capability.
- **Unit** (`tests/test_connector_adapters.py`, 5 tests): all three
  reference manifests validate and are compatible; the AWS and
  Workspace Directory adapters round-trip real input shapes without
  loss.
- **Full regression suite**: `pytest -q` — **954 passed, 6 skipped**
  (Postgres-gated), **21 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#24 baseline (922) is exactly this issue's 32 new tests.
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean
  (including the design doc's embedded Python code blocks, which this
  repo's `ruff format` also checks).
- **No headless UAT / browser-E2E layer**: this issue adds no route,
  template, or other user-visible surface — pure backend library code
  consumed by future issues (#25 wiring, #28 reference connector). Per
  `.agent/TESTING.md` §2 ("Pure isolated logic: unit + regression/full
  suite"), headless UAT is not required for this slice. It will be
  required once #25/#28 add an actual user-visible connector-management
  surface.
- **Migration**: none — no new tables/columns.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  found and fixed the secret-shaped-key false-positive above; confirmed
  no ingestion function touches `ControlTest`/`Finding`/any
  effectiveness table (RULES.md §9); confirmed `ingest_file_capture`
  never bypasses `capture_evidence_version`'s existing idempotency/
  orphan-cleanup guarantees; confirmed `ingest_identity_population`
  preserves the "never delete a missing person" invariant and applies
  the exact same email-normalization as the original
  `sync_directory_users`; confirmed retries are safe (file-capture
  idempotent by hash; identity-population idempotent by upsert;
  configuration-snapshot creates a new append-only row per call,
  matching `EvidenceSnapshot`'s existing accepted "no edit/delete"
  design — not a new risk, since the pre-existing AWS route already
  behaves this way today).

## Known deferred/untested paths

See design doc §8: no connector installation/config/health-lifecycle
persistence (#25); no registry/marketplace (#26); no package/signature
verification (#27); no new provider logic for Drive or any other
connector (#28); live production routes not yet retrofitted to call
through this contract (a documented, deliberate scope boundary, not an
oversight).
