# Issue #28: Google Drive as the reference evidence/file-capture connector

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Wires Google Drive into the connector platform (#24/#25/#26/#27) as a
real, working `file.capture` connector — replacing the placeholder
manifest/registry entry with an actual execution path into the
S3-compatible evidence repository (#32). See
`docs/superpowers/specs/2026-08-20-issue28-google-drive-reference-connector-design.md`
for the full design.

## A scope decision: two Drive integrations, not one rewritten into the other

Repository inspection found Google Drive already wired into miniGRC
twice, for two different domain concepts:

1. **Policy-document capture** (`app/routers/policies.py`, pre-dating
   the connector platform) — captures a Drive file as the next
   immutable `PolicyVersion` of a specific policy, stored on local
   disk. This is compliance-*document lifecycle* (PRD §5.4), not audit
   evidence, and rewriting it onto the evidence repository would be an
   unrelated, disproportionately risky refactor of a live feature for
   no benefit this issue's acceptance criteria actually ask for.
2. **The evidence repository / S3-compatible object storage boundary**
   (`app/evidence_repository.py`, #32, already merged, event-sourced,
   idempotent-by-content-hash) — this is what #28's acceptance criteria
   are actually shaped around.

This issue adds a new, additional evidence-capture path into (2),
reusing the same OAuth connection and provider client, and leaves (1)
untouched.

Also decided explicitly (design doc §2): Google Drive's real OAuth
credential stays in the pre-existing `GoogleDriveConnection` singleton
table rather than being force-migrated into `ConnectorInstance.secret_ref_json`
— `ConnectorInstance`/`ConnectorExecution` still participate (that
ledger is what "operates through the generic connector platform"
actually requires), but with an empty `config_schema` and no secret
ever stored there, matching `google_drive_manifest()`'s own existing
declaration.

## What changed

- `app/connectors/google_drive_capture.py` (new):
  - `get_or_create_google_drive_instance` — idempotently ensures
    exactly one `google_drive` `ConnectorInstance` exists (Drive is a
    singleton connection; unlike AWS, duplicates would be ambiguous).
  - `run_google_drive_capture` — the `file.capture` execution/ingestion
    boundary: `get_file_metadata` → extension allow-list check →
    `list_revisions` (best-effort, for `source_modified_at`) →
    `download_file_content` → `capture_raw_bytes` →
    `validate_evidence_content` → `build_s3_client` →
    `ingest_file_capture` (#24) → `capture_evidence_version` (#32).
    Mirrors `run_connector_instance`'s shape (disabled-instance
    rejection, idempotency-key ledger, rollback-then-record-failure on
    any error) specialized for file bytes, which
    `run_connector_instance`'s own JSON-result dispatch deliberately
    excludes.

No schema/migration change — every model this issue touches
(`ConnectorInstance`, `ConnectorExecution`, `EvidenceArtifact`,
`EvidenceArtifactVersion`, `GoogleDriveConnection`) already exists.

## Findings from self-review (adversarial pass on this issue's own new code)

Both fixed before shipping; see design doc §4.1/§4.2 for full detail:

1. **Repeated-capture bug (real, reproduced)** — testing a realistic
   "capture the same artifact twice in one session" sequence hit a
   genuine `IntegrityError`: this app's session factory uses
   `expire_on_commit=False`, so a held `EvidenceArtifact` object's
   `.versions` relationship — which `capture_evidence_version` reads to
   compute the next version number — goes stale across repeated
   captures in one session unless something refreshes it.
   `tests/test_evidence_repository.py`'s own existing tests already
   work around this with `session.refresh(artifact)` between calls,
   established as a caller responsibility; fixed by having
   `run_google_drive_capture` do this itself so future callers (a
   batch job, a route) don't have to remember. Regression:
   `test_changed_revision_creates_new_immutable_version`.
2. **SSRF guard gap** — `run_google_drive_capture` originally passed
   `drive_file_id` straight into Drive API calls without the existing,
   SSRF-safe `parse_drive_file_id` validation the policy-Drive routes
   already apply before their equivalent calls. Since this issue adds
   no HTTP route (backend-only, matching #24-27's precedent), nothing
   would have guaranteed a future caller validates first. Fixed by
   calling `parse_drive_file_id` at the top of the function itself.
   Regression: `test_malformed_drive_file_id_rejected_before_any_drive_call`.

## Test strategy and results

- **Unit/integration** (`tests/test_connector_google_drive_capture.py`,
  11 new tests, mocking Drive's HTTP layer the same way
  `tests/test_google_drive.py` already does and real `moto`-backed S3
  matching `tests/test_connector_ingestion.py`'s existing pattern):
  singleton-instance install idempotency; binary file capture with
  full provenance (connection, file ID, revision ID, modified time);
  same-content retry is a safe no-op; repeated idempotency key returns
  the existing execution without recalling Drive; a changed revision
  creates a new immutable version while the prior one is untouched;
  disabled instance rejected before any Drive call; malformed
  `drive_file_id` rejected before any Drive call; a Drive error records
  a failed execution without ingesting anything; an unsupported file
  type and content that doesn't match its declared extension are both
  rejected before any S3 upload; removing the `ConnectorInstance`
  afterward leaves previously captured evidence fully intact.
- **Full regression suite**: `pytest -q` — 1009 passed, 6 skipped
  (Postgres-gated), 21 deselected (`uat` marker), 2 xfailed (issue #65,
  pre-existing/unrelated), 0 failed. Delta from the pre-#28 baseline
  (998, immediately post-#27) is exactly this issue's 11 new tests.
- **Lint/format**: clean (`ruff check .`, `ruff format --check .`,
  including this design doc's embedded code blocks).
- **No headless UAT / no HTTP route**: no user-visible surface in this
  issue, matching #24/#25/#26/#27's own accepted precedent — this
  issue's own execution prompt only asks to "demonstrate
  install/configure/test/capture through #24/#25," which the
  integration tests above do directly.
- **No migration**: no schema change; every model reused already
  exists.
- **Pre-existing policy-document Drive capture path**: untouched — its
  own existing test suite (`tests/test_google_drive.py`) is unaffected
  and still passes as part of the full regression run above.

## Known deferred/untested paths

- No HTTP route/admin UI to trigger a Drive capture through this new
  path interactively — same accepted backend-only precedent #24-27
  established; this issue proves the connector executes correctly
  through the platform, not an operator-facing surface for it.
- No scheduled/periodic re-capture — confirmed no scheduler
  infrastructure exists anywhere in this repository to reuse (per
  inspection), and #28's own scope wording only asks for "the smallest
  scheduled/refresh behavior justified by existing scheduler patterns"
  — with none existing, manual/on-demand triggering (as tested) is the
  repository-grounded answer.
- Real Drive API behavior is exercised via mocks (matching
  `tests/test_google_drive.py`'s own established convention for the
  same provider boundary) — the miniGRC side of the integration
  (`ingest_file_capture` → `capture_evidence_version` → real
  `moto`-backed S3) executes through its real boundary, per
  `.agent/TESTING.md` §1.3.
