# Issue #13: Evidence populations, sampling, control testing, findings, remediation, retest

Status: implemented. See docs/worklog/2026-08-19-issue13-assurance-testing-findings.md
for the full verification account.

## 1. Repository-reality check

- **Genuinely greenfield.** Grepped `finding|deviation|retest|sample|population|testprocedure|testresult`
  across `app/`, `migrations/`, `docs/` — every hit is an incidental phrase
  (`"soc2-2017-sample"` catalog key, "adversarial review finding" in a
  comment) or `app/seed.py`'s demo "Sample risk" text, never a real domain
  concept. No prior design/worklog for #13 exists either.
- `ControlOccurrence.performed_at`/`performed_by_person_id`/`scope_note`/
  `evidence_note` (#11, `app/control_occurrences.py::perform_occurrence`)
  is **purely a performance claim** — "it happened, per this claim" — with
  zero test/effectiveness conclusion. This issue adds that conclusion layer
  entirely on top, exactly as #11's own design anticipated.
- `Risk` (`app/models.py:409-439`) is a **different concept**: an
  enterprise risk-register entry ("what could go wrong"), not a specific
  deviation discovered during a specific control test with a specific
  sample/evidence basis. It has no severity/due-date/remediation-update/
  retest concept and its own docstring already flags "a dedicated
  treatment/exception workflow is future scope." Reusing it directly for
  "finding" would conflate two genuinely different facts — a new `Finding`
  concept is added instead, deliberately un-prefixed (not `ControlFinding`)
  so it stays reusable by non-control-test origins later (ISO internal
  audit, manual auditor observations), per the issue's explicit "reuse for
  ISO" requirement.
- Evidence linkage precedent: `ControlOccurrenceEvidenceArtifact`
  (`app/models.py:333-361`) already exists as the join-table shape a new
  test-evidence link should mirror exactly — its own docstring explicitly
  anticipated this issue needing one.
- `Person.id` (never `User.id`) is the established "who did this" FK
  convention (`ControlOccurrence.performed_by_person_id`,
  `app/routers/occurrences.py`) — the new tester/owner fields follow it.
- Current real `aggregate_type` strings in use: `"control_occurrence"`,
  `"policy_version"`, `"evidence_artifact_version"`, `"compliance_scope"`.
  New aggregates below (`"control_test"`, `"finding"`) don't collide.
- RBAC: router-level `require_login`, per-mutation `require_write_access` +
  `verify_csrf` (#37) — confirmed unchanged in the two most recent routers
  (`occurrences.py`, `evidence_artifacts.py`). No reviewer-specific
  read-only route variant exists; every logged-in role gets identical GET
  access today, so findings/tests follow the same shape rather than
  inventing a fifth role.

## 2. Scope decisions

1. **Two new domains, not one **: `ControlTest*` (population → sample →
   test → exception → evidence link) is testing *mechanics*; `Finding`
   (open → remediating → retesting → closed, remediation-update thread) is
   the *consequence* lifecycle. They compose (a test can raise a finding;
   a finding's retest is a new `ControlTest` row) but are separately
   reusable — a finding doesn't require a formal test to exist (a manual
   auditor observation can open one directly, `source_test_id` nullable).
2. **Population/sample rows are plain, not event-sourced.** Once frozen,
   nothing in this design ever updates them — no edit route exists for
   either. Matches `ControlPeriod`'s own precedent ("scheduling/scoping
   metadata that gates [testing], not itself the material compliance
   fact") rather than `ControlOccurrence`'s "one long-lived, continuously
   projected aggregate" shape — there is no multi-step lifecycle to
   replay, only a single atomic creation.
3. **A population is "every `ControlOccurrence` for this control (+
   optional period) as of now," frozen by copying occurrence ids into
   child rows.** This directly satisfies "later source changes must not
   silently alter the historical sample basis" (RULES.md-equivalent
   language in the issue) — new occurrences created afterward, or a
   correction to an already-frozen occurrence's `scope_note`, never
   change the population's membership. No population-definition query
   language is invented; "criteria_note" is a free-text human explanation
   of scope, matching the issue's explicit "store the rationale, not a
   fake universal rule" instruction.
