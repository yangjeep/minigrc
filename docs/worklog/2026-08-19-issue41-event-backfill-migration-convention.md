# Issue #41: existing-mutable-state to event-backed migration convention

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** docs

## Summary

Documentation-only. #41 asked for a documented convention for
bootstrapping an existing mutable domain onto the event store without
fabricating history, "proven with one real domain migration."

#31 (policy-version lifecycle, merged earlier this session as PR #62)
already is that real domain and already satisfies every acceptance
criterion #41 lists — its migration backfills bootstrap `DomainEvent`
rows (`actor_type="migration"`) for pre-existing `PolicyVersion` rows,
is idempotent across an upgrade/downgrade/upgrade cycle, and its
projection rebuild reproduces identical state. No new code or migration
was needed; this issue's remaining work was purely to extract and
generalize the convention into its own document
(`docs/superpowers/specs/2026-08-19-issue41-event-backfill-migration-convention.md`)
so the next domain to event-source existing mutable state (#42, now
unblocked) can follow it without re-deriving it from #31's specifics.

## Correction made to #41's own premise

- #41 named #11 (control operations) as a candidate proof domain
  alongside #31. Checked directly: #11 never needed this — its
  `ControlOccurrence` table was introduced brand-new by #22 itself, with
  no pre-existing mutable rows. #31 is the only domain that actually
  performed this kind of migration.
- #41's text says a bootstrap event's `occurred_at` should be "left
  unset/equal to `recorded_at` since the real historical `occurred_at`
  is unknowable" — stated as a blanket rule. This is narrower than what
  `docs/superpowers/specs/2026-08-15-issue22-event-store-design.md` §5
  already authorized before #41 was filed ("a caller may only pass a
  different `occurred_at` for genuine backdating \[...\] e.g. a
  migration/import fact"), and narrower than what #31 actually
  correctly did (backdating `occurred_at` to each `PolicyVersion`'s real
  `captured_at` — a genuine signal, not a fabrication, and more
  historically accurate than the migration's run time). The design doc
  formalizes the fuller, already-authorized rule: use the best
  available real proxy timestamp when one exists; fall back to the
  migration's run time only when no such signal exists.

## Verification

No new code was written, so no new automated tests were added — the
acceptance criteria are satisfied by tests already merged in #31 (PR
#62):

- Idempotency: `tests/test_policy_lifecycle_migration.py::test_upgrade_downgrade_upgrade_round_trip_does_not_duplicate_events`.
- Rebuild equivalence: `tests/test_policy_lifecycle.py::test_rebuild_policy_version_lifecycle_projection_reproduces_state`.
- Bootstrap-event distinguishability: `tests/test_policy_lifecycle_migration.py::test_backfill_inserts_bootstrap_events_marked_as_migration`.
- No destructive rewrite: confirmed by direct reading of
  `migrations/versions/c4e8a7f2b391_add_policy_version_lifecycle.py`.

Reran the full `pytest tests/test_policy_lifecycle_migration.py
tests/test_policy_lifecycle.py` suite to reconfirm these still pass
before writing this doc — unchanged from #31's own merge (green).
`ruff format --check` on the new design doc — clean.

## Known deferred/untested paths

- No second domain migration was performed in this issue, per its own
  non-goals ("does not migrate every existing mutable domain in one
  PR"). #42 (extending the lifecycle pattern to `InternalControl`
  definitions) is the next candidate to apply this convention.
