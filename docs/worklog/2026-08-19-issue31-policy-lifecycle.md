# Issue #31: event-backed policy/compliance-version approval lifecycle

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Turns the existing plain-mutable `PolicyVersion` row into a real,
event-backed approval lifecycle: `draft -> in_review -> approved ->
effective -> superseded/withdrawn`. See
`docs/superpowers/specs/2026-08-19-issue31-policy-lifecycle-design.md`
for the full design, repository-reality inventory, and explicit scope
boundaries (Policy-level metadata/retirement and separation-of-duties
enforcement stay out of scope for this slice, documented as deliberate,
not oversights).

## What changed

- `app/models.py` — `POLICY_VERSION_LIFECYCLE_STATUSES` (6 states);
  `PolicyVersion` gains lifecycle columns (`lifecycle_status`,
  `submitted_by/at`, `reviewed_by/at`, `review_decision/comment`,
  `effective_at`, `superseded_at/by_version_id`, `withdrawn_at/reason`),
  a CHECK constraint, and `uq_policy_version_one_effective_per_policy`
  (partial unique index — see Bugs found below). New `Policy.effective_version`
  property and `control_mappings` relationship. New `PolicyControlMapping`
  table (mirrors `ControlRequirementMapping`) for the "framework/control
  linkage" acceptance criterion.
- `app/policy_lifecycle.py` (new) — event-sourced commands
  (`submit_for_review`, `review_version`, `make_version_effective`,
  `withdraw_version`) and projectors over the `"policy_version"`
  DomainEvent aggregate, plus `rebuild_policy_version_lifecycle_projection`.
  File-capture columns (bytes/hash/uploader/filename) stay a plain,
  non-event-sourced fact — only the lifecycle columns are projected.
- `app/routers/policies.py` — five new routes (submit-for-review,
  review, make-effective, withdraw, control-mappings), all
  `require_write_access`-gated. `retire_policy` now also withdraws the
  policy's effective version, if any (closes a real auditor-facing
  inconsistency: a retired policy could otherwise still show a version
  claiming to be the current effective document).
- `app/templates/policies/detail.html` — lifecycle badge + per-row
  action forms per version, mapped-controls section.
