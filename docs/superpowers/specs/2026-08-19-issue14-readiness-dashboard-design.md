# Issue #14: Continuous SOC 2 readiness work queue and operational dashboard

Status: implemented. See docs/worklog/2026-08-19-issue14-readiness-dashboard.md
for the full verification account.

## 1. Repository-reality check

- The current dashboard (`app/routers/dashboard.py`, `app/templates/dashboard.html`)
  already exists and is additive-only territory, not a rewrite: framework
  completion percentages (`app/progress.py::compute_progress`), open-risk
  count, policy-status counts/due-soon/overdue, recent requirement notes,
  admin-only recent audit events, and (from #30) an onboarding-incomplete
  banner. None of it touches `ControlOccurrence`/`ControlTest`/`Finding`/
  evidence gaps today.
- `app/vendor_flags.py::compute_flags` is this repo's only existing
  "compute live, never store" precedent (its own docstring: "persisting
  it would just be a cache that could drift stale") — but it returns
  plain warning strings scoped to one vendor, rendered inline on
  vendor list/detail pages. **No unified cross-domain queue page, and no
  "reason + deep link" presentation shape, exists anywhere yet** — #14
  is genuinely new UI shape, not a duplicate of an existing pattern.
- **"Evidence awaiting review" has no backing field anywhere.**
  `EvidenceSnapshot.status` (`EVIDENCE_STATUSES = ("pass","fail","warning","unknown")`)
  is a *connector-computed* result, not a human review/attestation
  state; `EvidenceArtifactVersion` has no status/review field at all.
  Building this queue item literally would require inventing a new
  field — directly conflicting with the issue's own "no new domain
  model beyond gaps strictly required to render existing state"
  instruction. **Deliberately not implemented** — see §2.
- No existing query anywhere computes "occurrence performed but missing
  evidence," "test failed with no finding opened yet," "finding overdue,"
  or "finding awaiting retest" — all genuinely fresh queries over
  already-existing columns, not duplicating anything.
- `EvidenceSnapshot.expires_at` (freshness) DOES already exist — "stale
  evidence where a freshness rule exists" is renderable from real state.
- `Policy.next_review_date` is the only existing policy-review signal;
  `PolicyVersion`'s lifecycle (#31) has `submitted_at`/`reviewed_at` but
  no existing "pending review too long" query — only the review-date
  signal is used here, not a new one.
- Ownership-gap fields that already exist (all nullable/emptyable):
  `InternalControl.owner`/`owner_person_id`, `ControlOccurrence.responsible_person_id`,
  `RequirementAssessment.owner`, `Risk.owner`, `VendorSystem.business_owner_person_id`/
  `renewal_owner_person_id`, `Policy.owner`, `ExternalConnection.owner`,
  `Finding.owner_person_id` (#13). This design surfaces the two with the
  clearest actionable "this needs an owner assigned" semantics
  (`InternalControl`, `Finding`) rather than all eight — see §2.
- `require_login` (no write access) is sufficient — a read-only queue
  needs no new role, matching the existing dashboard.

## 2. Scope decisions

1. **No new domain model or field, full stop.** Every queue category
   below is computed from columns that already exist. "Evidence awaiting
   review" is explicitly omitted — this repository has no human
   evidence-review workflow to surface yet (`EvidenceSnapshot.status` is
   a connector result, not a review state); inventing one here would be
   exactly the kind of scope expansion RULES.md §12 asks agents to
   surface rather than silently build.
2. **"Tests due" and "retests due" collapse into one category,
   `finding_needs_retest`.** No independent "test schedule"/"next test
   due" field exists anywhere — the only concrete, already-existing fact
   is "a finding is in `retesting` status and has no `FindingRetest` row
   yet." Treating "tests due" and "retests due" as the same underlying
   fact (rather than two categories for one condition) is the issue's
   own explicit instruction ("without duplicating queue items for the
   same underlying operational fact").
3. **A finding that is both "open" and overdue is one queue item, not
   two.** `finding_needs_attention` covers every non-closed,
   non-retesting finding; its `reason` string states "Overdue since
   {due_date}" / "Due {due_date}" / "No due date set" as appropriate,
   rather than emitting a second `finding_overdue` row for the same
   finding. This keeps the "no duplicate items for one fact" principle
   even though the issue's literal wording lists "open findings" and
   "overdue remediation" as separate bullets — the two bullets describe
   one underlying object (the finding) at different urgency, not two
   independent facts.
4. **Ownership gaps surface only `InternalControl` (no owner) and
   `Finding` (no `owner_person_id`, only while non-closed)** — the two
   places a missing owner is unambiguously actionable through an
   existing assignment route. The other six nullable-owner fields listed
   in §1 are left for a future, separately-scoped pass rather than
   inflating this queue with every free-text `owner == ""` field in the
   schema (several of which — `Risk.owner`, `Policy.owner` — have no
   dedicated "assign owner" route to link to today).
5. **No dismiss/snooze.** The issue frames it as optional ("if
   implemented, must not hide the underlying condition"). Implementing
   it correctly would require a new persisted per-item acknowledgment
   table — real new state the issue's own non-goals list doesn't
   require ("no new assurance domain model beyond gaps strictly
   required"). Not built in this slice.
6. **No blended aggregate readiness percentage.** The existing dashboard
   already shows raw, transparent counts (open risks, policies overdue,
   per-framework requirement-completion percent from `compute_progress`)
   — #14 adds a total open-queue-item count and per-category counts,
   still raw and transparent, never a single blended "readiness score."
7. **Framework filtering narrows visibility, it does not duplicate
   generation.** Every item is computed exactly once; an optional
   `framework_id` query filter hides items whose underlying control (via
   `ControlRequirementMapping` → `FrameworkRequirement.framework_id`)
   doesn't map to the selected framework. Items with no `control_id`
   (e.g. a manually-opened finding) are framework-agnostic and always
   shown regardless of the filter — hiding them under either lens would
   misrepresent them as resolved.
8. **The existing dashboard page is restructured, not replaced.** The
   readiness queue becomes the dashboard's primary content (per the
   issue's "prefer a compact operational dashboard over framework
   completion vanity metrics"); the existing framework-progress table,
   policy-status breakdown, and recent-activity panels move below it,
   unchanged in their own computation.

## 3. Model

No new table. A plain, request-scoped dataclass — computed live,
matching `app/vendor_flags.py`'s "never stored" precedent:

```python
@dataclass(frozen=True)
class ReadinessItem:
    category: str  # READINESS_CATEGORIES key
    reason: str  # human-readable, e.g. "Overdue since 2026-01-15"
    link: str  # URL to the underlying authoritative object
    control_id: str | None  # for framework-filter visibility; None = framework-agnostic
    due_on: date | None  # for sorting; None when not applicable


READINESS_CATEGORIES = {
    "occurrence_overdue": "Missed control occurrence",
    "occurrence_upcoming": "Upcoming control occurrence",
    "occurrence_missing_evidence": "Occurrence missing evidence",
    "evidence_stale": "Stale evidence",
    "control_test_failed_no_finding": "Test exception without a finding",
    "finding_needs_attention": "Finding needs attention",
    "finding_needs_retest": "Finding awaiting retest",
    "control_missing_owner": "Control missing an owner",
    "finding_missing_owner": "Finding missing an owner",
    "policy_review_overdue": "Policy review overdue",
}
```

## 4. Derivation (`app/readiness.py`)

One `_compute_<category>(session) -> list[ReadinessItem]` function per
category, each a single query over existing columns:

- `occurrence_overdue`/`occurrence_upcoming`: `ControlOccurrence.due_at`
  vs now / now+14 days, `performed_at IS NULL`.
- `occurrence_missing_evidence`: `performed_at IS NOT NULL`, no matching
  `ControlOccurrenceEvidence` or `ControlOccurrenceEvidenceArtifact` row
  (`NOT EXISTS` on both).
- `evidence_stale`: `EvidenceSnapshot.expires_at IS NOT NULL AND expires_at < now()`.
- `control_test_failed_no_finding`: `ControlTest.result == "exceptions_noted"`
  with no `Finding.source_test_id == test.id`.
- `finding_needs_attention`: `Finding.status IN ("open", "remediating")`.
- `finding_needs_retest`: `Finding.status == "retesting"`.
- `control_missing_owner`: `InternalControl.owner_person_id IS NULL AND owner == ""`.
- `finding_missing_owner`: `Finding.status != "closed" AND owner_person_id IS NULL`.
- `policy_review_overdue`: `Policy.next_review_date < today` (same
  predicate `dashboard.py` already uses for its count).

`compute_readiness_queue(session, *, framework_id=None) -> list[ReadinessItem]`
runs every `_compute_*` function, concatenates, and — when `framework_id`
is given — filters out items whose `control_id` is not `None` and maps
(via a single upfront `control_id -> {framework_id}` dict built from
`ControlRequirementMapping` JOIN `FrameworkRequirement`) to a set that
excludes the requested framework. Sorted by `due_on` (nulls last), then
category.

## 5. Routes/UI

`app/routers/dashboard.py`'s existing `GET /` gains an optional
`framework_id` query parameter, computes `compute_readiness_queue`, and
passes the queue (plus per-category counts and the active-framework list
for the filter dropdown) into the existing template context — no new
router file, no new route path; this is one view, not a separate page,
per the issue's "one place" acceptance criterion.

`app/templates/dashboard.html` is restructured: onboarding banner (#30,
unchanged) → new readiness queue section (framework filter dropdown,
grouped-by-category list, each row showing `reason` + a link into the
underlying page) → existing stat row/framework progress table/policy
breakdown/recent activity (unchanged computation, now secondary content).

## 6. Testing strategy

- Unit (`app/readiness.py`): each category's boundary (due-date edges,
  timezone-naive date comparisons using the same UTC convention as the
  rest of the app), empty/healthy state (no items), framework-filter
  visibility (control-mapped items hidden/shown correctly, control-less
  items always shown), no duplicate items for one underlying fact.
- Routes: dashboard renders the queue for a representative mixed state
  (at least one of each category); framework filter narrows correctly;
  RBAC — any logged-in role sees the queue (read-only, matches existing
  dashboard access).
- Headless UAT (required): a scenario building one instance of several
  categories (missed occurrence, missing-evidence occurrence, failed
  test with no finding, overdue finding, finding awaiting retest,
  unowned control) and confirming the dashboard surfaces all of them
  with working deep links, then resolving one and confirming it
  disappears.

## 7. Known deferred/untested paths

- "Evidence awaiting review" — no backing field exists; not implemented
  (§2.1).
- Dismiss/snooze — not implemented (§2.5).
- Ownership gaps beyond `InternalControl`/`Finding` (`Risk.owner`,
  `Policy.owner`, `VendorSystem` admin/owner fields, `RequirementAssessment.owner`,
  `ExternalConnection.owner`) — left for a future, separately-scoped pass.
- No notification/nagging delivery (explicit issue non-goal).
