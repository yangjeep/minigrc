# Issue #11: control operations lifecycle (cadence, occurrences, performance, evidence)

**Date:** 2026-08-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Implements #11's control operations lifecycle as the first concrete domain
built on the #22 event-store/projector foundation: cadence-driven and
manual/event-driven control occurrences, performance claims (with
backdating and correction support), evidence linking, and a control-period
grouping concept for scoping generation to an audit window. Reconciles the
pre-#22 design doc against the now-merged event-store contract (documented
in the design doc's §12) before implementation, per the explicit Phase 3
instruction.

## Files Changed

- `app/models.py` — `ControlPeriod` (plain mutable row), `ControlOccurrence`/
  `ControlOccurrenceEvidence` (canonical projections over the
  `"control_occurrence"` `DomainEvent` aggregate — never written to
  directly), `InternalControl` +3 columns (`cadence_type`,
  `cadence_interval_months`, `owner_person_id`) + 2 new CHECK constraints
  + a partial unique index (`uq_occurrence_control_due_no_period`).
- `app/control_occurrences.py` (new) — event projectors, `generate_occurrences`
  (idempotent per due date), `record_occurrence_manually`,
  `perform_occurrence`, `link_evidence`, `rebuild_control_occurrence_projection`.
- `app/routers/control_periods.py` (new) — structural/admin routes:
  create/activate/close a period, bulk-generate across all calendar
  controls.
- `app/routers/controls.py` — extended `GET /controls/{id}` with an
  occurrence list (overdue/upcoming/completed, computed not stored);
  `POST /controls/{id}/occurrences/generate` (self-serve rolling
  generation); `POST /controls/{id}/occurrences` (manual/event-driven
  creation); `POST /controls/{id}/owner` (assign `owner_person_id`);
  `CONTROLS_REGISTER_CONFIG` gained `cadence_type`/`cadence_interval_months`
  fields.
- `app/routers/occurrences.py` (new) — `GET /occurrences/{id}` (detail +
  pre-filled perform form), `POST /occurrences/{id}/perform` (sets the
  performance claim, optional evidence link in the same request via a
  `db.begin_nested()` savepoint so a duplicate-evidence failure can't
  discard the already-applied performance event).
