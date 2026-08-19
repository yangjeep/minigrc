# Issue #33: Readiness landing page — how far are we from SOC 2 audit readiness?

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Composes #14's readiness work queue and #30's onboarding checklist —
both already merged and already rendering on `/` — into one coherent
"how far are we" landing narrative: a deterministic PRD §8 readiness
stage label with an explanation, a prioritized top-5 slice of the queue,
and a fuller onboarding-forward panel for the two earliest program
stages. No new route, no new domain model, no new persisted state — see
`docs/superpowers/specs/2026-08-19-issue33-readiness-landing-page-design.md`
for the full design and exactly what gap this closes against #14/#30's
existing output.

## What changed

- `app/readiness.py` — `READINESS_STAGES`, `READINESS_STAGE_LABELS`,
  `ReadinessStageResult`, and `compute_readiness_stage(session, settings)`:
  walks the PRD §8 stages in order, returning the first one whose
  condition is unmet. Reuses `compute_onboarding_steps`' own
  `starter_controls`/`assign_owners`/`operating_period` results and
  `compute_readiness_queue`'s own item categories — no re-derivation of
  either module's logic, no new query beyond composing what already
  exists.
- `app/routers/dashboard.py` — `GET /` additionally computes the stage
  and slices the already-sorted queue into `top_priority_items` (first
  5) and `remaining_readiness_items` (the rest, still fully present).
- `app/templates/dashboard.html` — a stage banner above everything else;
  for `scope_incomplete`/`foundation_incomplete` a fuller "get started"
  panel replaces the previous compact one-line onboarding banner; a
  "Top priorities" table renders before the full (now collapsible)
  queue table, which is otherwise unchanged from #14.

## Why this is not duplicate readiness/task state (the issue's own explicit concern)

- The stage is computed fresh on every request from #14/#30's own
  already-computed results — there is no new column, no new table, and
  no way for it to drift from what those two modules already say, since
  it never redundantly re-implements their conditions.
- The "top priorities" slice is a plain Python list slice
  (`items[:5]`/`items[5:]`) over the exact same sorted list #14 already
  produces — not a second query, not a different ranking algorithm.

## Test strategy and results

- **Unit** (`tests/test_readiness_stage.py`, 9 tests): every stage
  boundary walked in order (fresh deployment → scope defined with no
  primary framework → scope defined with uncovered requirements →
  starter controls generated but an owner missing → foundation complete
  with no active period → an overdue occurrence → a finding needing an
  owner → a fully healthy state → confirming an upcoming-only occurrence
  never blocks `audit_package_ready`).
- **Routes** (`tests/test_dashboard_stage.py`, 3 tests): the fresh-state
  banner and fuller onboarding panel render; the stage banner changes on
  the very next request after defining scope with no other action
  (proving it's recomputed live, not cached); the top-priorities cap
  holds at 5 with the remaining count shown and still present in the
  response.
- **Headless UAT (required)** — `tests/uat/test_readiness_stage.py`:
  walks one deployment through every stage transition end to end over a
  real socket (scope → foundation → operating controls → audit-package
  ready → regressed by an open finding → recovered by closing it),
  asserting the exact rendered stage label at each step.
  `GRC_UAT_MODE=1 pytest -m uat tests/uat` — **15 passed, 1 skipped**
  (Postgres-gated, pre-existing).
- **Full regression suite**: `pytest -q` — **827 passed, 6 skipped**
  (Postgres-gated), **16 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#33 baseline (815/6/15/2) is exactly this issue's 12 new passing
  tests (9 + 3) + 1 new UAT scenario — no regressions elsewhere. No
  migration needed (pure composition of existing computed state).
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly): no
  new write path (the stage/priority view is entirely read-only,
  identical access level to the existing dashboard); no new event/
  projection/migration surface; confirmed the stage can only change by
  the underlying state actually changing (tested directly); confirmed no
  scope expansion beyond the design doc's explicit boundaries (no new
  route, no configurable priority count, no stage-history table).

## Known deferred/untested paths

- No stage-change history/audit trail — recomputed fresh on every load,
  per the existing "never store a derived flag" precedent; a plausible
  future addition, not required by this issue.
- The top-priority cap of 5 is a fixed judgment call, not configurable.
