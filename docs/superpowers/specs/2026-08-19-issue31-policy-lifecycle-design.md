# Issue #31: event-backed policy/compliance-version approval lifecycle

Status: implemented. See docs/worklog/2026-08-19-issue31-policy-lifecycle.md
for the verification account, including one addition adversarial review
surfaced beyond this design: `uq_policy_version_one_effective_per_policy`
(app/models.py), a partial unique index enforcing "at most one effective
version per policy" at the DB level — §4's application-level
`previous_effective` lookup in `make_version_effective` is only a
best-effort convenience for the common single-actor case; it cannot by
itself prevent two concurrent requests from each making a different
already-approved version effective (neither request's read can see the
other's uncommitted write). The index is the real guard.

## 1. Repository-reality check

- `Policy`/`PolicyVersion`/`PolicyApprovalSnapshot` (`app/models.py:668-790`)
  already exist (issue predates this one). `POLICY_STATUSES = ("draft",
  "approved", "retired")` lives on `Policy` itself, is a plain
  unconstrained `String(32)` column (no CHECK constraint —
  confirmed against `migrations/versions/647102981d1c_initial_mvp_schema.py`),
  and is freely settable by anyone with write access through
  `update_policy`'s plain form (`app/routers/policies.py`). There is no
  workflow gate today: an operator can flip a policy to "approved"
  without any review ever having happened.
- `PolicyVersion` is a real immutable-artifact row (id, sha256,
  byte_size, uploader, change_note, captured_at, source provenance) —
  created via plain `db.add(PolicyVersion(...))` in three places:
  `create_policy` (first version at creation), `upload_policy_version`
  (manual new version), `capture_drive_version` (Drive-sourced new
  version). None of this is event-sourced; it is a plain, immutable-by-
  convention row (never edited/deleted anywhere in the codebase) — the
  same category as `EvidenceSnapshot`'s captured bytes/hash, which
  RULES.md §7 requires provenance for but never requires event-sourcing
  for. `PolicyVersion` has **no lifecycle/workflow status column at
  all** today — only the parent `Policy.status`.
- `PolicyApprovalSnapshot` (`app/models.py:761-790`) is an append-only
  **mirror of Google Drive's own Approvals API state**, captured by
  `sync_drive_approvals` (`app/routers/policies.py:466-546`). It is
  explicitly external-fact provenance, not this app's own approval
  decision — confirmed by its docstring ("Append-only mirror of one
  Google Drive Approvals API record") and by `RULES.md` §9/PRD §4.5
  ("connectors provide facts, not compliance conclusions"). This issue
  must not conflate the two: a real, internal, event-backed approval
  decision is a distinct fact from "what Drive's Approvals API last
  reported."
- **Event-sourcing infrastructure is in place** (`app/events.py`,
  issue #22) and has one real caller today: `app/control_occurrences.py`
  (issue #11). The pattern: a plain module (not a class/framework)
  defines event types as string constants used as dict keys, one
  projector function per event type mutating a canonical projection
  row, a `{event_type: projector}` dict, `append_and_project(session,
  projectors, aggregate_type=..., aggregate_id=..., event_type=...,
  payload=..., actor_type=..., actor_id=..., idempotency_key=...)` for
  commands, and `rebuild_projection(session, projectors,
  aggregate_type=..., reset=...)` for deterministic rebuild. Multiple
  events across different aggregates can be appended in one caller
  transaction (e.g. `occurrences.py::perform` appends a
  `ControlOccurrencePerformed` event and, in the same request, links
  evidence via a second `append_and_project` call) — this repo's
  precedent for "one command touches two aggregates."
- **No existing precedent for retrofitting event-sourcing onto a
  table that predates the event store and already has real rows** —
  `2f6df407060b` (the migration that introduced `ControlOccurrence`)
  created a brand-new table with no legacy data, so it never had to
  backfill bootstrap `DomainEvent` rows. This issue is the first case
  of that; see §4 for the approach.
- **No `Policy`↔`InternalControl`/`Framework` linkage table exists.**
  The acceptance criteria's "framework/control linkage" fact has no
  home today. `ControlRequirementMapping`/`EvidenceControlMapping`/
  `EvidenceRequirementMapping` are the existing precedent for this
  exact shape of plain (non-event-sourced) many-to-many join table.
- `AuditEvent`/`app/audit.py::record_audit_event` is exactly the
  "mutable row plus a descriptive audit entry" pattern RULES.md §1.3
  calls out as insufficient when the event itself is the compliance
  fact — it has no aggregate/sequence/idempotency/replay semantics, just
  a free-text `detail` string. It remains useful as a human-readable
  audit trail alongside the new real domain events, not a replacement
  for them.
- No existing test (`tests/test_policies.py`) covers status transitions,
  immutability of an approved version, or drive-approvals-vs-internal-
  approval distinction — today's tests are upload/download/security-
  boundary focused only.

## 2. Scope decision: version-level lifecycle, not Policy-level

The issue's required lifecycle (`draft -> in_review -> approved ->
effective -> superseded/withdrawn`) is a **per-version** concept in real
GRC practice: one version is "effective" (the operative document) while
a later draft works through review; when the new draft reaches
"effective," the previously-effective version becomes "superseded." This
also matches which facts the issue asks for — "supersedes relationship"
and "content hash" are already version-level concepts.

`Policy.status` (`draft`/`approved`/`retired`) stays as-is, unchanged,
independent, and out of scope for event-sourcing in this slice — it is a
document-identity-level flag ("is this policy still maintained at all"),
not the per-revision approval workflow. The one deliberate coupling: see
§5's `retire_policy` change.

`Policy.effective_date`/`next_review_date` (existing, manually-set target
dates) are untouched — they are planning inputs, not the new per-version
`effective_at` actual-transition timestamp this issue adds.

## 3. Model changes

New constant:

```python
POLICY_VERSION_LIFECYCLE_STATUSES = ("draft", "in_review", "approved", "effective", "superseded", "withdrawn")
```

`PolicyVersion` gains (all nullable except `lifecycle_status`):

- `lifecycle_status` (`String(16)`, default `"draft"`, CHECK-constrained)
- `submitted_by`, `submitted_at` — set on submit-for-review
- `reviewed_by`, `reviewed_at`, `review_decision` (`"approved"|"rejected"`),
  `review_comment` — set on every review decision (both approve and
  reject overwrite these with the latest decision; full history of every
  round remains in `DomainEvent`)
- `effective_at` — set when this version becomes effective
- `superseded_at`, `superseded_by_version_id` (self-FK) — set when a
  newer version becomes effective
- `withdrawn_at`, `withdrawn_reason` — set on withdrawal

New table `PolicyControlMapping` (mirrors `ControlRequirementMapping`
exactly): `id`, `policy_id` FK, `control_id` FK,
`UniqueConstraint(policy_id, control_id)`, `created_at`.

No changes to `Policy`, `PolicyApprovalSnapshot`, or any existing
column's meaning.

## 4. Event design

**Aggregate type:** `"policy_version"`; `aggregate_id` = `PolicyVersion.id`.

Row creation itself (`create_policy`/`upload_policy_version`/
`capture_drive_version`) stays a plain insert — unchanged, per §2's
"file bytes are a provenance-bearing fact, not an event-sourced one"
precedent (matches `EvidenceSnapshot`). Only the **lifecycle columns**
listed in §3 are event-projected; a rebuild resets those columns to
their defaults on every row (never deletes rows — `PolicyVersion` rows
exist independent of lifecycle events, unlike `ControlOccurrence`) and
replays every `"policy_version"` event on top.

Event types and their commands (new module `app/policy_lifecycle.py`,
mirroring `app/control_occurrences.py`'s shape):

1. **`PolicyVersionSubmittedForReview`** — `submit_for_review(session,
   version, *, actor_type, actor_id)`. Precondition: `lifecycle_status
   == "draft"`, else `ValueError`. Payload: `{}`. Projector:
   `lifecycle_status = "in_review"`, `submitted_by = actor_id`,
   `submitted_at = event.occurred_at`.
2. **`PolicyVersionReviewed`** — `review_version(session, version, *,
   decision, comment, actor_type, actor_id)`. Precondition:
   `lifecycle_status == "in_review"`, else `ValueError`. `decision` must
   be `"approved"` or `"rejected"`; a rejection requires a non-empty
   `comment` (mirrors `frameworks.py::update_assessment`'s "not
   applicable requires a note" precedent). Payload: `{"decision":
   ..., "comment": ...}`. Projector: `reviewed_by = actor_id`,
   `reviewed_at = event.occurred_at`, `review_decision = decision`,
   `review_comment = comment`; if `decision == "approved"`,
   `lifecycle_status = "approved"`; if `"rejected"`, `lifecycle_status
   = "draft"` (back for revision — the file/hash never changes, only
   the workflow status; every round trip remains queryable via
   `DomainEvent` even though the projection shows only the latest).
3. **`PolicyVersionMadeEffective`** — `make_version_effective(session,
   version, *, actor_type, actor_id)`. Precondition: `lifecycle_status
   == "approved"`, else `ValueError`. Payload: `{}`. Projector:
   `lifecycle_status = "effective"`, `effective_at = event.occurred_at`.
   **In the same command function** (same caller transaction, two
   separate aggregate events — this repo's established "one command,
   multiple aggregates" shape from `occurrences.py::perform`): if a
   different version of the same policy currently has
   `lifecycle_status == "effective"`, also append **`PolicyVersionSuperseded`**
   on *that* version's aggregate, payload
   `{"superseded_by_version_id": version.id}`. Projector:
   `lifecycle_status = "superseded"`, `superseded_at =
   event.occurred_at`, `superseded_by_version_id = payload value`.
   `PolicyVersionSuperseded` is never invoked directly by a route — only
   as this side effect — but is registered in the projector dict like
   every other event type so `rebuild_projection` can replay it.
4. **`PolicyVersionWithdrawn`** — `withdraw_version(session, version, *,
   reason, actor_type, actor_id)`. Precondition: `lifecycle_status in
   ("draft", "in_review", "approved", "effective")` (not already
   `"superseded"`/`"withdrawn"`), else `ValueError`. Payload:
   `{"reason": reason}`. Projector: `lifecycle_status = "withdrawn"`,
   `withdrawn_at = event.occurred_at`, `withdrawn_reason = reason`.
   Deliberately usable from *any* non-terminal state: an abandoned draft
   that never reached approval, and a formerly-effective version being
   retired with no replacement, are the same terminal action from this
   app's perspective — matching the issue's own diagram, where
   "withdrawn" is reachable at the end of the chain alongside
   "superseded," not only from "draft."

`rebuild_policy_version_lifecycle_projection(session)`: for every
`PolicyVersion` row, reset the lifecycle columns to their defaults
(`lifecycle_status="draft"`, every other new column `None`), then replay
every `"policy_version"`-aggregate event via `rebuild_projection`.

No idempotency_key on any of these four commands: each is a one-shot
manual workflow action, and the state-precondition check already
rejects a double-submission cleanly (a second "make effective" call
finds `lifecycle_status != "approved"` and raises) — a stronger
correctness property than a key-based dedupe would add here, since a
key would only suppress a literal retry, not a semantically-invalid
repeat call from a stale page.

## 5. Route/behavior changes

New routes on the existing `policies` router, all gated by
`require_write_access` (issue #37) — no separation-of-duties check
(e.g. "reviewer must not be the submitter") is enforced; there is no
role/assignment data richer than the four coarse RBAC roles to express
it against yet, so this is a deliberate, documented deferral, not an
oversight, matching how #37 deferred object-scoping until #30:

- `POST /policies/{policy_id}/versions/{version_id}/submit-for-review`
- `POST /policies/{policy_id}/versions/{version_id}/review` (form:
  `decision`, `comment`)
- `POST /policies/{policy_id}/versions/{version_id}/make-effective`
- `POST /policies/{policy_id}/versions/{version_id}/withdraw` (form:
  `reason`)
- `POST /policies/{policy_id}/control-mappings` (form: `control_id`) —
  mirrors `controls.py::add_mapping` exactly, for the new
  `PolicyControlMapping` table.

**`retire_policy` (existing route) change:** in addition to its current
`policy.status = "retired"`, if the policy has a version with
`lifecycle_status == "effective"`, also call `withdraw_version(...,
reason="Policy retired")` on it in the same transaction. Closes a real
correctness gap: without this, a retired policy could still show a
version claiming to be the active, effective document — an
auditor-facing inconsistency. Regression test added (`retire_policy`
already existed; this is a bug-fix-shaped addition to it, not a new
feature).

`create_policy`/`upload_policy_version`/`capture_drive_version`: only
change is the new `PolicyVersion` row starts with
`lifecycle_status="draft"` (the column default) — no other change to
these three routes.

Template (`app/templates/policies/detail.html`): add a "Lifecycle"
column to the versions table, and per-row action forms shown only for
the actions valid from that row's current `lifecycle_status` (submit
when `draft`; approve/reject when `in_review`; make-effective when
`approved`; withdraw when not already `superseded`/`withdrawn`).

## 6. Migration and backfill

New Alembic migration:

1. `create_table("policy_control_mappings", ...)`.
2. Add the eight new nullable `PolicyVersion` columns (§3) plus
   `lifecycle_status` with `server_default="draft"`.
3. **Backfill, per policy** (Python loop over `SELECT` results calling
   `op.execute` per row — same shape as
   `migrations/versions/a248573ccdfe_...`'s framework-catalog backfill,
   not a single blanket `UPDATE`, since the rule is genuinely per-row):
   - The **latest** version (`MAX(version_number)`) of a policy whose
     `Policy.status` is `"approved"` or `"retired"` is backfilled to
     `lifecycle_status="effective"`, `effective_at = that version's
     captured_at` (the best available signal from the pre-lifecycle
     model — the current file *was* the operative document; there is no
     stronger signal available, and this is explicitly an inferred
     timestamp equal to capture time, not a distinct real approval
     date).
   - The latest version of a policy whose `Policy.status` is `"draft"`
     stays `lifecycle_status="draft"` (the column default — no update
     needed).
   - Every **non-latest** version of any policy is backfilled to
     `lifecycle_status="superseded"`, `superseded_at` = the next
     version's `captured_at`, `superseded_by_version_id` = the next
     version's id — uncontroversial: a newer capture existing at all is
     sufficient signal that the older one is no longer current,
     regardless of whether formal review happened under the old model.
   - A **matching bootstrap `DomainEvent` row is inserted via raw SQL**
     for every backfilled transition (`PolicyVersionMadeEffective` for
     the "effective" case, `PolicyVersionSuperseded` for the
     "superseded" case), with `actor_type='migration'`,
     `actor_id=NULL`, `occurred_at`/`recorded_at` = the same timestamp
     used for the column backfill, and `aggregate_sequence=1` (the
     first-ever event for that now-newly-event-sourced aggregate — safe
     because this migration is the only place `"policy_version"` events
     can pre-date real application usage). This is what makes
     `rebuild_policy_version_lifecycle_projection` reproduce the exact
     same backfilled state later, not just the projection table at
     migration time — satisfying "projections must be rebuildable from
     events" for this aggregate going forward, and satisfying the
     acceptance criterion "existing policy data migrates without
     fabricated historical actions" (the `actor_type='migration'` marker
     is the fabrication guard: no event here ever claims a human
     reviewed/approved anything).
   - No event is backfilled for the `"draft"` case — there is nothing to
     project; the column default already matches.
4. `batch_alter_table`: add the `ck_policy_version_lifecycle_status`
   CHECK constraint (SQLite batch-mode, matching every prior CHECK-
   constraint-on-existing-table precedent in this repo).

Downgrade: drop the CHECK constraint, drop the eight new columns, drop
`policy_control_mappings`. The backfilled `DomainEvent` rows are **not**
deleted on downgrade (matching this repo's "never destructively rewrite
history" stance — `DomainEvent` rows are immutable and the schema
guards reject deleting them anyway); a downgrade-then-upgrade cycle
would re-run the same backfill logic against `Policy.status`/version
data, which is idempotent by construction (re-deriving the same
inference), not append duplicate events against a schema that no longer
has the columns to receive them meaningfully — acceptable for a
downgrade path whose primary purpose is schema rollback, not perfect
event-log symmetry.

## 7. Test strategy

- **Unit**: each of the four command functions — success path,
  precondition-violation `ValueError` for every invalid prior state,
  reject-requires-comment validation, projector correctness in
  isolation.
- **Integration — supersede side effect**: making version 2 effective
  while version 1 is currently effective supersedes version 1 in the
  same transaction; verify both projections and both underlying events
  exist.
- **Regression**: `retire_policy` on a policy with an effective version
  also withdraws it; existing upload/download/security tests
  (`tests/test_policies.py`) unaffected.
- **Immutability proof**: after a version becomes `"effective"`, its
  `sha256`/`byte_size`/`original_filename`/`uploader` are unchanged by
  any subsequent lifecycle action — only the lifecycle columns move.
- **Rebuild**: `rebuild_policy_version_lifecycle_projection` reproduces
  identical lifecycle state after a full draft→review→approve→
  effective→supersede sequence, wiped and replayed.
- **Migration tests (SQLite + Postgres-gated)**: seed pre-migration
  `Policy`/`PolicyVersion` rows in each of the three `Policy.status`
  values with 1-3 versions each; run the migration; assert the exact
  backfill rules in §6, assert bootstrap `DomainEvent` rows exist with
  `actor_type='migration'`, assert the new CHECK constraint rejects an
  invalid `lifecycle_status`.
- **Authorization**: reader/auditor get 403 on all five new routes, no
  state change (extends `tests/test_rbac.py`'s established pattern).
- **Browser/E2E + headless UAT**: draft → submit → approve → effective
  → new draft → submit → approve → effective (supersedes the first) →
  verify the old version's bytes/hash/approval facts are unchanged and
  it now reads "superseded."