- New migration `c4e8a7f2b391`: adds the lifecycle columns/table/
  constraints/index; deterministically backfills existing
  `PolicyVersion` rows (latest version of an `approved`/`retired`
  policy → `effective`; every non-latest version → `superseded`; a
  `draft` policy's latest version stays `draft`) with matching bootstrap
  `DomainEvent` rows (`actor_type="migration"`) so
  `rebuild_policy_version_lifecycle_projection` reproduces the backfill
  later instead of silently losing it. No event ever claims a human
  reviewed/approved anything.

## Bugs found during verification

1. **Event-append ordering caused a false-positive constraint violation
   in the normal, single-actor sequential case.** `make_version_effective`
   originally appended the new version's `PolicyVersionMadeEffective`
   event *before* the old version's `PolicyVersionSuperseded` event.
   Once `uq_policy_version_one_effective_per_policy` existed, the first
   event's projector flush found the *old* version still `"effective"`
   — momentarily requiring two effective rows for the same policy
   within one call — and correctly raised `IntegrityError`, even
   though no other request was involved at all. Root-caused directly
   (a small reproduction script isolating just `make_version_effective`)
   rather than guessed at. Fixed by reordering: supersede the old
   version first, then make the new one effective.
2. **The reordering fix alone made `rebuild_policy_version_lifecycle_projection`
   intermittently flaky** (~1 in 3 runs). Root cause: `rebuild_projection`
   (app/events.py, issue #22) replays events ordered by
   `(recorded_at, aggregate_type, aggregate_id, aggregate_sequence)` —
   its own docstring already documents that cross-aggregate ties on
   `recorded_at` fall back to an `aggregate_id` tiebreak with no relation
   to true causal order. The `PolicyVersionSuperseded` (old version) and
   `PolicyVersionMadeEffective` (new version) events from one
   `make_version_effective` call are causally linked but sit on
   *different* aggregates, so a tied/near-tied `recorded_at` could
   replay them in the "wrong" order — and my projector's per-event
   `session.flush()` turned that into a transient, spurious
   `IntegrityError` mid-rebuild, even though the *final* replayed state
   is always self-consistent. Confirmed via 3 repeated runs (1 failure)
   before fixing, then 5/5 clean after. Fixed by moving the flush out of
   the projector entirely and into `make_version_effective` itself,
   called once after *both* events are appended — the live command path
   still gets a synchronous, catchable `IntegrityError` against the
   final state; `rebuild_projection`'s replay no longer flushes between
   individual projector calls, so no intermediate (and possibly
   mis-ordered) state is ever sent to the DB engine for constraint
   checking.
3. **Adversarial-review-found (not test-found): no DB-level invariant
   stopped two concurrently-approved versions of the same policy from
   both becoming effective.** `make_version_effective`'s
   `previous_effective` lookup is an in-session read with no locking —
   two different requests, each in their own transaction, could both
   read "no conflicting effective version" and both proceed. Fixed by
   adding `uq_policy_version_one_effective_per_policy`, a partial unique
   index (`WHERE lifecycle_status = 'effective'`), the same pattern
   already established by `ControlOccurrence`'s
   `uq_occurrence_control_due_no_period`. The router now catches the
   resulting `IntegrityError` and returns a clean rejection instead of a
   raw 500.
4. **Pre-existing test fragility, unmasked by the new template markup**:
   `tests/test_policies.py::test_download_requires_auth_and_has_correct_headers`
   extracted a version id via
   `detail.text.split("/versions/")[1].split("/download")[0]` — a naive
   assumption that the download link was the *first* `/versions/` URL on
   the page. The new lifecycle action forms add earlier `/versions/...`
   URLs, breaking that assumption. Fixed the test to extract the
   download link via a precise regex instead of weakening or deleting
   the assertion.

None of these were found by a failing CI/user report — all four were
caught during this session's own implementation/adversarial-review loop,
before commit.

## Test strategy and results

- **Unit — domain functions** (`tests/test_policy_lifecycle.py`, 22
  tests): every precondition/`ValueError` path for all four commands;
  supersede side effect (with/without a prior effective version);
  withdraw from every valid non-terminal state and rejection from every
  terminal one; immutable file-fact survival through lifecycle
  transitions; `Policy.effective_version`; rebuild reproduces identical
  state after a full draft→…→supersede→withdraw sequence (including a
  version with no lifecycle events at all); the DB constraint itself,
  independent of the command layer.
- **Route/regression** (`tests/test_policy_lifecycle_routes.py`, 13
  tests): all five new routes over real HTTP; double-submit fails
  cleanly (no corruption); reject-requires-comment; supersede via real
  upload+review+effective routes; `retire_policy` regression (both
  directions — withdraws an effective version, and is a no-op when
  there is none); duplicate control-mapping rejected; reader/auditor
  403 on lifecycle and control-mapping routes with no state change.
- **Migration** (`tests/test_policy_lifecycle_migration.py`, 8 tests,
  SQLite; 1 Postgres-gated, skipped locally): every backfill rule in the
  design doc; bootstrap events marked `actor_type="migration"`; CHECK
  constraint rejects the old unconstrained shape; the new partial unique
  index itself; upgrade→downgrade→upgrade round trip does not duplicate
  events (caught and fixed the naive idempotency gap directly, before
  it could reach CI — see #37's own PR for the analogous class of CI
  surprise this session already hit once).
- **Headless UAT** (`tests/uat/test_policy_lifecycle.py`, required, not
  skippable): the full real journey — draft → submit → approve →
  effective → new version → submit → approve → effective (supersedes
  the first) — over a real socket, verifying rendered lifecycle badges
  at each step and that the superseded version's file remains
  downloadable; a reader attempting a lifecycle action gets 403. `GRC_UAT_MODE=1
  pytest -m uat tests/uat` — **6 passed, 1 skipped** (Postgres-gated
  SOC 2 lifecycle parametrize, no local Postgres).
- **Full regression suite**: `pytest` — **640 passed, 3 skipped**
  (Postgres-gated), **7 deselected** (`uat` marker), 0 failed.
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  confirmed reader/auditor cannot perform any of the five new mutations
  (tested); confirmed retries/double-submits fail cleanly via the
  precondition checks, not duplicate events (tested); confirmed the
  concurrent-effective-version race (found above, fixed); confirmed
  approved/effective history cannot be rewritten — `review_version`'s
  "rejected → back to draft" path is only reachable from `"in_review"`,
  never from `"approved"`/`"effective"`; confirmed the migration never
  fabricates a human decision (`actor_type="migration"` throughout);
  confirmed SQLite/Postgres portability via the same partial-index/
  batch-alter-table patterns already established elsewhere in this
  repo; confirmed no secrets/URLs/connector surfaces were touched; no
  scope expansion beyond the design doc's explicit boundaries.

## Known deferred/untested paths

- Separation-of-duties (reviewer ≠ submitter) is not enforced — no
  role/assignment data richer than the four RBAC roles exists yet to
  express it against; documented as a deliberate deferral in the design
  doc, matching #37's identical treatment of object-scoping.
- Policy-level metadata edits and `Policy.status` remain plain,
  non-event-sourced fields — only the per-version approval workflow is
  event-backed in this slice.
- PostgreSQL migration/index tests are gated on `TEST_DATABASE_URL` and
  were not run against a live PostgreSQL server in this sandbox; they
  will run in CI's `test-postgres` job.
