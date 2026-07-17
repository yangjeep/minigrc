# AWS CloudTrail + IAM evidence

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Seventh slice of `feat/startup-compliance-operations`: the AWS connector
(CloudTrail logging posture + basic IAM hygiene — not a CSPM) and the
shared `EvidenceSnapshot` model both AWS checks write to. See ADRs
#21/#22.

## Files Changed

- `app/models.py` — `AwsConnection` (config, updated in place, encrypted
  `external_id`), `EvidenceSnapshot` (immutable, append-only) +
  `EvidenceRequirementMapping`/`EvidenceControlMapping` join tables.
- `app/aws_connector.py` — `build_session` (ambient credential chain,
  optional `AssumeRole`), `test_connection`, `check_cloudtrail` (no
  trail/not logging → fail; logging with gaps → warning; fully configured
  → pass), `check_iam` (root MFA/keys, password policy, per-user MFA/key
  age; capped at 200 users in the payload), `build_evidence_snapshot`
  (shared by the router and the CLI). Every AWS API failure maps to
  `status="unknown"`, never `"fail"`.
- `app/routers/aws_connector.py` — `/connectors/aws` (view, any
  authenticated user), `/edit` + `POST` (admin-only, save settings,
  external ID encrypted, unset field preserves the existing value),
  `/test` and `/run-checks` (admin-only, all CSRF-protected).
- `app/routers/evidence.py` — `/evidence` (list + filter),
  `/evidence/{id}` (detail), map-to-requirement/map-to-control (mirrors
  `ControlRequirementMapping`'s idempotent-resubmit-via-UniqueConstraint
  pattern).
- `app/cli.py` — `aws-run-checks` command (writes the same evidence shape
  as the UI route; suitable for an external cron, no cron infra added).
- `app/routers/placeholders.py` / `app/main.py` — Evidence is now a real
  router, removed from `PLACEHOLDERS`; AWS connector wired in.
- `app/templates/connectors/aws.html`, `aws_edit.html`,
  `app/templates/evidence/list.html`, `detail.html`.
- `pyproject.toml` — added `boto3`.
- `migrations/versions/ae147df9b081_*.py` — four new tables, no backfill
  needed (all new columns/tables).
- `tests/test_aws_connector.py` — 30 tests: `test_connection`/
  `check_cloudtrail`/`check_iam` against every status (pass/fail/warning/
  unknown) via **botocore Stubber** (no real AWS calls), `AssumeRole`
  success/failure, router admin-gating and CSRF, settings save with
  external-ID encryption, evidence snapshot writes + audit events,
  Evidence list/detail/mapping (including duplicate-mapping rejection),
  and CLI success/no-connection/AssumeRole-failure paths.

## Verification

- [x] `pytest` — 190 passed
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] Migration verified on a fresh database (new tables only, no
  backfill).

## Decisions & Alternatives Rejected

- See ADRs #21/#22.
- Caught a real CSRF gap while writing this: the first draft of
  `app/routers/aws_connector.py` was missing `Depends(verify_csrf)` on
  all three POST routes — fixed before any test was written against it,
  per this app's "CSRF on every state-changing form" constraint.
- `AwsConnection` is updated in place (not append-only like
  `GoogleDriveConnection`) — it holds configuration, not an OAuth grant
  with a revocation lifecycle to preserve history for.

## Known Gaps / Follow-ups

- Docs/README pass and the stacked draft PR are the final commit on this
  branch.
- No scheduled/background AWS polling — `run-checks` is an explicit admin
  action or CLI invocation, matching the "explicit Sync now actions, no
  new queue" constraint; an external cron can call
  `python -m app.cli aws-run-checks`.
