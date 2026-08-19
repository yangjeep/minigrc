# Issue #14: Continuous SOC 2 readiness work queue and operational dashboard

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a unified, cross-domain readiness work queue (`app/readiness.py`)
computed live from already-authoritative state — never a second
manually-maintained task table — and makes it the dashboard's primary
content, restructuring (not replacing) the existing framework-progress/
policy/activity panels beneath it. See
`docs/superpowers/specs/2026-08-19-issue14-readiness-dashboard-design.md`
for the full design and repository-reality inventory, including the
several places this design deliberately does NOT implement something
the issue's wording literally lists, with reasoning for each.

## What changed

- `app/readiness.py` (new) — `ReadinessItem` dataclass, `READINESS_CATEGORIES`,
  ten `_compute_*` query functions (one per category), and
  `compute_readiness_queue(session, *, framework_id=None)` which computes
  every item exactly once and only *filters visibility* by framework —
  never duplicates generation.
- `app/routers/dashboard.py` — `GET /` gains an optional `framework_id`
  query parameter; computes and passes the queue plus category labels.
- `app/templates/dashboard.html` — the readiness queue (with a framework
  filter dropdown) is now the page's primary content; the onboarding
  banner (#30) stays above it, everything else moves below, unchanged.

## Categories implemented (all derived from columns that already exist)

`occurrence_overdue`, `occurrence_upcoming` (14-day window),
`occurrence_missing_evidence`, `evidence_stale` (`EvidenceSnapshot.expires_at`),
`control_test_failed_no_finding`, `finding_needs_attention` (reason
states overdue/due/no-due-date — one row per finding, never a second
`finding_overdue` row for the same fact), `finding_needs_retest`
(collapses the issue's separate "tests due"/"retests due" bullets into
the one underlying fact), `control_missing_owner`, `finding_missing_owner`,
`policy_review_overdue`.

## Deliberately NOT implemented (documented up front, not discovered after the fact)

- **"Evidence awaiting review"** — no backing field exists anywhere
  (`EvidenceSnapshot.status` is a connector-computed result, not a human
  review state; `EvidenceArtifactVersion` has no status field at all).
  Implementing this literally would require inventing a new field,
  directly conflicting with the issue's own "no new domain model beyond
  gaps strictly required to render existing state" instruction.
- **Dismiss/snooze** — optional per the issue's own wording; doing it
  correctly would need a new persisted acknowledgment table, which is
  exactly the kind of new state this issue's non-goals ask to avoid.
- **Ownership gaps beyond `InternalControl`/`Finding`** — six other
  nullable-owner fields exist in the schema (`Risk.owner`, `Policy.owner`,
  `VendorSystem`'s owner fields, `RequirementAssessment.owner`,
  `ExternalConnection.owner`); left for a future, separately-scoped pass
  rather than inflating the queue with every free-text `owner == ""`
  field, several of which have no dedicated "assign owner" route to
  link to today.
- **No blended aggregate readiness score** — only raw, transparent
  per-category and total counts, per the issue's explicit warning
  against false precision.

## A pre-existing bug from #13, found and fixed while building #14's tests

`app/finding_lifecycle.py::_project_opened` assigned
`payload.get("due_date")` — an ISO **string**, since the event payload
is JSON — directly to `Finding.due_date` without parsing it back into a
`datetime.date`. On SQLite this raised `TypeError: SQLite Date type only
accepts Python date objects as input` the moment a finding was actually
opened with a due date and then flushed. No #13 test ever caught this
because none of #13's own tests passed a real `due_date` to
`open_finding` — #14's readiness tests are the first to do so (a finding
with a due date is exactly what `finding_needs_attention`'s
overdue-reason logic needs to exercise).

Followed the full bug-fix loop: reproduced via the new failing test,
root-caused to the missing parse step, fixed with a minimal
`_parse_due_date` helper, and added a dedicated regression test in
`tests/test_finding_lifecycle.py` (the correct domain home for it, even
though #13 is already merged — a follow-up fix on a later branch, not a
rewrite of #13's history) proving both the direct call and a projection
rebuild round-trip a real date correctly.

## Test strategy and results

- **Unit** (`tests/test_readiness.py`, 22 tests): every category's
  boundary (overdue vs. within-window vs. far-future; performed-with/
  without-evidence; failed-test-with/without-a-finding; every finding
  status transition's effect on which items appear; owner-present vs.
  missing; archived-policy exclusion), empty/healthy state, no-duplicate-
  item-for-one-fact proof, framework-filter visibility (control-mapped
  items correctly hidden/shown, framework-agnostic items always shown).
- **Regression** (`tests/test_finding_lifecycle.py`, +1 test): the
  due-date bug above.
- **Routes** (`tests/test_dashboard_readiness.py`, 5 tests): empty-state
  message, a real finding surfaced with a working deep link, framework
  filter narrowing over HTTP, reader-role visibility (read-only, matches
  existing dashboard access), resolving the underlying object removes
  the queue item.
- **Headless UAT (required)** — `tests/uat/test_readiness_dashboard.py`:
  builds one ownership gap, one missed occurrence, and one finding over
  a real socket; confirms the dashboard surfaces all three with working
  deep links and the total-count line; resolves the ownership gap via
  the real register API and confirms exactly that item disappears while
  the still-open finding remains; plus a reader-visibility check.
  `GRC_UAT_MODE=1 pytest -m uat tests/uat` — **14 passed, 1 skipped**
  (Postgres-gated, pre-existing).
- **Full regression suite**: `pytest -q` — **815 passed, 6 skipped**
  (Postgres-gated), **15 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#14 baseline (787/6/13/2) is exactly this issue's 28 new passing
  tests (22 + 5 + 1 regression) + 2 new UAT scenarios — no regressions
  elsewhere, and no migration was needed (pure read-layer feature).
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly): no
  new write path was introduced (the queue is entirely read-only,
  `require_login` only, matching the existing dashboard's own access
  level — no new information disclosure beyond what visiting `/controls`,
  `/findings`, etc. directly already exposes to every logged-in role);
  no new event/projection/migration surface to check for drift or
  history-rewriting; confirmed items are computed exactly once and only
  filtered, never duplicated, per framework; confirmed no scope expansion
  beyond the design doc's explicit boundaries.

## Known deferred/untested paths

- "Evidence awaiting review," dismiss/snooze, and the remaining six
  ownership-gap fields — deliberately not implemented (see above).
- No notification/nagging delivery (explicit issue non-goal).
