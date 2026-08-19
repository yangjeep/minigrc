# Issue #34: Defensible SOC 2 audit/PBC package export

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds `app/audit_package.py::generate_audit_package`, a deterministic
zip-file generator that turns the current `ComplianceScope`'s declared
audit period into a portable PBC package: scope metadata, in-scope
frameworks/requirements, controls, in-period occurrences and control
tests (with exceptions and evidence links), findings (with remediation
updates and retests), the policy versions that were effective during
the period, embedded evidence/policy bytes with SHA-256 verification,
and a `manifest.json` listing every included file's hash. No new table,
no server-side persistence of generated packages — see
`docs/superpowers/specs/2026-08-19-issue34-audit-pbc-package-design.md`
for the full design and every scoping/inclusion rule.

## What changed

- `app/audit_package.py` (new) — `AuditPackageError`,
  `generate_audit_package(session, settings, *, actor_email) -> (bytes, str)`.
  Reuses the exact single-file retrieval mechanisms
  `app/routers/evidence_artifacts.py` (S3 via `app/object_storage.py`)
  and `app/routers/policies.py` (`policy_version_path`) already use —
  this module only adds zip/manifest assembly and the "abort the whole
  export, don't produce a partial one" discipline on any
  missing/corrupt file.
- `app/routers/audit_package.py` (new) — `require_admin` router-level
  (matches `app/routers/audit_log.py`'s "bulk/sensitive data leaves the
  system" convention): `GET /admin/audit-package` (form/status page),
  `POST /admin/audit-package` (generate + stream the zip back, or flash
  a clear error — never a raw 500 — on `AuditPackageError`).
- `app/templates/audit_package/form.html` (new).
- `app/main.py` — registered the router; added "Audit Package" to
  `ADMIN_NAV_ITEMS`.

## Scoping rules applied (full reasoning in the design doc §2.3)

Frameworks/requirements/controls/policies are included in full
(org-wide singulars, not period-specific). `ControlOccurrence` rows are
included when `due_at` **or** `performed_at` falls in the period.
`ControlTest` rows are included when `performed_at` falls in the
period. `PolicyVersion`s included per policy: every version effective
at any point overlapping the period (can be more than one across a
mid-period supersession). `Finding` rows are included when tied to an
in-period test, opened during the period, **or** still open as of the
period's end (so unresolved issues from before the period remain
visible) — tested explicitly for both the "still open" and "closed
before the period, excluded" cases.

## A finding from adversarial review, documented rather than silently shipped

**Person identities are never resolved to a name/email in the export —
only opaque `*_person_id` values are included** (`performed_by_person_id`,
`tester_person_id`, `owner_person_id`). An auditor working only from the
package cannot map "who did this" to a human name without separate
miniGRC access. This is a deliberate PII-minimization decision, not an
oversight — added explicitly to the design doc's known-gaps list during
review rather than left implicit, since the issue/PRD give no explicit
instruction either way and it's a real, easy-to-miss trade-off between
export usefulness and PII exposure in a document meant to leave the
system.

## Test strategy and results

- **Unit** (`tests/test_audit_package.py`, 11 tests): no-scope/no-period
  failures; occurrence/control-test in-period vs. out-of-period
  inclusion; finding inclusion rules (still-open-at-period-end included
  even if opened earlier; closed-before-period excluded); an effective
  policy version embedded with a matching hash; a missing policy file
  on disk aborts the whole export; the manifest lists every embedded
  file with a hash that independently re-verifies against the actual
  bytes, including the manifest's own hash; a missing evidence object
  in S3 aborts the whole export; real evidence bytes embedded with a
  matching hash (moto-backed, mirroring `tests/test_evidence_repository.py`'s
  S3-mocking approach).
- **Routes** (`tests/test_audit_package_routes.py`, 9 tests): the
  scope-incomplete hint; a real generate-and-download round trip
  returning an actual zip; the failure path flashing a clear error, not
  a 500; `require_admin` enforced on both GET and POST across
  operator/reader/auditor roles.
- **Headless UAT (required)** — `tests/uat/test_audit_package.py`:
  builds a fully representative period over a real socket (a control,
  a performed occurrence, an approved-and-effective policy version, a
  control test with a real exception, a finding with a remediation
  update tied to that test), generates the package, and independently
  re-verifies every manifest hash against the embedded bytes plus
  spot-checks the actual JSON content (the exception text, the
  remediation note, the policy's effective lifecycle status); plus a
  reader-403 check. `GRC_UAT_MODE=1 pytest -m uat tests/uat` —
  **17 passed, 1 skipped** (Postgres-gated, pre-existing).
- **Full regression suite**: `pytest -q` — **847 passed, 6 skipped**
  (Postgres-gated), **18 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#34 baseline (827/6/16/2) is exactly this issue's 20 new passing
  tests (11 + 9) + 2 new UAT scenarios — no regressions elsewhere. No
  migration needed (no new tables).
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  confirmed the natural query path never touches `Secret`,
  `GoogleDriveConnection.encrypted_refresh_token`,
  `AwsConnection.encrypted_external_id`, or either `secret_id` FK;
  confirmed `require_admin` blocks every other role (tested); confirmed
  duplicate evidence references (an artifact linked to both an
  occurrence and a test) are embedded exactly once (Python `set`
  dedup); confirmed distinct policies/artifacts with identical filenames
  never collide in the zip (id-prefixed paths); found and documented
  the person-identity-resolution gap above; confirmed no scope
  expansion beyond the design doc's explicit boundaries.

## Known deferred/untested paths

- No server-side package history/retention — only an audit-log entry
  recording that a generation happened and its manifest hash.
- No auditor-facing delivery/portal (explicit MVP non-goal, PRD §9).
- No export of a past, no-longer-current scope revision as its own
  period — always the current `ComplianceScope` row's declared period.
- Person identities are not resolved to names/emails in the export (see
  above) — a small, additive change if a future issue asks for it.