- `app/registers/router.py` — `update_row` now catches `IntegrityError`
  (matching `create_row`'s existing handling) — a real gap this issue's
  new CHECK constraint made reachable in practice.
- `app/cli.py` — `generate-control-occurrences` subcommand.
- `app/templates/control_periods/*.html`, `app/templates/occurrences/detail.html`,
  `app/templates/controls/{list,detail}.html` (extended).
- `migrations/versions/2f6df407060b_...` (new) — additive-only; hand-edited
  after autogenerate for the partial index, `batch_alter_table` FK/CHECK
  constraints, and an explicit `copy_from` in `downgrade()` (see Decisions
  below).
- `tests/test_control_occurrences.py`, `tests/test_control_periods.py`,
  `tests/test_occurrences.py` (new); `tests/test_cli.py`,
  `tests/test_register_api.py`, `tests/test_postgres_compat.py` (extended).
- `docs/superpowers/specs/2026-08-15-issue11-control-operations-lifecycle-design.md`
  — §12 (reconciliation against #22) and §13 (post-implementation
  adversarial review) added this session.
- `docs/architecture.md` — new "Control operations (issue #11)" section.

## Verification

- [x] Unit/regression tests: 69 new tests — `test_control_occurrences.py`
      (32: schema, projectors, generation, rebuild determinism),
      `test_control_periods.py` (10: structural routes),
      `test_occurrences.py` (20: operational routes), plus 4 added to
      `test_cli.py` and 3 to `test_register_api.py`. One existing test in
      `test_postgres_compat.py` was extended rather than added.
- [x] Full suite: 498 passed, 1 skipped (Postgres-gated), 0 failed
      (`pytest -q`, 482s).
- [x] `ruff check .` / `ruff format --check .` clean.
- [x] SQLite migration verified by hand: `upgrade head` → `downgrade base`
      → `upgrade head` full round trip, including the two new CHECK
      constraints and the partial unique index.
- [x] PostgreSQL live-migration test — extended
      (`tests/test_postgres_compat.py::test_migrations_apply_cleanly_against_postgres`
      now also proves the partial index's NULL-vs-NULL semantics and the
      new CHECK constraint against a real server). Not run locally (no
      Postgres in this sandbox); confirmed via CI's `test-postgres` job on
      PR #52 — passed (39s), see
      https://github.com/yangjeep/minigrc/actions/runs/32047366852/job/95438247060.
- [x] Independent adversarial review (fresh subagent, full read of §12) —
      6 findings, all fixed; see Decisions below. Idempotency, the
      `/perform` + evidence-link savepoint handling, and the partial
      index's NULL semantics were specifically distrusted by the reviewer
      and independently re-verified as correct.
- [x] Manual UI/API smoke tests: register-grid PATCH for
      `cadence_type`/`cadence_interval_months` (including negative-value
      rejection returning a clean 422, not a 500); `/controls/{id}`,
      `/occurrences/{id}` template rendering.

## Decisions & Alternatives Rejected

- **Route not in the original design table, added during implementation:**
  `POST /control-periods/{id}/activate` — without it, a period created in
  its default `"planned"` status has no route ever transitioning it to
  `"active"`, making `generate_occurrences`'s active-period requirement
  unreachable via the UI.
- **Independent adversarial review found 6 real, confirmed gaps**, all
  fixed (see design doc §13 for full detail):
  1. **(High)** `cadence_type`/`cadence_interval_months`/`owner_person_id`
     were never actually wired into the register grid/a route despite
     the design doc claiming they were — the generation machinery was
     correct but unreachable by a real user. Fixed with register-grid
     `FieldSpec`s for the two enum/number fields and a small dedicated
     form/route for the person-FK field.
  2. **(High)** A non-positive `cadence_interval_months` would send the
     due-date generation loop backward forever (only `None`/`0` were
     filtered, not negative values). Fixed with a DB CHECK constraint
     (authoritative) plus a domain-level guard (defense in depth).
  3. **(Medium)** No Postgres-specific test coverage existed for the new
     tables despite the design's own test plan calling for it. Extended
     the gated live-Postgres test.
  4. **(Medium)** The CLI's non-active-period path raised an uncaught
     `ValueError` instead of the clean error pattern its sibling checks
     use. Fixed with `try/except`.
  5. **(Medium)** `/perform`'s date field always defaulted to today, even
     when correcting an already-performed occurrence — risked silently
     shifting an immutable event's `occurred_at` on resubmission. Fixed
     to pre-fill from the existing claim.
  6. **(Low)** Missing negative-path test for an unknown
     `evidence_snapshot_id` on `/perform` (already handled correctly;
     just untested).
- **A related gap found while fixing #2, not flagged by the reviewer:**
  `ControlOccurrence`'s `UniqueConstraint("control_id", "control_period_id",
  "due_at")` does not stop two period-less occurrences from sharing a
  `due_at` — standard SQL treats `NULL != NULL` for uniqueness on both
  SQLite and Postgres. Closed with a companion partial unique index
  (`uq_occurrence_control_due_no_period`), tested on both backends.
- **A genuine Alembic/SQLite tooling issue, unrelated to this domain's
  correctness:** `batch_alter_table`'s reflection-based
  `drop_constraint(name, type_="check")` intermittently failed to find a
  second named CHECK constraint on `internal_controls` when both were
  dropped via table-recreate in this environment — reproduced directly
  against `ApplyBatchImpl`/`get_check_constraints` internals; plain
  `Table(autoload_with=...)` reflection outside that code path correctly
  found both. Worked around with an explicit `copy_from=<Table>` in the
  migration's `downgrade()`, which bypasses reflection entirely — this is
  `batch_alter_table`'s own documented workaround for CHECK-constraint
  limitations, not a novel pattern.
- **`update_row` (generic register-grid PATCH) now catches
  `IntegrityError`**, mirroring `create_row`'s existing handling exactly.
  This is shared infrastructure, not #11-specific code, but the new
  `ck_control_cadence_interval_positive` constraint is the first case
  where a register-grid PATCH could hit a non-uniqueness CHECK violation
  on update — without this fix it would surface as a generic 500 instead
  of a clean 422. Small, safe, precedent-matching fix; not a refactor.

## Known Gaps / Follow-ups

- Control-occurrence activity is completely absent from `/admin/audit-log`
  and the Dashboard's recent-activity widget (both read `AuditEvent`
  directly; occurrence mutations are event-sourced instead, deliberately,
  per design doc §12.3). Issue #45 ("auditor-facing historical timeline")
  is the tracked follow-up. Flagged in the design doc §10 as needing
  explicit human sign-off before this is considered acceptable long-term,
  not silently resolved here.
- No lower bound on backdating a performance claim (`occurred_at` only
  rejects future values) — an open product policy question, not a bug,
  per design doc §12.3.
- `owner_person_id`'s dedicated form/route (rather than a register-grid
  field) is a reasonable but new precedent for this app's UI conventions;
  a future issue adding more person-FK fields to controls should consider
  whether a shared micro-pattern (or a register-grid FK field type) is
  warranted rather than one-off forms per field.

## Next Steps

Push branch, open draft PR against `main`. Do not merge without explicit
authorization per standing instructions.