4. **`ControlTest` is event-sourced** (`aggregate_type="control_test"`),
   growing over time exactly like `ControlOccurrence`: `ControlTestRecorded`
   (creates the row + its `ControlTestException` children in one payload)
   followed optionally by later `ControlTestEvidenceLinked` events — same
   shape as `ControlOccurrenceMaterialized` → `ControlOccurrenceEvidenceLinked`.
   A test is never corrected in place; a retest is a **new** `ControlTest`
   row with `retest_of_test_id` pointing back — directly satisfies "Retest
   results are new facts, not edits of prior failures."
5. **`Finding` is event-sourced** (`aggregate_type="finding"`), matching
   `PolicyVersion`'s precedent of a rich state machine with full event
   history (`FindingOpened` → `FindingRemediationUpdateRecorded`* →
   `FindingRetestRecorded`* → `FindingClosed`). This is the one place
   RULES.md §3's exact example applies ("this control was tested and
   passed" is literally the shape of the fact here) — closing a finding
   appends an event; it never rewrites `FindingOpened`'s original payload,
   so "closing a finding does not erase the original issue" holds by
   construction.
6. **Closure decision is a free enum with a rationale, not an automatic
   inference from a passing retest.** `record_retest` never auto-closes —
   closure is always a distinct, explicit `close_finding` call by an
   authorized human, per the issue's "external auditor conclusions must
   not be fabricated... unless explicitly entered by an authorized human"
   and this repo's existing AI-advisory-only invariant (the same principle
   applied to automation in general, not only AI).
