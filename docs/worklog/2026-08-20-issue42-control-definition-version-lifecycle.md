# Issue #42: Extend the compliance-version lifecycle to control definitions

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Extends #31's event-backed policy-version approval lifecycle pattern
to `InternalControl`'s own *definition* history — distinct from #11's
`ControlOccurrence`, which is the control's recurring *operational*
history. Closes the exact gap `docs/superpowers/specs/2026-08-15-issue11-
control-operations-lifecycle-design.md` §10 risk #4 named: "no way to
reconstruct what a control's definition said as of occurrence time."
See `docs/superpowers/specs/2026-08-20-issue42-control-definition-version-lifecycle-design.md`
for the full design.

This issue closes out the last unblocked child of two epics: #21
(P0, event-centric persistence — this was its only remaining open
child besides #44, which is explicitly deferred until a real
read-performance need exists) and a further step toward #10 (SOC 2
Type II-first).

## Scope decisions

1. **`InternalControlVersion` is fully event-sourced from creation**,
   unlike `PolicyVersion` (#31), which pre-existed as a plain-inserted
   file-capture row before #31 added lifecycle columns to it. This is
   a brand-new table with no pre-existing rows to preserve — same
   reasoning `EvidenceArtifactVersion` (#32) already applied.
2. **No existing route needed rewiring.** Repository inspection found
   no route anywhere edits `InternalControl.name`/`description`/
   `status`/`review_frequency` after creation — those fields are only
   ever set by `app/seed.py` and `app/starter_controls.py`. Introducing
   the version-lifecycle mechanism required no changes to any existing
   edit path, since none exists; `InternalControl`'s own plain columns
   stay untouched by this module, same precedent as `Policy.title`/
   `description` relative to `PolicyVersion`.
3. **The "procedure" decision is documented, not implemented.**
   Grepped `app/models.py`, `docs/product-scope.md`, and
   `docs/domain/domain-model.md` — no "procedure as a governed
   document" concept exists anywhere; the only hit
   (`ControlTest.procedure_description`) is an unrelated, already-
   settled concept (a test's methodology narrative). Decision: `Policy`
   extended with a `document_type` field would be sufficient *if* a
   real need arises later — no such field is added now, since nothing
   currently needs to author a "procedure"-flavored document and doing
   so speculatively would be exactly the infrastructure-before-a-
   concrete-use RULES.md warns against.
4. **Audit/PBC package export (`app/audit_package.py`) is not updated
   in this issue.** It's a legitimate downstream consumer of this new
   capability (the PRD's §5.13 "controls and historical effective
   versions" applies directly), but wiring `control_definition_version_id`
   into the export is a separable, independent change that doesn't
   require touching this issue's schema/lifecycle code again — noted
   as a known follow-on rather than expanding this issue's scope.

## What changed

- `app/models.py`: `InternalControlVersion` (new, fully event-sourced,
  mirrors `PolicyVersion`'s lifecycle-column shape:
  draft/in_review/approved/effective/superseded/withdrawn, partial
  unique index for "at most one effective version per control").
  `InternalControl` gains `versions`/`effective_version`.
  `ControlOccurrence` gains nullable `control_definition_version_id`.
- `app/control_definition_lifecycle.py` (new): `draft_control_definition_version`,
  `submit_for_review`, `review_version`, `make_version_effective`,
  `withdraw_version`, `bootstrap_initial_effective_version` (draft →
  submit → approve → effective in one call, for system-generated
  controls), plus projectors and `rebuild_internal_control_version_projection`.
- `app/control_occurrences.py`: `generate_occurrences`/
  `record_occurrence_manually` now snapshot the control's currently-
  effective version id into each new occurrence's payload —
  `NULL` if none exists yet (an honest gap, never fabricated).
- `app/starter_controls.py`: every starter-generated control now also
  gets a real, system-tagged effective version via
  `bootstrap_initial_effective_version` — the migration backfill below
  only covers controls that exist at migration time, not future
  onboardings.
- `migrations/versions/96fd3c35a6ae_...py`: new `internal_control_versions`
  table, new `control_occurrences.control_definition_version_id`
  column, and a deterministic per-control backfill (one effective
  version snapshotting current fields/mappings per existing control,
  `actor_type="migration"`, mirroring #31/#41's established
  convention).

## Findings from self-review (adversarial pass on this issue's own new code)

Three real bugs surfaced while writing this module's own tests, all
fixed and all now regression-tested — see design doc §3.1 for full
detail on the two lifecycle-module bugs:

1. **Stale-relationship bug in `draft_control_definition_version`**
   (same class as #28's `run_google_drive_capture` fix): this app's
   `expire_on_commit=False` session factory means a long-lived
   `control` object's `.versions`/`.mappings` relationships go stale
   across repeated drafts against the same control in one session.
   Fixed with `session.refresh(control)`. Regression:
   `test_version_numbers_increment_per_control`.
2. **A real rebuild-replay ordering bug, unique to this module** (not
   present in #31, since `PolicyVersion`'s projectors never flush at
   all — `InternalControlVersion`'s `_project_drafted` must flush to
   create each new row, and that unrelated flush can batch two
   different aggregates' pending transitions together in an order the
   partial unique index doesn't like during replay). Reproduced
   deterministically (5/5 full-file runs) before the fix, 5/5 clean
   after. Fixed by having `_project_superseded`/`_project_made_effective`
   each flush their own mutation immediately rather than deferring to
   one flush at the end of `make_version_effective` (which only ever
   protected the live-call path). Regression:
   `test_rebuild_internal_control_version_projection_reproduces_state`.
3. **The same stale-relationship class again, in
   `generate_occurrences`/`record_occurrence_manually`**: `control.effective_version`
   read a `.versions` collection cached empty from an earlier draft
   call, before any version existed, and never refreshed. Fixed the
   same way. Regressions:
   `test_control_definition_version_id_snapshotted_not_live_join`,
   `test_record_occurrence_manually_snapshots_control_definition_version`.

## Test strategy and results

- **Lifecycle** (`tests/test_control_definition_lifecycle.py`, 24
  tests): draft snapshot defaults/overrides/mapping-set, version
  numbering, full state machine (submit/review/approve/effective/
  supersede/withdraw) with the same invalid-transition and terminal-
  state coverage `test_policy_lifecycle.py` has, `bootstrap_initial_effective_version`,
  rebuild-reproduces-state, and the DB partial-unique-index constraint
  proven directly at the schema level.
- **Migration/backfill** (`tests/test_control_definition_lifecycle_migration.py`,
  7 tests + 1 Postgres-skip): existing control gets one effective
  version; mapping-set snapshot correctness (including the empty
  case); bootstrap events tagged `actor_type="migration"`; old-status
  values rejected by the CHECK constraint after migration;
  upgrade→downgrade→upgrade round-trip doesn't duplicate events or
  lose the projection row; the partial unique index enforced after
  migration; live-PostgreSQL backfill test (`test_backfill_against_postgres`,
  skipped locally — no `TEST_DATABASE_URL`/Postgres available in this
  environment; exercised by CI's `test-postgres` job, matching #31's
  own established local/CI split).
- **Occurrence-snapshot integration** (`tests/test_control_occurrences.py`,
  +3 tests, mirroring the file's own existing
  `test_responsible_person_id_snapshotted_not_live_join`): no
  fabricated reference before any version exists; historical stability
  (an occurrence generated against v1 keeps referencing v1 even after
  v2 becomes effective; new occurrences reference v2); manual
  occurrence recording snapshots the same way.
- **Starter-control wiring** (`tests/test_starter_controls.py`, +1
  test): every starter-generated control gets a real effective version
  whose snapshot matches the control's actual fields/mappings.
- **Full regression suite**: `pytest -q` — 1044 passed, 7 skipped
  (Postgres-gated, +1 over baseline for the new Postgres backfill
  test), 21 deselected (`uat` marker), 2 xfailed (issue #65,
  pre-existing/unrelated), 0 failed. Delta from the pre-#42 baseline
  (1009, immediately post-#28) is exactly this issue's 35 new tests.
- **Lint/format**: clean (`ruff check .`, `ruff format --check .`,
  including this design doc's embedded code blocks).
- **SQLite/PostgreSQL equivalence**: verified locally on SQLite (batch
  table recreation for the new FK, partial unique index with both
  `sqlite_where`/`postgresql_where` specified); the live-PostgreSQL
  backfill test is exercised by CI's `test-postgres` job.
- **No headless UAT / no HTTP route**: no user-visible surface in this
  issue — backend service layer only, matching the connector epic's
  own established precedent for slices that don't yet have an
  operator-facing surface defined.

## Known deferred/untested paths

- `app/audit_package.py`'s export does not yet surface
  `control_definition_version_id` or control-definition version
  history — a legitimate, independent follow-on (§ scope decision 4
  above), not implemented here to avoid expanding this issue's scope.
- No HTTP route/admin UI to review/approve a control-definition
  revision interactively — this issue proves the lifecycle mechanism
  and its occurrence-snapshot integration, not an operator-facing
  surface for authoring revisions (no such surface currently exists
  for controls at all, per the repository-reality check in §1 of the
  design doc).
- Live-PostgreSQL backfill verification depends on CI's `test-postgres`
  job (no local Postgres available in this environment) — same
  established split #31 already has.
