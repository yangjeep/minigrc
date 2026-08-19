# Issue #33: Readiness landing page — how far are we from SOC 2 audit readiness?

Status: implemented. See docs/worklog/2026-08-19-issue33-readiness-landing-page.md
for the full verification account.

## 1. Repository-reality check

- #14 (readiness work queue, `app/readiness.py::compute_readiness_queue`)
  and #30 (onboarding checklist, `app/onboarding.py::compute_onboarding_steps`)
  are both merged and both already render on `/` (`app/routers/dashboard.py`,
  `app/templates/dashboard.html`) — the dashboard route IS this app's
  post-login landing page (`NAV_ITEMS`'s first entry, `app/main.py`). There
  is no separate "landing page" route to build; #33's own execution
  prompt explicitly warns against "implementing duplicate task/readiness
  state" and instructs inspecting what #14/#30 already produce first.
- What's missing, concretely, against #33's acceptance criteria:
  1. **No single "how far are we" stage label exists.** The dashboard
     shows an onboarding-progress fraction and a readiness queue
     side by side, but nothing synthesizes them into the PRD §8 stage
     vocabulary (`scope incomplete` / `foundation incomplete` /
     `operating controls` / `evidence gaps` / `testing/remediation
     incomplete` / `audit-package ready`).
  2. **The queue dumps every row with no prioritization.** #33's
     execution prompt explicitly asks for "a small useful set of next
     actions rather than dumping every row" — #14 built the full,
     transparent table (correctly, for that issue's scope) but never a
     "top priorities" slice.
  3. **A fresh, unscoped deployment's queue renders as "Nothing needs
     attention right now."** That message is honest for a mature,
     healthy program but actively misleading for a program that hasn't
     started — #33 explicitly asks the page to "distinguish new-program/
     incomplete-scope state from active operating-period state."
- Every fact needed for all three already exists: `get_compliance_scope`,
  `compute_onboarding_steps`, and `compute_readiness_queue`'s own item
  categories are sufficient to derive a stage and a priority ordering —
  no new persisted state, matching the issue's explicit instruction.

## 2. Scope decisions

1. **No new route, no new page.** `/` remains the landing page; #33 is
   entirely a composition/synthesis layer over #14+#30's existing
   outputs, rendered on the same route.
2. **The stage is a pure function of existing computed state, not a new
   stored field.** `compute_readiness_stage(session, settings)` (added to
   `app/readiness.py`, since it's a natural synthesis of that module's
   own queue plus `app/onboarding.py`'s steps) walks the PRD §8 stages in
   order and returns the first one whose condition is unmet:
   - `scope_incomplete` — no `ComplianceScope` exists yet.
   - `foundation_incomplete` — scope exists, but starter controls aren't
     fully generated or a control is missing an owner (reuses
     `compute_onboarding_steps`' own `starter_controls`/`assign_owners`
     step results — not a re-derivation).
   - `operating_controls` — foundation is done, but no operating period
     is active yet (reuses the `operating_period` step).
   - `evidence_gaps` — an operating period is active, but the readiness
     queue has an overdue occurrence, a missing-evidence occurrence, or
     stale evidence.
   - `testing_remediation_incomplete` — evidence gaps are clear, but the
     queue has a failed test with no finding, a finding needing
     attention/retest, or an ownership/policy-review gap.
   - `audit_package_ready` — none of the above; the queue's only
     possible remaining category is `occurrence_upcoming` (proactive,
     not a blocker).
   Every stage is directly traceable to concrete state already computed
   elsewhere — exactly the PRD §8 invariant ("every stage/blocker is
   traceable to concrete authoritative state"), and reuses rather than
   re-implements #14/#30's own condition logic.
3. **"Top priorities" is a slice of the existing queue, not a new
   category or a new sort.** The queue is already sorted
   (`due_on` ascending, nulls last, then category — #14). The landing
   page shows the first `_TOP_PRIORITY_LIMIT` (5) items prominently; the
   full table remains, unchanged, immediately below for complete
   transparency — the issue asks to prioritize what's shown *first*, not
   to hide anything.
4. **No opaque/manual score.** The stage is a label with an explicit
   reason string, never a percentage; it can only regress if the
   underlying state regresses (e.g. a new overdue occurrence appears),
   never by manual edit — there is no field to edit.
5. **The existing small onboarding banner is replaced by a fuller "get
   started" panel only for the two earliest stages**
   (`scope_incomplete`/`foundation_incomplete`); once a program reaches
   `operating_controls` or later, the banner reverts to the existing
   compact form (a mature program with, say, one remaining onboarding
   step doesn't need the full onboarding narrative re-litigated on every
   landing-page view).

## 3. Model

No new table, no new field. Two additions to `app/readiness.py`:

```python
READINESS_STAGES = (
    "scope_incomplete",
    "foundation_incomplete",
    "operating_controls",
    "evidence_gaps",
    "testing_remediation_incomplete",
    "audit_package_ready",
)

READINESS_STAGE_LABELS = {
    "scope_incomplete": "Scope incomplete",
    "foundation_incomplete": "Foundation incomplete",
    "operating_controls": "Operating controls",
    "evidence_gaps": "Evidence gaps",
    "testing_remediation_incomplete": "Testing/remediation incomplete",
    "audit_package_ready": "Audit-package ready",
}


@dataclass(frozen=True)
class ReadinessStageResult:
    stage: str  # READINESS_STAGES key
    reason: str  # human-readable "why this stage" explanation
```

## 4. Routes/UI

`app/routers/dashboard.py`'s existing `GET /` additionally computes
`compute_readiness_stage` and slices the already-computed queue into
`top_priority_items`/`remaining_items`. `app/templates/dashboard.html`
gains, above the existing readiness-queue table: a stage banner (label +
reason) and, for the two earliest stages, a fuller onboarding-forward
panel replacing the current compact one-line banner. The "top
priorities" list renders above the existing full queue table (which
stays exactly as #14 built it).

## 5. Testing strategy

- Unit (`app/readiness.py`): each stage's exact boundary (the specific
  state transition that advances/regresses it), confirming
  `occurrence_upcoming` alone never blocks `audit_package_ready`.
- Routes: fresh deployment shows `scope_incomplete` + the fuller
  onboarding panel; a fully-operating, healthy program shows
  `audit_package_ready` with the compact banner; a mixed state shows the
  correct intermediate stage and a top-priorities slice capped at 5 with
  the full table still present below it.
- Headless UAT (required): walk a deployment through several stage
  transitions end to end (fresh → scope defined → foundation built →
  period started → an issue introduced → resolved → audit-package
  ready), confirming the stage label changes deterministically at each
  step.

## 6. Known deferred/untested paths

- No stage-change notification/history — the stage is recomputed fresh
  on every page load, per the "never store a derived flag" precedent;
  there is no "stage changed on date X" audit trail (a plausible future
  addition, not required by this issue).
- The `_TOP_PRIORITY_LIMIT` of 5 is a judgment call, not a configurable
  setting — no evidence a fixed number is wrong; easy to change later if
  real usage disagrees.