7. **No sample-size algorithm.** `selection_method`/`selection_rationale`
   are free text the tester fills in (e.g. "haphazard, 3 of 12 monthly
   occurrences — smallest population, judgmental full look-back"). Nothing
   computes or suggests a "correct" sample size.
8. **Remediation updates reuse `RequirementNote`'s existing append-only-
   thread shape** (a small child table growing via events on the parent
   aggregate) rather than inventing a new generic "Comment" entity.

## 3. Model

```python
class ControlTestPopulation(Base):
    __tablename__ = "control_test_populations"
    id: str  # default=new_id — plain row, not an event aggregate id
    control_id: str  # FK internal_controls.id
    control_period_id: str | None  # FK control_periods.id
    description: str
    criteria_note: str  # free text: how this population was scoped
    frozen_at: datetime
    created_by: str  # actor email, matches ControlPeriod.created_by


class ControlTestPopulationItem(Base):
    __tablename__ = "control_test_population_items"
    id: str  # default=new_id
    population_id: str  # FK control_test_populations.id
    control_occurrence_id: str  # FK control_occurrences.id
    # UniqueConstraint(population_id, control_occurrence_id)


class ControlTestSample(Base):
    __tablename__ = "control_test_samples"
    id: str  # default=new_id
    population_id: str  # FK control_test_populations.id
    selection_method: str  # free text, e.g. "haphazard"
    selection_rationale: str  # free text
    selected_at: datetime
    created_by: str


class ControlTestSampleItem(Base):
    __tablename__ = "control_test_sample_items"
    id: str  # default=new_id
    sample_id: str  # FK control_test_samples.id
    population_item_id: str  # FK control_test_population_items.id
    # UniqueConstraint(sample_id, population_item_id)


TEST_RESULTS = ("no_exceptions", "exceptions_noted")


class ControlTest(Base):
    __tablename__ = "control_tests"
    id: str  # == aggregate_id; no default=new_id
    control_id: str
    control_period_id: str | None
    sample_id: str | None  # nullable: direct-observation tests need no formal sample
    procedure_description: str
    tester_person_id: str  # FK people.id
    performed_at: datetime  # business time, may be backdated like ControlOccurrence
    result: str  # TEST_RESULTS
    notes: str
    retest_of_test_id: str | None  # FK control_tests.id, self-referential
    created_at: datetime  # == event.recorded_at
    updated_at: datetime


class ControlTestException(Base):
    __tablename__ = "control_test_exceptions"
    id: str  # default=new_id
    test_id: str  # FK control_tests.id
    sample_item_id: str | None  # FK control_test_sample_items.id — nullable: a
    # process-level exception isn't always tied to one item
    description: str


class ControlTestEvidenceArtifact(Base):
    __tablename__ = "control_test_evidence_artifacts"
    id: str  # default=new_id
    test_id: str  # FK control_tests.id
    evidence_artifact_version_id: str  # FK evidence_artifact_versions.id
    created_at: datetime
    # UniqueConstraint(test_id, evidence_artifact_version_id) — mirrors
    # ControlOccurrenceEvidenceArtifact exactly.


FINDING_SEVERITIES = ("low", "medium", "high", "critical")
FINDING_STATUSES = ("open", "remediating", "retesting", "closed")
FINDING_CLOSURE_DECISIONS = ("remediated_and_retested", "risk_accepted", "false_positive", "other")


class Finding(Base):
    __tablename__ = "findings"
    id: str  # == aggregate_id; no default=new_id
    title: str
    description: str
    severity: str  # FINDING_SEVERITIES
    status: str  # FINDING_STATUSES
    control_id: str | None
    source_test_id: str | None  # FK control_tests.id
    owner_person_id: str | None  # FK people.id
    due_date: date | None
    closure_decision: str | None  # FINDING_CLOSURE_DECISIONS, set only when closed
    closure_note: str
    created_at: datetime
    updated_at: datetime


class FindingRemediationUpdate(Base):
    __tablename__ = "finding_remediation_updates"
    id: str  # default=new_id
    finding_id: str  # FK findings.id
    note: str
    actor: str
    linked_evidence_artifact_version_id: str | None  # FK evidence_artifact_versions.id
    created_at: datetime


class FindingRetest(Base):
    __tablename__ = "finding_retests"
    id: str  # default=new_id
    finding_id: str  # FK findings.id
    test_id: str  # FK control_tests.id
    created_at: datetime
```

## 4. Lifecycle / state transitions

`app/control_tests.py` (mirrors `app/control_occurrences.py`'s shape):

- `freeze_population(session, control, *, control_period=None, description, criteria_note, actor)`
  → snapshots every current `ControlOccurrence` for that control (+ period
  filter) into a new population + items. Plain insert, no event.
- `draw_sample(session, population, population_item_ids, *, selection_method, selection_rationale, actor)`
  → validates every id belongs to that population; plain insert.
- `record_test(session, control, *, sample=None, control_period=None, procedure_description, tester_person_id, performed_at, result, notes="", exceptions=(), retest_of=None, actor)`
  → validates `result in TEST_RESULTS`; validates every exception's
  `sample_item_id` (if given) belongs to `sample`; validates
  `retest_of.control_id == control.id` when given; appends
  `ControlTestRecorded` (aggregate `"control_test"`).
- `link_test_evidence(session, test, evidence_artifact_version, *, actor)`
  → appends `ControlTestEvidenceLinked` on the same aggregate — mirrors
  `link_evidence_artifact_version`.
- `rebuild_control_test_projection(session)`.

`app/finding_lifecycle.py` (mirrors `app/policy_lifecycle.py`'s shape):

- `open_finding(session, *, title, description, severity, control_id=None, source_test_id=None, owner_person_id=None, due_date=None, actor)`
  → `FindingOpened`, status="open".
- `record_remediation_update(session, finding, *, note, linked_evidence_artifact_version_id=None, actor)`
  → rejects if `finding.status == "closed"`; `FindingRemediationUpdateRecorded`;
  if current status is "open", also transitions to "remediating" (the
  first update is what signals work has genuinely started — an explicit,
  narrow piece of inferred state, not a general auto-transition rule).
- `request_retest(session, finding, *, actor)` → requires
  `status == "remediating"`; `FindingRetestRequested`, status="retesting".
- `record_retest(session, finding, *, test, actor)` → requires
  `status == "retesting"`, and enforces lineage: when `finding.source_test_id`
  is set, `test.retest_of_test_id` must match it; when it isn't but
  `finding.control_id` is, `test.control_id` must match instead (a retest
  can never be accepted from an unrelated control); a finding with
  neither has nothing to validate against, so any test is accepted.
  `FindingRetestRecorded` (payload: test_id, test_result); status becomes
  `"remediating"` again if `test.result == "exceptions_noted"` (bounced
  back — clearly unresolved), otherwise stays `"retesting"` awaiting an
  explicit closure decision. Never auto-closes (see design decision #6).
- `close_finding(session, finding, *, decision, note="", actor)` → allowed
  from any non-`"closed"` status (a false positive or accepted risk can be
  closed without ever reaching "retesting"); `FindingClosed`.
- `rebuild_finding_projection(session)`.

## 5. Routes

`app/routers/control_tests.py` (`require_login` router-level):
`/control-tests/populations` (list/new/create), `/control-tests/populations/{id}`
(detail + "draw sample" form), `/control-tests/populations/{id}/samples`
(create), `/control-tests` (list/new/create — control + optional sample
+ result + exceptions), `/control-tests/{id}` (detail: result, exceptions,
linked evidence, retest lineage both directions), `/control-tests/{id}/link-evidence`
(POST).

`app/routers/findings.py` (`require_login` router-level): `/findings`
(list/new/create), `/findings/{id}` (detail + remediation-update form +
request-retest/record-retest/close forms), `/findings/{id}/remediation-updates`
(POST), `/findings/{id}/request-retest` (POST), `/findings/{id}/record-retest`
(POST, `test_id` selected from that control's tests not yet used as a
retest for this finding), `/findings/{id}/close` (POST).

All mutation routes: `require_write_access` + `verify_csrf` (#37
convention, no new role introduced).

## 6. Testing strategy

- Unit: population freezing correctness (membership fixed even after a
  later occurrence is added), sample-item validation (rejects an item not
  in the given population), test recording (valid results only, exception
  ownership validated), retest linkage (`retest_of.control_id` mismatch
  rejected), finding transitions (each precondition, each rejected
  wrong-status call), remediation-update auto-transition, closure from
  every valid prior status.
- Integration: event append + projection atomicity for both new
  aggregates; rebuild reproduces identical state for both.
- Routes: RBAC (reader/auditor blocked from every mutation), CSRF,
  cross-control/period/sample invalid-reference rejection.
- Migration: SQLite + PostgreSQL-gated; no data backfill (all-new tables).
- Headless UAT (required): the full chain from the issue's own
  verification section — recurring control occurrence → population →
  sample → test with one exception → finding → remediation update →
  retest → close — plus a reader-403 check.

## 7. Known deferred/untested paths

- No owner-reassignment or reopen-a-closed-finding action (smallest
  coherent slice; easy additive follow-up).
- The "record a test" form captures exceptions as one free-text line per
  exception, each stored with `sample_item_id=None` — the domain function
  (`record_test`) fully supports pinning an exception to a specific
  `ControlTestSampleItem`, but building a per-item checkbox+description UI
  row for every sample item was judged not worth the added form complexity
  for this MVP slice. API/future-UI callers can still pass
  `sample_item_id` directly.
- No `EvidenceSnapshot` (connector-sourced) linkage for tests — only
  `EvidenceArtifactVersion`, matching what #32 was explicitly built to
  anticipate; `EvidenceSnapshot` linkage is a plausible, separately-scoped
  future addition if a concrete connector-evidence-in-testing need arises.
- No bulk/CSV import for populations or findings.
