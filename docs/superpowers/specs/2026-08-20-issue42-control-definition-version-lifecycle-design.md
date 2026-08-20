# Issue #42: Extend the compliance-version lifecycle to control definitions

Parent epic: #10 (SOC 2 Type II-first) and #21 (event-centric persistence,
P0). Depends on #31 (merged), #11 (merged).

## 1. Repository-reality check

- **#31's `PolicyVersion` lifecycle is hybrid, not fully event-sourced.**
  `PolicyVersion` pre-existed #31 as a plain-inserted file-capture row;
  #31 bolted on event-sourced *lifecycle columns* only (`lifecycle_status`
  and everything below it), never the row's own existence. `InternalControlVersion`
  has no such pre-existing plain-insert use — it is a brand-new table,
  so it should be **fully event-sourced from creation**, the same fork
  in the road #32's design doc already reasoned through for
  `EvidenceArtifactVersion` ("this is a brand-new table with no
  pre-existing rows to preserve, so there is no reason to keep it a
  plain insert").
- **No existing route edits `InternalControl.name`/`description`/
  `status`/`review_frequency` after creation.** Grepped every router —
  `app/routers/controls.py` only has owner-assignment, occurrence
  generation/creation, and requirement-mapping routes. Rows are only
  ever constructed by `app/seed.py` (dev seeding) and
  `app/starter_controls.py::generate_starter_controls_for_framework`
  (onboarding). This means introducing the version-lifecycle mechanism
  requires **no rewiring of any existing edit path** — there isn't
  one. `InternalControl`'s own plain columns stay exactly as they are
  today (same precedent as `Policy.title`/`description`, which #31
  never touches either); `InternalControlVersion` is a new, additional,
  governed history alongside them, not a replacement.
- **`InternalControl` rows are not typically empty.** Starter-control
  generation creates real rows during onboarding, so a #31-style
  deterministic migration backfill is required, not "assume empty."
  Going forward, `generate_starter_controls_for_framework` itself must
  also create an initial effective version for every control it
  creates — the migration only covers rows that exist *at migration
  time*, not future onboardings.
- **No "procedure" concept exists anywhere** (confirmed by grep across
  `app/models.py`, `docs/product-scope.md`, `docs/domain/domain-model.md`).
  The only hit, `ControlTest.procedure_description`, is an unrelated,
  already-settled concept (a test's methodology narrative, #13) that
  offers no evidence either way. Decision (§6): no `Procedure` model.
  No code change to `Policy` either — nothing today needs to author a
  "procedure"-flavored document, so adding a speculative
  `document_type` field now would be exactly the infrastructure-before-
  a-concrete-use RULES.md warns against. The decision is recorded, not
  implemented ahead of need.
- **`ControlRequirementMapping` has no history at all** — current rows
  are the only state. A control-definition version needs to snapshot
  this many-to-many set as of that version; a plain JSON array of
  `requirement_id`s is sufficient (no new versioned join table).

## 2. `InternalControlVersion` (new, fully event-sourced)

```python
CONTROL_DEFINITION_LIFECYCLE_STATUSES = (
    "draft",
    "in_review",
    "approved",
    "effective",
    "superseded",
    "withdrawn",
)


class InternalControlVersion(Base):
    __tablename__ = "internal_control_versions"
    __table_args__ = (
        UniqueConstraint("control_id", "version_number", name="uq_internal_control_version_number"),
        CheckConstraint(f"lifecycle_status IN {CONTROL_DEFINITION_LIFECYCLE_STATUSES}", ...),
        Index(
            "uq_internal_control_version_one_effective_per_control",
            "control_id",
            unique=True,
            sqlite_where=text("lifecycle_status = 'effective'"),
            postgresql_where=text("lifecycle_status = 'effective'"),
        ),  # same partial-unique-index guard as PolicyVersion
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    version_number: Mapped[int]

    # The snapshot itself — what this issue actually closes the gap on.
    name: Mapped[str]
    description: Mapped[str]
    status_snapshot: Mapped[str]  # InternalControl.status as of this version
    review_frequency_snapshot: Mapped[str]  # InternalControl.review_frequency as of this version
    mapped_requirement_ids_json: Mapped[str]  # JSON array snapshot of ControlRequirementMapping

    # Event-sourced lifecycle — same shape/state machine as PolicyVersion.
    lifecycle_status: Mapped[str] = mapped_column(default="draft")
    drafted_at: Mapped[datetime.datetime]
    drafted_by: Mapped[str | None]
    submitted_by / submitted_at
    reviewed_by / reviewed_at / review_decision / review_comment
    effective_at
    superseded_at / superseded_by_version_id  # FK to internal_control_versions.id
    withdrawn_at / withdrawn_reason

    control: Mapped[InternalControl] = relationship(back_populates="versions")
```

`InternalControl` gains `versions` (same `cascade="all, delete-orphan"`,
`order_by=".version_number.desc()"` shape `EvidenceArtifact.versions`
already uses for its own fully-event-sourced children) and an
`effective_version` property mirroring `Policy.effective_version`
exactly.

Separate constant from `POLICY_VERSION_LIFECYCLE_STATUSES` even though
the values coincide — matching this codebase's existing convention of
never sharing a literal tuple object across otherwise-independent
domains (e.g. `CONTROL_PERIOD_STATUSES` vs. `CONNECTION_TEST_STATUSES`),
so the two lifecycles can evolve independently later without a
surprising shared-object coupling.

## 3. Lifecycle module (`app/control_definition_lifecycle.py`, new)

Mirrors `app/policy_lifecycle.py`'s exact shape — same event types
minus the mechanical rename, same command-level validation, same
"supersede-then-make-effective, one flush at the end" ordering for
`make_version_effective`:

- `InternalControlVersionDrafted` — the only event that *creates* the
  row (unlike `PolicyVersionSubmittedForReview`, which mutates an
  already-plain-inserted row). Payload: `control_id`, `version_number`,
  `name`, `description`, `status_snapshot`, `review_frequency_snapshot`,
  `mapped_requirement_ids`.
- `InternalControlVersionSubmittedForReview` (empty payload).
- `InternalControlVersionReviewed` (payload: `{decision, comment}` —
  `"approved"` → `lifecycle_status="approved"`; `"rejected"` →
  back to `"draft"`, identical to `PolicyVersionReviewed`).
- `InternalControlVersionMadeEffective` (empty payload) — supersedes
  the previous effective version of the same control first, in the
  same transaction, one flush at the end (identical ordering rationale
  to `make_version_effective`'s own docstring: reversing the order
  would momentarily need two "effective" rows to coexist, which the
  partial unique index correctly rejects even for a single caller).
- `InternalControlVersionSuperseded` (payload: `{superseded_by_version_id}`).
- `InternalControlVersionWithdrawn` (payload: `{reason}`) — usable
  from any non-terminal state, same as `withdraw_version`.

```python
def draft_control_definition_version(
    session,
    control,
    *,
    name=None,
    description=None,
    status_snapshot=None,
    review_frequency_snapshot=None,
    mapped_requirement_ids=None,
    actor_type="user",
    actor_id=None,
) -> InternalControlVersion:
    """Every snapshot field defaults to the control's own CURRENT value
    when omitted — makes "snapshot exactly what's on the row right now"
    (the migration backfill's and starter-control-generation's use case)
    a one-line call, while still allowing an explicit override for a
    real future revision proposal."""


def bootstrap_initial_effective_version(session, control, *, actor_type="system", actor_id=None):
    """draft -> submit -> approve -> effective in one call, tagged
    actor_type="system" — the sequence app/starter_controls.py uses for
    every newly onboarded control so it starts with a real effective
    version (the migration backfill in §4 covers only rows that exist
    at migration time; this covers every control created afterward)."""
```

### 3.1 Findings from self-review, fixed before shipping

Two real bugs surfaced while writing this module's own tests, both
fixed and both now regression-tested:

1. **Stale-relationship bug in `draft_control_definition_version`.**
   This app's session factory uses `expire_on_commit=False` (same class
   of bug found and fixed in `app/connectors/google_drive_capture.py`,
   issue #28): a long-lived `control` object's `.versions`/`.mappings`
   relationships go stale across repeated drafts against the same
   control within one session, so `next_version_number` computed the
   same number twice, tripping `uq_internal_control_version_number`.
   Fixed by calling `session.refresh(control)` at the top of the
   function rather than requiring every caller to remember. Regression:
   `test_version_numbers_increment_per_control`.
2. **A real rebuild-replay ordering bug, unique to this module.**
   `app/policy_lifecycle.py`'s projectors never flush — `PolicyVersion`
   rows pre-exist every lifecycle event, so nothing forces an
   intermediate flush during that module's rebuild replay, and the
   entire replay for a policy accumulates purely in memory until the
   caller's own final commit. `InternalControlVersion` is different:
   `_project_drafted` must flush to create each new row (a later event
   for the same aggregate needs to find it). During rebuild replay,
   that unrelated flush can land between one aggregate's own
   `Superseded` mutation and a *different* aggregate's pending
   `MadeEffective` mutation, batching them into one later flush whose
   internal statement order is not guaranteed to apply "superseded"
   before "effective" — reproducibly (not flakily — confirmed via 5
   repeated full-file runs before and after the fix) violating
   `uq_internal_control_version_one_effective_per_control` for exactly
   the length of that flush. Fixed by having `_project_superseded` and
   `_project_made_effective` each flush their own mutation immediately
   as it is applied, rather than deferring to one flush at the end of
   `make_version_effective` (which only ever protected the live-call
   path, not replay). Regression:
   `test_rebuild_internal_control_version_projection_reproduces_state`.

## 4. `ControlOccurrence` gains `control_definition_version_id`

Nullable FK to `internal_control_versions.id`, populated by
`generate_occurrences`/`record_occurrence_manually` at *generation*
time from `control.effective_version.id if control.effective_version else None`
— closing the exact gap `docs/superpowers/specs/2026-08-15-issue11-
control-operations-lifecycle-design.md` §10 risk #4 names. A later
edit to the control's definition (a new draft/approve/effective cycle)
never changes an already-recorded occurrence's reference, because the
occurrence snapshots the version **id**, and `InternalControlVersion`
rows are immutable historical facts once created — the same guarantee
`ControlOccurrence` already gives `responsible_person_id`.

**Pre-existing occurrences are never retroactively populated.** Per
RULES.md's "never invent historical events for existing rows," an
occurrence generated before this feature existed has no reliable way
to know what the control's definition actually said at that moment —
`control_definition_version_id` stays `NULL` for those rows, an honest
gap rather than a fabricated reconstruction. Only occurrences generated
*after* this ships get a real reference.

## 5. Migration and backfill

New table (`internal_control_versions`), new nullable column
(`control_occurrences.control_definition_version_id`), plus a
deterministic backfill mirroring #31's exact technique
(`migrations/versions/c4e8a7f2b391_...py`, read directly): raw
`sa.table(...)` proxies, no app-code imports, one bootstrap-tagged
(`actor_type="migration"`) `DomainEvent` per necessary transition,
directly paired with a raw row insert/update — never a full replay of
every intermediate state.

Concretely, for every existing `InternalControl` row: insert one
`InternalControlVersion` row directly (`version_number=1`,
`lifecycle_status="effective"`, snapshot = the row's current
`name`/`description`/`status`/`review_frequency` plus its current
`control_requirement_mappings` set as JSON, `effective_at =
control.updated_at`), and two bootstrap `DomainEvent`s against that new
aggregate id — `InternalControlVersionDrafted` (this table has no
pre-existing plain-insert row, so unlike #31's single tail event, the
*creating* event must be backfilled too) then
`InternalControlVersionMadeEffective`. No event claims a human drafted
or approved anything — `actor_type="migration"`, `actor_id=None`.

**Idempotency across a downgrade/upgrade cycle** can't key on
`aggregate_id` the way #31 does (its aggregate ids are pre-existing
row ids; mine are freshly generated during the migration itself, so a
second run would generate different ones). Instead: before backfilling,
parse the `payload_json` of every existing `actor_type="migration"`,
`event_type="InternalControlVersionDrafted"` event for `aggregate_type
="internal_control_version"` and collect their `control_id`s — skip any
control already represented. `internal_control_versions` (the table)
is dropped entirely on downgrade like any other new table; the
`DomainEvent` rows are never deleted (immutable), so this control-id
based check is what survives a downgrade-then-upgrade cycle correctly.

## 6. The "procedure" decision

No distinct `Procedure` model or `Policy.document_type` field is added
in this issue. Investigation (§1) found no existing "procedure as a
governed document" concept, no consumer that would create one, and no
issue/design doc describing a genuinely different lifecycle/relationship
shape a procedure would need beyond what `Policy` already provides
(draft → review → approved → effective → superseded, ownership,
content capture). If a real need for a distinguishable "procedure"
document type arises later, extending `Policy` with a
`document_type` field (`"policy" | "procedure"`) is the repository-
grounded path — smaller than a second parallel model — but building
that extension now, with nothing to use it, would be speculative.
This decision itself satisfies the acceptance criterion ("a documented
decision exists"); no schema change accompanies it.

## 7. Test strategy

- **Lifecycle** (mirrors `tests/test_policy_lifecycle.py`): draft →
  in_review → approved → effective → superseded/withdrawn; rejection
  returns to draft; invalid transitions rejected; at most one effective
  version per control (partial unique index); superseding order
  (previous superseded before new one effective, one flush).
- **Occurrence snapshot**: generating an occurrence after a control has
  an effective version records that version's id; editing/creating a
  new control-definition version afterward never changes the
  already-recorded occurrence's reference; an occurrence generated for
  a control with no effective version yet gets `NULL`, not a
  fabricated reference.
- **Migration/backfill** (mirrors `tests/test_policy_lifecycle_migration.py`):
  every existing control gets exactly one effective version reflecting
  its current fields/mappings; idempotent across downgrade→upgrade.
- **Starter controls**: `generate_starter_controls_for_framework`
  produces controls that already have a real effective version, not
  just migration-backfilled legacy ones.
- **SQLite/PostgreSQL equivalence** for the new schema (no
  backend-specific behavior introduced).
- **Regression**: existing #11/#31 suites remain green.

## 8. Definition of done

- `InternalControl` has explicit draft/approved/effective/superseded
  version history, event-backed via the same pattern as #31.
- Approved/effective control-definition versions are immutable
  historical facts (partial unique index + append-only events).
- Control occurrences reference the control-definition version
  effective at occurrence time, going forward.
- The "procedure" decision is documented, not silently skipped.
- No duplication of #31's event/projection mechanism.
