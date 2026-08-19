# Issue #13: Evidence populations, sampling, control testing, findings, remediation, retest

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds the SOC 2 Type II assurance/testing lifecycle on top of #11's control
operations: frozen population snapshots, sample selection, control-test
recording (event-sourced, aggregate `"control_test"`), and a finding
lifecycle (open → remediating → retesting → closed, event-sourced,
aggregate `"finding"`) with remediation updates and retest linkage. See
`docs/superpowers/specs/2026-08-19-issue13-assurance-testing-findings-design.md`
for the full design, repository-reality inventory, and the several places
this design deliberately narrows the issue wording with reasoning
(population/sample are plain rows not event-sourced; `Finding` stays a
separate, un-prefixed concept from `Risk`; no sample-size algorithm; no
per-item exception UI).

## What changed

- `app/models.py` — `ControlTestPopulation`/`ControlTestPopulationItem`
  (plain, frozen-once rows), `ControlTestSample`/`ControlTestSampleItem`
  (plain), `ControlTest`/`ControlTestException`/`ControlTestEvidenceArtifact`
  (event-sourced projection, growing over time like `ControlOccurrence`),
  `Finding`/`FindingRemediationUpdate`/`FindingRetest` (event-sourced
  projection, rich state machine like `PolicyVersion`).
- `app/control_tests.py` (new) — `freeze_population`, `draw_sample`,
  `record_test`, `link_test_evidence`, `rebuild_control_test_projection`.
- `app/finding_lifecycle.py` (new) — `open_finding`,
  `record_remediation_update`, `request_retest`, `record_retest`,
  `close_finding`, `rebuild_finding_projection`.
- `app/routers/control_tests.py`, `app/routers/findings.py` (new) —
  `require_login`/`require_write_access`/`verify_csrf` (#37 convention).
- `app/templates/control_tests/*.html`, `app/templates/findings/*.html`
  (new) — populations list/new/detail, test list/new/detail, finding
  list/new/detail with remediation/retest/close forms.
- New migration `a7c2e9f1b3d5` — nine brand-new tables, no data backfill.

## Bugs found and fixed during adversarial testing (not shipped with the gap)

1. **Reset functions used the Core `Table.delete()` construct instead of
   the ORM-synchronizing `sqlalchemy.delete()`.** `_reset_control_tests`/
   `_reset_findings` originally called `ControlTest.__table__.delete()`
   etc. — this deletes rows at the SQL level but never expunges matching
   objects from the session's identity map, so a subsequent
   `session.add(ControlTest(id=<same id>, ...))` during
   `rebuild_control_test_projection`/`rebuild_finding_projection` produced
   a `SAWarning` ("New instance ... conflicts with persistent instance")
   and silently replaced the stale in-memory object rather than cleanly
   re-inserting — caught immediately by running the new rebuild tests
   with `-W error::sqlalchemy.exc.SAWarning`, which is now how this
   project verifies rebuild functions (see `app/control_occurrences.py`/
   `app/compliance_scope.py` for the correct `delete(Model)` precedent
   this should have matched from the start). Fixed by importing and using
   `sqlalchemy.delete(Model)` instead, matching every other rebuild
   function in the codebase.
2. **`record_retest`'s docstring claimed a lineage invariant it never
   actually validated.** The original implementation only checked
   `finding.status == "retesting"` before linking a retest — nothing
   verified `test.retest_of_test_id` actually pointed back at the
   finding's `source_test_id`, despite the docstring claiming it must.
   Fixed by adding the check, and extended it further: a finding with no
   `source_test_id` but a `control_id` now also rejects a retest test
   from an unrelated control (only a finding with neither has nothing to
   validate lineage against). Regression tests added for both the
   original gap and the extended case.
3. **`record_test` never validated that a given sample actually belonged
   to the control being tested.** A caller could record a test against
   control A while passing a `sample` drawn from control B's population —
   nothing rejected the mismatch. Fixed by looking up the sample's
   population and comparing `population.control_id` to the control being
   tested, with a regression test.

## Known, accepted, documented gaps (not fixed — judged low severity/likelihood)

- `open_finding`/`create_finding` does not cross-validate that a supplied
  `source_test_id` actually belongs to a supplied `control_id` when both
  are given independently. In normal navigation the "Open a finding from
  this test" link pre-fills both consistently; a directly-crafted
  inconsistent form submission would produce confusing (but not
  security- or history-corrupting) metadata, not a defect that bypasses
  authorization or fabricates a compliance conclusion.
- Neither population/sample creation, test recording, nor any finding
  transition uses an idempotency key — a double-submit could create a
  redundant row/event. For finding transitions specifically, the
  status-gated preconditions catch most double-submits as a clean
  rejection (e.g. closing an already-closed finding), which is a
  stronger protection than #30/#32's equivalent bootstrap-action gaps.
  Matches this project's existing accepted risk tolerance for this class
  of low-stakes, non-destructive, admin-only action.
- No owner-reassignment or reopen-a-closed-finding action.
- No per-item exception UI — the "record a test" form captures exceptions
  as free-text lines (`sample_item_id=None`); the domain function fully
  supports pinning an exception to a specific sample item for future/API
  callers.
- No `EvidenceSnapshot` (connector-sourced) linkage for tests — only
  `EvidenceArtifactVersion`, matching what #32 was explicitly built to
  anticipate.
- No bulk/CSV import for populations or findings.

## Test strategy and results

- **Unit** (`tests/test_control_tests.py`, 14 tests): population freezing
  and its immutability against later occurrences, sample-item validation,
  test recording (valid results, exception ownership, sample/control
  cross-reference validation), retest control-matching, evidence linking
  + projection rebuild.
- **Unit** (`tests/test_finding_lifecycle.py`, 19 tests): every state
  transition's precondition and rejection path, remediation-update
  auto-transition, retest lineage enforcement (both the source-test-id
  and control-id cases), retest never auto-closing, closure from any
  non-closed status, closing preserves the original event, projection
  rebuild.
- **Routes/regression** (`tests/test_control_tests_routes.py`, 16 tests;
  `tests/test_findings_routes.py`, 12 tests, including a full HTTP-level
  remediation → retest → close journey with correct lineage): RBAC
  (reader/auditor 403, no state change), 404s, invalid-reference
  rejection, evidence linking.
- **Migration** (`tests/test_control_test_migration.py`): SQLite (4
  tests, including a CHECK-constraint proof); PostgreSQL-gated (1 test,
  skipped locally, runs in CI).
- **Headless UAT (required)** — `tests/uat/test_assurance.py`: the full
  chain from the issue's own verification section (recurring control
  occurrence → frozen population → sample → test with one exception →
  finding → remediation update → retest → close) over a real socket,
  plus a reader-403 check. `GRC_UAT_MODE=1 pytest -m uat tests/uat` —
  **12 passed, 1 skipped** (Postgres-gated, pre-existing).
- **Full regression suite**: `pytest -q` — **787 passed, 6 skipped**
  (Postgres-gated), **13 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#13 baseline (722/5/11/2) is exactly this issue's 65 new passing
  tests + 1 new Postgres-gated skip + 2 new UAT scenarios — no
  regressions elsewhere.
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  confirmed reader/auditor cannot mutate any new state (tested); found
  and fixed the three defects listed above; confirmed `DomainEvent`
  immutability holds for both new aggregate types (inherited, unchanged);
  confirmed the migration fabricates no historical facts (brand-new
  tables, no backfill); confirmed no secrets/PII exposure beyond fields
  already present on `Person`; confirmed no scope expansion beyond the
  design doc's explicit boundaries.
