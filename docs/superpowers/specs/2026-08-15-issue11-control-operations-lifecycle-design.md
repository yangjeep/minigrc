# Issue #11: Operational control model for the SOC 2 Type II lifecycle

**Status:** design reconciled against the merged #22 event-store contract
(2026-08-17) — implementation follows in this branch,
`yangjeep/issue-11-control-operations`.
**Parent epic:** #10. **Depends on:** #22 (merged, `fda8631`) — this design
was originally written before #22 merged; **read §12 first**, it
supersedes §3.3/§5/§6/§7/§8 below on how `ControlOccurrence` history is
recorded. Sections §0–§2, §3.1, §3.2, §3.4, §9, §10, §11 are unaffected and
still describe the actual, current plan. **Author:** Claude (agent),
2026-08-15 (original design), reconciled 2026-08-17.

This is a design/gap-analysis document, produced per issue #11's explicit
instruction to research and document before writing code. It is grounded in
the actual current repository state (`app/models.py`, routers, migrations,
tests) as of commit `30969b6` on `main`/`yangjeep/epic`, not on the epic's
prose description of what supposedly already exists — see "Correcting the
epic's premise" below for why that distinction matters here.

## 0. Correcting the epic's premise (read this first)

Issue #10 says: *"Preserve the multi-framework foundation already introduced
under Feature 14."* This is factually wrong about the current repository
state, and it's worth stating plainly rather than quietly working around it:

- Grepping the whole repo for `Feature [0-9]+` shows shipped, committed
  features run from **Feature 1** through **Feature 13** ("Final
  hardening", `docs/worklog/2026-07-20-final-hardening.md`). There is no
  "Feature 14" anywhere in `main`'s history.
- "Feature 14" exists **only** as a design document — 1018 lines, three
  commits, zero code — on an unmerged sibling branch,
  `origin/yangjeep/dev-soc2-2` (`docs/superpowers/specs/2026-07-21-feature14-multiframework-foundation-design.md`).
  That branch's merge-base with `main` is `main`'s own tip, so it is purely
  additive and uncommitted anywhere else. Its title is *"Feature 14 (slice
  1/4): Multi-framework foundation — ISO 27001 + SOC 2"* and its entire
  scope is `Framework`/`FrameworkCategory`/`FrameworkRequirement` catalog
  reconciliation, legacy-ISO conflict detection, demo-seed gating, and
  canonical-field immutability. **It never touches `InternalControl`.**
- There is also an unrelated numbering collision worth naming explicitly so
  nobody confuses the two while reading the epic: the epic's own child
  issue **"#14"** (*"Build a continuous SOC 2 readiness work queue"*) is a
  completely different thing from the engineering label **"Feature 14"**
  (the catalog-reconciliation design above). They happen to share a number
  by coincidence.

**Conclusion, not treated as a blocker:** the actual multi-framework
foundation that exists *today*, in committed code, is exactly what
`app/models.py` shows — `Framework`, `FrameworkRequirement`,
`InternalControl`, `ControlRequirementMapping`, `RequirementAssessment`,
`RequirementNote` — already framework-agnostic (a control can map to
requirements across multiple frameworks with zero schema change; this was
true before "Feature 14" was ever proposed). This design targets *that*
schema. It does not depend on, block, or need to wait for the unmerged
Feature 14 slice 1 doc, because that doc's entire scope (catalog/category
data) and this issue's scope (control operational data) touch disjoint
tables. Either can land first. See §9 for the full compatibility argument.

## 1. Current-state architecture summary

(Traced from code, not docs — file:line citations throughout.)

**Shape.** One FastAPI process, one SQLAlchemy session per request
(`app/deps.py::get_db`, commit-on-success/rollback-on-exception), one
SQLite (or Postgres) database, server-rendered Jinja2, no queue, no
generic service/repository layer — routers call the ORM directly
(`docs/architecture.md`).

**The existing framework/control schema** (`app/models.py:37-193`):

```
Framework 1──* FrameworkRequirement 1──1 RequirementAssessment
                      │        └──* RequirementNote
                      │ *
                      ▼
             ControlRequirementMapping
                      ▲ *
                      │
InternalControl ──────┘
```

- `InternalControl` (`models.py:99-125`): `name`, `description`, `owner`
  (free `String`, **not** a `Person` FK), `status` (`not_started /
  in_progress / implemented / needs_review`), `review_frequency`
  (`monthly / quarterly / semiannual / annual`). **This field means "how
  often we re-review the control's written definition," not "how often the
  control operates"** — a distinction the current schema doesn't draw
  because it's never needed to. There is zero occurrence/operation
  tracking anywhere in the current schema. A control is either
  `"implemented"` or it isn't; nothing records that it ran this quarter.
- `EvidenceSnapshot` (`models.py:660-693`, AWS-connector-sourced only
  today) maps to controls/requirements via `EvidenceControlMapping` /
  `EvidenceRequirementMapping`, each `UNIQUE(evidence_snapshot_id,
  control_id)` / `UNIQUE(evidence_snapshot_id, requirement_id)` —
  i.e. one evidence row maps to a given control **at most once, ever**,
  with no period or occurrence dimension.
- `RequirementAssessment` (`models.py:152-174`) is a single rolling row per
  `FrameworkRequirement`, mutated in place (`app/routers/frameworks.py::update_assessment`,
  lines 298-367) — not period-scoped. This is framework-checklist state and
  is unrelated to #11's control-*occurrence* concern; it stays completely
  untouched by this design.
- `Person` (`models.py:235-257`) already exists as a shared identity
  primitive independent of `User`, and is already used for real FK-based
  ownership elsewhere: `VendorSystem.business_owner_person_id` /
  `primary_admin_person_id` / etc. (`models.py:294,299-300,316`),
  populated directly from a form dropdown
  (`app/routers/vendor_systems.py:159,162`) with no tie to the submitting
  session. `InternalControl.owner` predates this pattern and is still a
  plain string.

**Authorization** is exactly binary (`app/deps.py:37-82`): `require_login`
(any active, non-pending user) vs. `require_admin` (`role == "admin"`),
which today gates only: integration/connection config
(`app/routers/aws_connector.py:154` — confirmed `require_admin`, not just
`require_login`), Admin > Users/Connections/Jobs, and destructive vendor
ops. **Everything else — including all of `app/routers/controls.py` and
`app/routers/evidence.py` — is `require_login`-only.**
`docs/product-scope.md` states this as a stated non-goal, not an
oversight: *"No org switching, no per-user roles beyond logged in or not
in this MVP."*

**Audit trail** is one uniform helper, `record_audit_event(session, *,
entity_type, entity_id, action, detail="", actor="system")`
(`app/audit.py:10-27`), called in the same transaction as every mutation
that matters (`app/routers/frameworks.py:359-366`,
`app/routers/controls.py:94-101`, `app/routers/evidence.py:96-103,129-136`,
etc.). `actor` is **always** `request.state.user.email` from the
authenticated session — never client-supplied form input — a real,
consistently-applied invariant, confirmed across every router that calls
it.

**Generic CRUD** (`app/registers/config.py`, `app/registers/router.py`,
used by `CONTROLS_REGISTER_CONFIG` in `app/routers/controls.py:23-38`) is
strictly field-level create/read/update/delete (+ bulk field update) —
`RegisterConfig`'s `RegisterAction` is a closed `list/create/edit/delete`
literal (`config.py:10`) and there is no mechanism to add a custom
non-CRUD action route inside it, nor to make a field or the delete action
conditionally read-only per row (only globally, per whole register). A
worthwhile side-finding: `CONTROLS_REGISTER_CONFIG` sets neither
`deletable` nor `require_admin_for`, so **any logged-in user can currently
delete an `InternalControl`** via `DELETE /api/registers/controls/{id}`,
which ORM-cascades its `ControlRequirementMapping` rows
(`cascade="all, delete-orphan"`, `models.py:119-121`) but has no
relationship-level cascade toward `EvidenceControlMapping` — if any exists
for that control, the delete raises an unhandled `IntegrityError` that
surfaces as a generic 500 (`app/routers/*` has no try/except around
`delete_row`, unlike `create_row`'s explicit `IntegrityError` catch). This
is a **pre-existing latent bug, not introduced by this design** — flagged
in §10 because #11 measurably increases its blast radius (a control now
commonly has real occurrence history hanging off it, making an accidental
delete more consequential) without being in scope to fix here.

**Precedents this design leans on heavily:**
- *Computed, never stored, driftable flags*: `app/vendor_flags.py::compute_flags`
  computes operational warnings live at request time rather than storing a
  status that can go stale.
- *Owner-as-FK-with-fallback*: `VendorSystem.business_owner_person_id` (a
  real, shipped `Person` FK) — the correct precedent for adding
  `InternalControl.owner_person_id`, not the unshipped Feature-14-slice-1
  doc's `display_name_override` idea (an earlier draft of this document
  cited the wrong one; corrected here).
- *Action route template*: `app/routers/frameworks.py::update_assessment`
  — lookup + 404 guard, `Depends(verify_csrf)`, validate with
  redirect-with-flash before any mutation, snapshot before/after state,
  mutate in place, one `record_audit_event` call per logical change,
  everything riding the single implicit commit in `get_db`'s teardown. This
  is the template for every new mutating route below.
- *Migration shape*: `migrations/versions/bef6beccbfd1_add_external_connections_table.py`
  (new table: one `op.create_table` with explicit columns/constraints) and
  `migrations/versions/8961da81a764_add_user_status_and_google_subject.py`
  (new nullable column + `op.batch_alter_table` for constraints on
  SQLite) are the two templates for this design's migration.

## 2. SOC 2 Type II lifecycle gap analysis

Verified against AICPA primary/authoritative sources and established
audit-practice explainers, explicitly separating official criteria from
common convention as the issue asks:

- **Type I vs. Type II.** A Type I report is a point-in-time opinion:
  controls were suitably *designed* as of one date. A Type II report
  covers a specified **review period** (commonly 3–12 months, most often
  6–12) during which the auditor tests whether controls were both
  suitably designed **and operated effectively throughout**. This is
  AT-C §205's own framing, not a convention. **Gap:** miniGRC has no
  concept of a review period at all today — `RequirementAssessment` is a
  single rolling row, `InternalControl.status` is a single current value.
- **TSC structure (AICPA, 2017 + 2022 revised points of focus).** Each
  Common Criterion (CC1–CC9) plus category (Security/Availability/
  Confidentiality/Processing Integrity/Privacy) is a criterion plus
  illustrative, non-mandatory *points of focus*. **"Operating cadence" is
  not an AICPA term anywhere in the TSC** — it is pure auditor/practitioner
  convention for how often a control's activity should recur so evidence
  can be sampled across a period. This matters directly: miniGRC must not
  hardcode SOC2-specific cadence rules into the generic model (see §9's
  leakage discussion) — cadence is a control-operations concept this repo
  needs regardless of framework, not something SOC2 dictates.
- **Testing procedures, population, sample.** AICPA attestation standards
  require inquiry **plus** at least one of observation, inspection, or
  re-performance — inquiry alone is insufficient. For each control, the
  auditor defines the **population** (every instance the control should
  have occurred within the period) and draws a **sample** to test.
  **Gap, explicitly out of scope here:** population/sampling/test-
  procedure/result belongs to #13, not #11. #11 only needs to make the
  population *computable* later (i.e., produce real occurrence rows #13
  can query and sample from) — it must not attempt sampling itself.
- **Design effectiveness vs. operating effectiveness — the single most
  important distinction for this design.** A Type II opinion has two
  separate legs: controls were suitably *designed*, and controls
  *operated effectively* throughout the period. A control instance simply
  *occurring* only supplies evidence toward the second leg for **that one
  instance** — it does not by itself establish operating effectiveness for
  the whole period, which requires seeing every instance due in the
  window, dated and consistent, with no unremediated gaps. **This is
  exactly why #11 must stop at "this occurrence happened" and must not
  produce any pass/fail/compliant/effective conclusion** — that
  aggregation-and-conclusion step is #13's testing lifecycle, operating
  over the population of occurrences #11 produces.
- **Typical cadences (practice, not AICPA mandate).** Quarterly access
  reviews (four evenly-spaced cycles across 12 months, not one review
  right before the audit), annual incident-response tabletops (at least
  one dated inside the audit period), periodic vulnerability scanning.
  The consistent theme: auditors evaluate consistency against whatever
  cadence the *organization itself* committed to — not a codified AICPA
  quota. **This confirms the cadence model must be organization-configured
  per control (an integer interval), not a fixed enum of "the" SOC2
  cadences** — directly shapes §5 below.

## 3. Proposed domain model and relationships

All additive. No existing table is redefined; no existing column's type,
nullability, or meaning changes.

```
InternalControl (existing, +3 nullable columns)
  ├─ cadence_type: "calendar" | "event_driven" | NULL
  ├─ cadence_interval_months: int | NULL   (only meaningful when calendar)
  └─ owner_person_id: FK → Person | NULL   (existing free-text `owner` untouched)
        │
        │ 1───*
        ▼
  ControlOccurrence (new)
        │ 0..1
        ▼
  ControlPeriod (new, optional grouping — NOT required to exist)

ControlOccurrence ──*───* EvidenceSnapshot   via ControlOccurrenceEvidence (new)
```

### 3.1 `ControlPeriod` (not `AssessmentPeriod` — see naming rationale)

Deliberately **not** named "AssessmentPeriod" (would collide semantically
with the existing `RequirementAssessment`) and deliberately **not** named
"AuditPeriod" either — an earlier draft of this design used that name, but
review found it imports SOC2 Type II vocabulary ("the window a report's
opinion covers") into what must remain a framework-neutral concept; an
ISO-only deployment has no equivalent notion and would reasonably ask
"audit of what, by whom?" `ControlPeriod` is the neutral name.

```python
CONTROL_PERIOD_STATUSES = ("planned", "active", "closed")


class ControlPeriod(Base):
    __tablename__ = "control_periods"
    __table_args__ = (
        CheckConstraint("ends_on > starts_on", name="ck_control_period_dates"),
        CheckConstraint(f"status IN {CONTROL_PERIOD_STATUSES}", name="ck_control_period_status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. "SOC 2 Type II — H1 2026"
    starts_on: Mapped[datetime.date] = mapped_column(nullable=False)
    ends_on: Mapped[datetime.date] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
```

No FK to `Framework` — periods are framework-neutral time windows,
consistent with the epic's own stated principle ("frameworks map onto
controls; they do not own operational truth").

**`status` has real, enforced meaning** (fixing an early-draft gap where it
was a label nobody read): `"planned"` periods accept no occurrence
generation or association yet (lets an admin define next year's period
ahead of time without it being live); `"active"` periods accept
generation and manual association; `"closed"` periods reject further
generation/association, and — once closed — `starts_on`/`ends_on` become
immutable through the update route (only `name` stays editable), with
`closed_at` recording the transition separately from the shared
`updated_at`, so a later rename doesn't erase the historical fact of when
it closed.

**`ControlPeriod` is optional infrastructure, not a mandatory gate** — see
§5: the default occurrence-generation path does not require a period to
exist at all. This directly avoids forcing an ISO-only deployment (no
Type-II-style period concept) to fabricate a fake period just to unlock
scheduled generation.

### 3.2 `InternalControl` additions

```python
CADENCE_TYPES = ("calendar", "event_driven")

# added columns, all nullable — every existing row gets NULL, meaning
# "occurrence tracking not configured," inventing no retroactive
# obligation for existing data:
cadence_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
cadence_interval_months: Mapped[int | None] = mapped_column(nullable=True)
owner_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
```

`review_frequency` is **left completely unchanged** and keeps its existing
meaning ("how often we re-review the control's written definition") — a
deliberately distinct concept from operating cadence, so an existing,
tested field isn't silently repurposed. `cadence_interval_months` is a
plain integer (not a second string enum mirroring `review_frequency`) so
"every N months" generalizes past the four preset labels
(monthly/quarterly/semiannual/annual) if an organization's real policy is
"every 2 months" — directly satisfying the issue's explicit instruction
not to hardcode only the common calendar buckets. The UI still offers
1/3/6/12 as one-click presets; the column just isn't limited to them.

`owner_person_id` is nullable, alongside the untouched free-text `owner`
string. "Effective owner" resolves to `owner_person_id`'s `Person` if set,
else the legacy string — the same shape `VendorSystem` already uses for
its owner-ish fields, not a new pattern.

### 3.3 `ControlOccurrence` — the core new table

One row type for both "expected" and "actual" (not a two-table split,
which would duplicate almost every field for no benefit):

```python
OCCURRENCE_ORIGINS = ("generated", "manual")


class ControlOccurrence(Base):
    __tablename__ = "control_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "control_id", "control_period_id", "due_at", name="uq_occurrence_control_period_due"
        ),
        CheckConstraint(f"origin IN {OCCURRENCE_ORIGINS}", name="ck_occurrence_origin"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    control_period_id: Mapped[str | None] = mapped_column(ForeignKey("control_periods.id"), nullable=True)

    due_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    performed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    responsible_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    performed_by_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)

    scope_note: Mapped[str] = mapped_column(Text, default="")
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    control: Mapped[InternalControl] = relationship()  # no cascade -- see §4
    control_period: Mapped[ControlPeriod | None] = relationship()
    responsible_person: Mapped[Person | None] = relationship(foreign_keys=[responsible_person_id])
    performed_by: Mapped[Person | None] = relationship(foreign_keys=[performed_by_person_id])
```

Design notes, each directly answering a specific issue requirement or
critique finding:

- **No `attested_at`/`attested_by_person_id`/`attestation_note`.** An
  earlier draft had these as a separate review/sign-off stage. Cut after
  review found three compounding problems: (a) nothing in a single-tenant,
  no-RBAC app can actually distinguish "who may attest" from "who may
  perform," so it was a second copy of `performed_at`/`performed_by`
  under different names; (b) "attestation" is itself loaded assurance
  vocabulary (AICPA attestation *engagements* are literally what SOC 2
  reports are) and nothing would stop a future PR from treating it as an
  effectiveness sign-off; (c) its server-stamped-vs-user-entered semantics
  were never pinned down, making it a backdating vector with no
  legitimate use case (attesting is an act of confirmation *now*, not a
  historical fact logged later, unlike `performed_at`). **Resolution:**
  the issue's required "human attestation/review where required" concept
  is satisfied by the act of an authenticated session submitting
  `POST /occurrences/{id}/perform` itself, captured via the existing
  `record_audit_event(actor=request.state.user.email)` mechanism (§7) —
  no new schema needed. If #13 later needs a genuinely distinct
  maker/checker split (tester vs. reviewer, with real enforcement), that's
  a second, different concept with its own second caller, built there.
- **`due_at`/`performed_at` nullable, independently.** `due_at` is null
  for pure event-driven occurrences with no schedule. `performed_at` is
  null until actually done. Distinct from `created_at` (always
  server-stamped at INSERT) so a late-logged/backdated `performed_at`
  claim remains *visible* by comparing the two — though see §7 for why
  `created_at` alone is an insufficient backdating check for `origin =
  "generated"` rows, and what actually closes that gap.
- **"Missing/overdue" is a computed predicate**
  (`due_at < now() AND performed_at IS NULL`), **never** a stored/mutable
  status column — directly following `app/vendor_flags.py::compute_flags`'s
  existing precedent of computing operational warnings live rather than
  storing something that can drift.
- **`UNIQUE(control_id, control_period_id, due_at)`** makes generation
  idempotent (regenerating for the same control+period+due-date is a
  no-op) while still allowing unlimited manual/event-driven rows (`due_at`
  and/or `control_period_id` `NULL`) — SQLite and Postgres both treat
  `NULL` as distinct from `NULL` for uniqueness, so this is correct SQL
  semantics, not a hand-wave. Known, accepted edge case: a manual entry
  that happens to exactly coincide with a generated occurrence's
  `(control_id, control_period_id, due_at)` triple will collide and be
  rejected — treated as intentional dedup, not a bug, and rare enough not
  to warrant `origin` in the key.
- **No cascade from `InternalControl` to `ControlOccurrence`.** The
  relationship is deliberately declared with **no** `cascade=` argument
  (defaults to save-update/merge only) and the FK carries no `ondelete=`
  clause, matching this repo's existing convention (no `ondelete=` appears
  anywhere in `app/models.py` today) — under `PRAGMA foreign_keys = ON`
  this means SQLite/Postgres both reject deleting a control that has
  occurrence rows, by default (`RESTRICT`-like `NO ACTION`), rather than
  silently destroying history. **Implementation note, not just a design
  aside:** `InternalControl.mappings` a few lines above in the same file
  *does* use `cascade="all, delete-orphan"` — an implementer copy-pasting
  that pattern onto the new `occurrences` relationship by analogy would
  silently reintroduce cascade-delete of occurrence history. Worth an
  explicit "do not cascade" comment at the point of implementation.
- **`performed_by_person_id` is a freely-selected `Person` FK, matching
  `VendorSystem`'s existing pattern** — populated from a dropdown, with
  **no** enforced tie to the submitting session. This is intentional and
  matches house style (`owner`, `responsible_person_id`, etc. are all
  unenforced accountability metadata) — but it means the row alone cannot
  prove *who actually submitted the claim*. That's solved separately, by
  audit trail, not by gating: see §7.

### 3.4 `ControlOccurrenceEvidence`

```python
class ControlOccurrenceEvidence(Base):
    __tablename__ = "control_occurrence_evidence"
    __table_args__ = (
        UniqueConstraint("occurrence_id", "evidence_snapshot_id", name="uq_occurrence_evidence"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("control_occurrences.id"), nullable=False)
    evidence_snapshot_id: Mapped[str] = mapped_column(ForeignKey("evidence_snapshots.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
```

A new, minimal join table — **not** a modification of the existing
`EvidenceControlMapping`. Considered extending it with a nullable
`occurrence_id` instead, but rejected: its existing
`UNIQUE(evidence_snapshot_id, control_id)` would need to become
`UNIQUE(evidence_snapshot_id, control_id, occurrence_id)`, and because
`NULL != NULL` for uniqueness in both dialects, that change would
silently weaken today's "one mapping per (evidence, control)" guarantee
for all-`NULL`-occurrence rows (today's exact, only use case) unless a
dialect-specific partial/filtered unique index were added — something
this repo has deliberately avoided elsewhere
(`docs/architecture.md`: *"no SQLite-specific raw SQL outside
build_engine's pragma listener"*). A separate table sidesteps this with
zero risk to existing evidence-mapping behavior.

**Named-scope caveat, stated explicitly rather than left implicit:** this
table only links an occurrence to the AWS-connector-sourced
`EvidenceSnapshot` — the only evidence storage that exists today. #13 will
almost certainly need to build the first *general* evidence-upload
mechanism (nothing generic exists yet; `Policy` uploads are a distinct,
document-specific pipeline) and will likely add its own
occurrence-to-generic-evidence link then. `ControlOccurrenceEvidence` is
correctly scoped to what exists now, not a premature abstraction — but
future readers should know its generic-sounding name currently covers only
one evidence source.

For occurrences with no connector evidence (the common manual/event-driven
case), `evidence_note` (free text on the occurrence itself) is the
minimal fallback — sufficient for #11, deliberately not attempting to
build a new upload pipeline, which is explicitly #13's territory.

## 4. Migration/backfill strategy

Every change here is additive; there is no backfill *problem*, only
backfill *absence-of-need*, which is itself worth stating explicitly:

- `control_periods`, `control_occurrences`, `control_occurrence_evidence`
  are brand-new tables — nothing to migrate into them.
- `internal_controls` gains three nullable columns
  (`cadence_type`, `cadence_interval_months`, `owner_person_id`) with no
  `server_default` needed (nullable, no NOT NULL requirement) — every
  existing row gets `NULL` across the board, which correctly means
  "occurrence tracking not configured for this control yet." **This is a
  deliberate choice, not an oversight:** backfilling e.g.
  `cadence_type='calendar'` from the existing `review_frequency` value
  would silently assert that every pre-existing control (including
  placeholder/demo ones) is now subject to occurrence-tracking and
  overdue detection it never had — inventing compliance obligations
  retroactively. An admin/owner must explicitly opt a control in.
- Two migrations, following the two confirmed existing templates:
  1. `op.create_table("control_periods", ...)`, `op.create_table("control_occurrences", ...)`,
     `op.create_table("control_occurrence_evidence", ...)` — single-file
     shape identical to `bef6beccbfd1_add_external_connections_table.py`
     (explicit columns, `CheckConstraint`s, `ForeignKeyConstraint`s,
     `UniqueConstraint`s, plain `downgrade()` drops).
  2. `op.add_column("internal_controls", ...)` x3 (all nullable, no
     `batch_alter_table` needed for a bare nullable column add without a
     new constraint) — but the `cadence_type` `CheckConstraint` (enum-style,
     matching the existing `ck_user_role`/`ck_user_status` shape) **does**
     need `op.batch_alter_table("internal_controls", schema=None)` on
     SQLite, following `8961da81a764_add_user_status_and_google_subject.py`'s
     exact pattern (SQLite can't `ALTER TABLE ADD CONSTRAINT` directly;
     harmless no-op wrapper on Postgres).
- No existing row's `status`/`review_frequency`/`owner`/mappings/
  assessments are touched by either migration. `tests/test_framework_control_relationship.py`'s
  two locked-in behaviors (the DB-level unique-pair constraint on
  `ControlRequirementMapping`, and the bidirectional
  `control.mappings`/`requirement.mappings`/`requirement.framework`
  relationship chain) are untouched by this design and must still pass
  unmodified.

## 5. Expected-occurrence / cadence semantics

**Two cadence types, not one universal model** — directly per the issue's
warning against false universal assumptions:

- **`cadence_type = "calendar"`**: occurrences are proactively
  *materialized* ahead of their due date (so "missing occurrence" is a
  real, queryable row, not merely an absence the UI can't reason about —
  the issue's explicit requirement). Generation is a pure, deterministic
  function of `(control.cadence_interval_months, a start anchor)`.
- **`cadence_type = "event_driven"`**: no occurrence is ever
  pre-materialized. Rows are created reactively, `origin="manual"`, only
  when the triggering real-world event happens (e.g. an offboarding
  check). "Missing/overdue" simply does not apply to these — there is
  nothing to be overdue against without a separate trigger source (e.g. a
  future HRIS connector), which is out of scope for #11.
- **`cadence_type = NULL`**: no occurrence tracking configured — the
  control behaves exactly as it does today (design/status only, or
  continuous/automated evidence via the existing `EvidenceSnapshot`
  mapping, unchanged).

**Generation mechanism** (`app/control_occurrences.py`, a plain module —
matching the shape of `app/csv_import.py`/`app/vendor_flags.py`/
`app/requirements.py`, not a framework):

```python
def generate_occurrences(
    session: Session,
    control: InternalControl,
    *,
    period: ControlPeriod | None = None,
    horizon_months: int = 12,
    actor: str,
) -> list[ControlOccurrence]: ...
```

- Applies only to `cadence_type == "calendar"` controls with
  `cadence_interval_months` set; otherwise a no-op.
- **Period-scoped path** (`period` given, must be `status="active"`):
  compute due dates at the configured interval, starting from
  `period.starts_on`, through `period.ends_on`.
- **Period-less, rolling default path** (`period=None`) — the primary,
  always-available path, added specifically so a `ControlPeriod` is never
  a precondition for ordinary cadence tracking (an ISO-only deployment
  with no Type-II-style period concept can use this exclusively): compute
  due dates starting from the later of "the last existing occurrence's
  `due_at` for this control" or "today," for `horizon_months` months
  ahead.
- Idempotent via a plain check-then-insert per computed date (`SELECT`
  existing `(control_id, period_id, due_at)` rows first, insert only the
  missing ones) — **not** the advisory-lock/`BEGIN IMMEDIATE` concurrency
  machinery an earlier draft borrowed from the unmerged Feature-14-slice-1
  doc. That mechanism solves a concurrent-writer race this feature
  doesn't have (this is a single admin- or owner-triggered, infrequent,
  single-tenant operation) — the `UNIQUE` constraint plus a plain
  check-then-insert is sufficient and matches this repo's actual KISS
  constraint rather than importing complexity from a design that hasn't
  shipped. One `record_audit_event` per row actually inserted.
- `due_at` is stored as **UTC, end of the computed calendar day**
  (`datetime.combine(computed_date, time.max, tzinfo=UTC)`), documented
  explicitly as a stated convention rather than left ambiguous — matching
  this app's existing all-UTC convention (`app/models.py::utcnow`) and its
  existing lack of any per-organization timezone setting anywhere in
  `app/config.py` (a known, accepted limitation shared with the rest of
  the app, not a new gap introduced here). The "overdue" predicate compares
  against `datetime.now(UTC)` for the same reason.
- **Cadence changes never retroactively touch existing occurrence rows.**
  Editing `cadence_type`/`cadence_interval_months` only affects *future*
  generation runs. If a mid-period cadence change means regeneration
  produces a different set of due dates than before, old rows are left
  exactly as they are (no auto-cleanup, no silent deletion) — this is the
  explicit, stated rule for historical stability, and #13/#14 should
  expect possibly-stale generated rows to coexist with corrected ones
  rather than assume exactly one clean set per control per period.
- **`ControlOccurrence.responsible_person_id` is snapshotted at generation
  time** from `control.owner_person_id` as it existed *then* — never a
  live join. Changing the control's owner later never rewrites past
  occurrences' historical responsible party. Directly tested (§8).

## 6. API/UI changes

Minimal, following existing router conventions (`app/routers/controls.py`,
`app/routers/evidence.py`, `app/routers/frameworks.py`) — no new UI
framework, no new template system.

| Route | Method | Authz | Notes |
|---|---|---|---|
| `/control-periods` | `GET` | `require_login` | list |
| `/control-periods` | `POST` | `require_admin` + CSRF | bulk/structural — see rationale below |
| `/control-periods/{id}` | `GET` | `require_login` | detail incl. occurrence rollup |
| `/control-periods/{id}/close` | `POST` | `require_admin` + CSRF | status→closed, stamps `closed_at`, audited |
| `/control-periods/{id}/generate-occurrences` | `POST` | `require_admin` + CSRF | bulk, all calendar-cadence controls |
| `/controls/{control_id}/occurrences/generate` | `POST` | `require_login` + CSRF | single-control rolling generation (period-less path), self-serve |
| `/controls/{control_id}/occurrences` | `POST` | `require_login` + CSRF | manual/event-driven creation; optional `control_period_id` from a dropdown of currently-`active` periods |
| `/occurrences/{id}/perform` | `POST` | `require_login` + CSRF | sets `performed_at`/`performed_by_person_id`/`scope_note`/`evidence_note`, optional evidence-snapshot link |
| Extend `GET /controls/{control_id}` | `GET` | `require_login` | occurrence list: upcoming / overdue / completed, computed not stored |

**Why the admin/login split isn't uniform:** an earlier draft put every new
route at `require_login`, reasoning "no new authorization tier, matching
`controls.py`/`evidence.py`." Review found the cited precedent was wrong
for the *bulk* operations specifically — `aws_connector.py`'s "Run checks
now" (the closest real analogue to "regenerate occurrences for everyone")
is `require_admin`, not `require_login`. The corrected split: **structural,
cross-control, or period-wide actions are `require_admin`** (creating/
closing a period, bulk generation across every control) — the same
category as connection config and "run checks now"; **individual,
single-control record-keeping stays `require_login`** (recording your own
occurrence, self-serve rolling generation for one control you can already
see) — the same category as `update_assessment` and control-mapping
edits. No new permission tier is invented; both tiers already exist and
are just applied to the correct action, per existing precedent rather than
guessed.

## 7. Authorization and integrity model

**The core stance, unchanged from the draft and confirmed correct by
review:** CLAUDE.md hard constraint #3 ("no RBAC beyond logged in or
not") means no route may gate an action on "are you the owner" — this
matches the existing, working pattern for `InternalControl.owner`,
`Risk.owner`, `RequirementAssessment.owner`, and `VendorSystem`'s
Person-FK owner fields, none of which are ever checked before allowing an
edit. `owner_person_id`, `responsible_person_id`, and
`performed_by_person_id` remain **accountability metadata, not
enforcement** — this is intentional and should not be changed.

**What review corrected: authorization ("who may act") and attribution
("who is recorded as having acted, and can that record be trusted") are
two different questions, and the draft under-specified the second one.**
`performed_by_person_id` is a freely form-selected `Person` — matching
`VendorSystem`'s existing pattern — so nothing prevents User A from
submitting `/occurrences/{id}/perform` and selecting Person B (a different
real employee) as `performed_by_person_id`. A 404/existence check on the
selected Person proves nothing about who actually submitted the form.
That's exactly the self-attestation/forged-actor scenario a SOC 2 auditor
tests for.

**The fix that adds zero new infrastructure and doesn't touch
authorization:** every mutating route in §6 **must** call
`record_audit_event(db, entity_type="control_occurrence",
entity_id=occurrence.id, action=..., actor=request.state.user.email,
detail=<before/after snapshot>)` in the same transaction — this is
required anyway by CLAUDE.md hard constraint #5 ("every mutation that
matters to an auditor writes an AuditEvent"), and an earlier draft
under-specified it (only the generation path mentioned audit events). This
gives an auditor two independently visible facts per occurrence: "Person X
is *claimed* as having performed this" (unenforced, editable, exactly as
intended) and "User Y's authenticated session is what *actually
submitted* that claim, at server time Z" (tamper-evident, since `actor` is
never client input anywhere in this codebase). Nobody is blocked; no
permission tier is added; this is the same "compute and expose, never
gate" philosophy already correctly applied to the missing/overdue
predicate, just applied to actor identity too.

**Mutability policy for `/perform`:** rather than either (a) hard-rejecting
a second call (which would force awkward workarounds like duplicate rows
to correct a mistake) or (b) silently allowing overwrite with no trace,
this design follows `update_assessment`'s established house pattern:
**allow re-submission, but every call snapshots before/after state into
the mandatory `AuditEvent`** — a correction is always possible, and always
visible in the append-only audit log, rather than hidden. This is more
aligned with "historical facts preserved via audit trail" than a hard
reject, and requires no new versioning table.

**Closing a real backdating gap:** comparing `performed_at` against
`created_at` (as an earlier draft proposed) does **not** detect backdating
for `origin="generated"` rows, because `created_at` reflects *generation*
time (often months before the work is due), not *submission* time.
`ControlOccurrence.updated_at` (`onupdate=utcnow`, added for exactly this
reason — every other mutable table in `models.py` already has one) plus
the mandatory `AuditEvent.created_at` on the `/perform` call are the real,
server-stamped anchors a `performed_at` claim should be compared against.

**Cross-scope integrity:** every new route 404s on a nonexistent
`control_id`/`control_period_id`/`evidence_snapshot_id`, matching the
existing `add_mapping`/`map_requirement` pattern exactly
(`app/routers/controls.py:80-83`, `app/routers/evidence.py:82-85`).
Attempting to generate occurrences against a non-`"active"` period, or
associate a manual occurrence with a `"closed"` period, is rejected the
same way (redirect-with-flash, no mutation), following `update_assessment`'s
validate-before-mutate shape.

## 8. Test strategy

New file `tests/test_control_occurrences.py`, using the existing fixtures
exactly as-is (`tests/conftest.py`: `logged_in_client`, `admin_client`,
CSRF scraped from a prior GET via `extract_csrf_token`,
`follow_redirects=False` + assert on the 303 `Location` header — the
pattern used throughout `tests/test_requirements.py`):

- **Generation determinism/idempotency**: a calendar-cadence control
  across a sample period produces the expected due-date set; calling
  generation twice produces no duplicate rows (`UNIQUE` constraint holds);
  regenerating after a `cadence_interval_months` change leaves prior rows
  untouched and only adds new ones going forward.
- **Event-driven/manual pattern**: a control with `cadence_type =
  "event_driven"` never gets auto-generated rows; a manually-created
  occurrence is recorded correctly with `origin="manual"`.
- **Missing/overdue detection**: an occurrence with `due_at` in the past
  and `performed_at IS NULL` is correctly surfaced by the computed query;
  one performed on time is not.
- **Actual performance + evidence linkage**: `/perform` sets
  `performed_at`/`performed_by_person_id`, optionally links an existing
  `EvidenceSnapshot` via `ControlOccurrenceEvidence`, and writes the
  required `AuditEvent` with the real session's `actor`.
- **Historical stability**: change `InternalControl.owner_person_id` and
  `cadence_type` after an occurrence exists; assert the existing
  occurrence's `responsible_person_id` is unchanged, and that newly
  generated occurrences use the new owner.
- **Authorization split**: `require_admin` enforced on period
  create/close/bulk-generate (403 for a non-admin `logged_in_client`);
  `require_login` sufficient for single-control generation, manual
  creation, and `/perform`.
- **Invalid cross-scope references**: 404 on a nonexistent `control_id`,
  `control_period_id`, or `evidence_snapshot_id` on every new route, with
  no partial mutation and no audit event (mirroring
  `test_credential_leakage.py`-style "no side effect on rejected request"
  assertions already used elsewhere).
- **Existing regression coverage preserved unmodified**: run
  `tests/test_framework_control_relationship.py` and
  `tests/test_requirements.py` unchanged — both must still pass exactly
  as today, proving `ControlRequirementMapping`/`RequirementAssessment`
  behavior is untouched.
- **Postgres**: extend `tests/test_postgres_compat.py` with the new
  tables/columns under its existing `skipif not TEST_DATABASE_URL` gate,
  matching its established pattern precisely (no new gating mechanism).

## 9. Compatibility analysis for #12–#15

- **#12 (SOC2-as-primary framework experience).** Fully orthogonal at the
  schema level: this design never adds a `Framework` FK to
  `ControlOccurrence`/`ControlPeriod`, and framework-scoped readiness is
  computed later by joining through the existing, unmodified
  `ControlRequirementMapping` chain — not by duplicating operational
  state per framework. No conflict with the unmerged Feature-14-slice-1
  catalog-reconciliation work either (disjoint tables entirely); either
  can land first.
- **#13 (evidence populations/sampling/testing/findings/remediation).**
  `ControlOccurrence` becomes the unit #13's population/sample/test
  tables reference (a stable UUID PK, already exactly what a `Test`/
  `Finding` row would FK to). The fact/effectiveness distinction is drawn
  precisely at this boundary: #11 records *that* an occurrence happened
  and who claims responsibility; #13 owns the *conclusion* about whether
  it was effective. One real, load-bearing gap to hand off explicitly:
  manual/event-driven occurrences only get `control_period_id` set if a
  user explicitly picks an active period from the dropdown (§6) — #13's
  population logic ("all occurrences of control C in period P") will need
  to handle occurrences with `NULL` period alongside FK-linked ones if it
  wants full coverage; this is a stated, not hidden, seam.
- **#14 (readiness dashboard).** Consumes the same computed
  overdue/missing predicate directly (`due_at < now() AND performed_at IS
  NULL`) — no new state needed, directly satisfying #14's own "derive
  from authoritative state, no second manually-maintained task database"
  principle. `cadence_interval_months` (added specifically after review
  caught its absence in an earlier draft) is exactly what lets #14 detect
  "a control has a cadence configured but this period's occurrence hasn't
  been generated yet" as a queue item, without inventing its own
  cadence-config field.
- **#15 (BYOK assistant).** `scope_note`/`evidence_note` are plain
  nullable text columns on a row a human still submits via
  `/occurrences/{id}/perform` — a clean pre-fill-only surface. The
  assistant never needs to touch `performed_at`, any FK, or the audit
  trail directly.
- **Segregation-of-duties tension, inherited not introduced.** #13's own
  stated ask ("authorization for tester/reviewer/finding updates") can't
  be satisfied by object-level access checks under CLAUDE.md's hard
  no-RBAC constraint. This tension exists regardless of what #11 does;
  #11 correctly declines to invent a new permission tier unilaterally to
  paper over it (§7's audit-trail-based attribution is the pattern to
  extend, not a preventive gate) — flagged here so whoever scopes #13
  makes that call explicitly rather than discovering the constraint
  mid-implementation.

## 10. Risks, ambiguities, and assumptions to resolve before coding

1. **The epic's "Feature 14 already exists" premise is false in committed
   code** (§0). Assumption made here: design against today's actual
   `InternalControl`/`Framework` schema, not the unshipped Feature-14-
   slice-1 fields (`is_system_provided`, `catalog_status`, etc.), since
   the two efforts touch disjoint tables and neither blocks the other.
   **This is the single most important thing for a human to confirm
   before implementation starts** — if there's a reason Feature 14 slice 1
   must land first that isn't visible from the repo alone (e.g. an
   external roadmap commitment), that changes sequencing, though not this
   design's schema.
2. **Pre-existing register-grid delete bug, blast radius grows.** Any
   logged-in user can delete an `InternalControl` today
   (`CONTROLS_REGISTER_CONFIG` sets no `deletable`/`require_admin_for`
   override), and doing so while `EvidenceControlMapping` rows exist
   already produces an unhandled 500 rather than a clean error. This
   design adds `ControlOccurrence` with a deliberately non-cascading FK —
   correct for history preservation — but means that same delete attempt
   will now also fail the same way once any occurrence exists, and
   occurrences will exist far more commonly than evidence mappings do
   today. **Not fixed in this design** (out of scope, pre-existing,
   unrelated to #11's own diff) but flagged loudly: recommend a small,
   separate follow-up (wrap `delete_row` in the same `IntegrityError`
   catch `create_row` already has, or a product decision on whether
   `InternalControl` should ever be deletable once occurrence history
   exists).
3. **`ControlPeriod`'s degree of usage is genuinely unknown.** The issue's
   acceptance criteria require an "assessment period" concept to exist,
   but §5's period-less rolling-generation path means a real deployment
   could operate #11 entirely without ever creating one. Worth confirming
   with a human whether `ControlPeriod` should be more strongly
   encouraged in the UI (e.g., a prominent nudge to create one) or is
   correctly left as an optional, discoverable-when-needed feature for
   #12's eventual SOC2 default experience to lean on harder.
4. **No versioning of `InternalControl` itself.** `ControlOccurrence`
   snapshots `responsible_person_id` (deliberately), but not the
   control's `name`/`description`/`status`/`review_frequency`/mapping set
   as of occurrence time — those can drift after the fact with no way to
   reconstruct "what did this control's definition say when this
   occurrence happened." Treated here as an acceptable, explicit scope
   cut for #11 (full control versioning would be a real, separate
   feature) — flagged so it's a stated decision, not a silent gap.
5. **`cadence_interval_months` UI presets vs. free entry.** Design intent
   is an integer column with 1/3/6/12 UI presets plus free entry for
   non-standard intervals. Worth confirming this is the right amount of
   flexibility to expose in the first UI pass, vs. shipping presets-only
   for #11 and loosening the UI later (the column itself doesn't need to
   change either way).
6. **(Added 2026-08-17, §12.3) Control-occurrence activity will be fully
   absent from `/admin/audit-log` and the Dashboard's recent-activity
   widget**, per the reconciliation's decision not to write a parallel
   `AuditEvent` for occurrence mutations. This is a real, non-trivial gap
   in existing admin visibility, not a partial degradation, and #11 ships
   no interim mitigation (full fix is issue #45, unscheduled). Worth a
   human's explicit confirmation before merge that this trade-off is
   acceptable for the release this ships in, versus, e.g., a minimal
   stop-gap (a short-lived `AuditEvent` row with only `action="control
   occurrence activity — see domain_events"` and no duplicated detail) if
   full visibility loss isn't acceptable yet.
7. **(Added 2026-08-17, §12.3) No lower bound on `occurred_at` backdating
   for `ControlOccurrencePerformed`.** Only a future-date rejection is
   enforced; how far into the past a claim may legitimately reach (e.g.
   relative to the control's cadence configuration date, or the
   enclosing period's `starts_on`) is a genuine product policy question
   left open rather than guessed at — worth a decision if real usage
   surfaces implausible backdated claims in practice.

## 11. Concrete implementation slices / commit plan

Each slice independently testable and revertible; suggested commit-per-slice:

1. **Schema + migration**: `ControlPeriod`, `ControlOccurrence`,
   `ControlOccurrenceEvidence` tables; `InternalControl` +3 nullable
   columns. Alembic migration(s) per §4. No routes, no UI yet.
   `tests/test_control_occurrences.py` schema/constraint-only tests
   (uniqueness, check constraints, FK integrity, no-cascade-delete
   behavior) — proves the schema before any behavior is built on it.
2. **Generation module**: `app/control_occurrences.py::generate_occurrences`
   (period-scoped and period-less paths), CLI command
   `python -m app.cli generate-control-occurrences` for manual/testable
   invocation. Determinism/idempotency/cadence-change tests.
3. **Routes — structural (admin)**: `/control-periods` CRUD +
   `/close` + `/generate-occurrences`. Authorization + validation tests.
4. **Routes — operational (login)**: `/controls/{id}/occurrences/generate`,
   `/controls/{id}/occurrences` (manual), `/occurrences/{id}/perform`.
   Mandatory `AuditEvent` on every mutation, before/after snapshot per
   §7. Historical-stability and forged-actor/audit-trail tests.
5. **UI**: extend `GET /controls/{control_id}` with the occurrence list
   (upcoming/overdue/completed, computed); minimal `control-periods`
   list/detail templates, following existing Jinja2 template patterns
   (no new frontend tooling).
6. **Docs/worklog**: `docs/architecture.md` gains a "Control operations"
   section describing the new tables and generation mechanism, following
   the existing per-area-subsection convention; a
   `docs/worklog/<date>-control-operations-foundation.md` entry per this
   repo's worklog template, once code lands (not before — worklog entries
   describe what changed, and nothing has changed yet).

Verification demo for the PR (mirrors the issue's own acceptance
criteria): create a `ControlPeriod` for a sample 6-month Type II window;
configure one control as `cadence_type="calendar"`,
`cadence_interval_months=3`; generate occurrences (two expected in the
window); mark one performed with a linked evidence snapshot; leave the
other overdue; confirm the overdue one is surfaced by the computed
predicate and the performed one is not; confirm the same control's
existing `ControlRequirementMapping` to an ISO 27001 requirement is
completely unaffected (framework mapping stays intact, no duplicated
operational state).

## 12. Reconciliation against the merged #22 event-store contract (2026-08-17)

This design was written on 2026-08-15, before #22 (event store, `app/events.py`)
existed on `main`. #22 merged 2026-08-17 (`fda8631`). Per issue #21's body
("control operations: expected occurrence established/materialized,
performed, evidence attached/reviewed" is explicitly listed among the
material compliance state requiring domain events — correcting an earlier
draft of this reconciliation, which mis-cited this quote as living in
`.agent/RULES.md`; it doesn't) and `.agent/RULES.md` §1 item 3 ("do not
confuse an audit log with event-backed truth — a mutable row plus a
descriptive audit entry is insufficient when the event itself is the
compliance fact"), `ControlOccurrence`'s lifecycle as designed in
§3.3/§5/§6/§7 above — a plain mutable row updated in place, described by
`record_audit_event` — is exactly the pattern #21/#22 exist to replace.
This section states the amendments precisely, changing only how history
is *recorded*, not the already-reviewed domain reasoning (cadence types,
field shapes, no-cascade FK, admin/login authorization split, naming
rationale) from §3–§10, which all remains correct and unchanged.

**Verification note (post-adversarial-review, 2026-08-17):** an
independent review of this reconciliation flagged that dropping
`record_audit_event` for occurrence mutations might conflict with a
CLAUDE.md "every mutation writes an AuditEvent" hard constraint. Checked
directly against the actual current `CLAUDE.md` and `.agent/RULES.md` on
this branch (`grep -rn "AuditEvent" CLAUDE.md .agent/RULES.md` — zero
hits in either file): no such constraint currently exists in either
document. That constraint existed in an earlier, now-superseded root
`CLAUDE.md` (before PR #29 replaced it with the current
event-centric-first version) — the review's own citation was almost
certainly working from a stale/cached copy rather than the file as it
actually stands on `main` today, the exact class of mistake
`.agent/RULES.md` §2 warns against. §12.3 below stands as originally
reasoned. Every other finding from that review that *did* check out
against real, current code is incorporated throughout this section.

### 12.1 What's superseded vs. unchanged

**Superseded** (persistence mechanism only — the field-level reasoning in
each referenced section still applies and is not repeated here):

- §3.3 `ControlOccurrence` as a plain mutable table with `origin`/
  `performed_at`/etc. columns written directly by routes.
- §5's generation mechanism as direct `session.add(ControlOccurrence(...))`.
- §6/§7's routes calling `record_audit_event` as the mechanism for
  historical/attribution integrity on the occurrence itself.
- §8's test list, extended (not replaced) — see 12.6.

**Unchanged:**

- §3.1 `ControlPeriod` stays a plain mutable row (`created_at`/`created_by`/
  `updated_at`, `record_audit_event` on create/close), matching this
  repo's existing pattern for comparable administrative/scheduling
  lifecycle state (`TrustCenterSection`'s draft/published, `Job`'s
  pending/running/succeeded/failed) rather than every entity adjacent to
  an event-backed domain needing its own event history. `ControlPeriod`
  is scheduling metadata that *gates* when occurrences get generated; it
  is not itself the material fact #21/#22 target. `Job`/`TrustCenterSection`
  are exactly this repo's own precedent for that line.
  **Acknowledged tension, not a clean-cut call** (raised by adversarial
  review): unlike `Job`/`TrustCenterSection`, a period's `starts_on`/
  `ends_on` boundary genuinely helps define which occurrences count as
  "in period" for #13's future population/sampling logic, so an edited
  period *while active* is closer to compliance-relevant history than
  disposable queue state. Kept as plain-mutable anyway because the
  mutation window is hard-bounded (§3.1's `status="closed"` locks
  `starts_on`/`ends_on` permanently, with `record_audit_event` still
  covering pre-close edits) — genuinely lower-stakes than an occurrence's
  own unbounded correction history. Revisit if #13 finds the bounded
  `AuditEvent` trail insufficient for period-boundary reconstruction in
  practice; don't event-source it speculatively now.
- §3.2's `InternalControl` additions (`cadence_type`,
  `cadence_interval_months`, `owner_person_id`) stay plain mutable columns,
  edited through the existing register-grid mechanism exactly like
  `status`/`owner`/`review_frequency` are today. §10 risk #4 already
  correctly deferred *all* `InternalControl` definition-versioning to "a
  real, separate feature" — that feature is now tracked as #42
  (filed 2026-08-15, depends on #31's pattern). #11 does not event-source
  `InternalControl` edits; it only *snapshots* the control's current
  `owner_person_id` (and, per 12.3, `cadence_interval_months`) into each
  occurrence's own event payload at generation time — exactly as §5
  already specified ("never a live join"), just now expressed as an event
  payload field instead of a copied column value.
- §3.4's `ControlOccurrenceEvidence` keeps its shape (a join table, not a
  new `EvidenceControlMapping` column) for the reasons already given —
  see 12.4 for how *linking* becomes an event.
- §9, §10, §11's structure and content — the compatibility analysis,
  open risks, and slice plan are unaffected; §11's slice 1 ("schema +
  migration") now includes the `domain_events`-registration step in
  12.7, and slice 6 (worklog) is unaffected.

### 12.2 Event types (aggregate_type = `"control_occurrence"`)

One aggregate per occurrence; `aggregate_id` = the occurrence's own id
(minted with `new_id()`, same 32-char hex scheme as everything else).
Events, in the order a real occurrence accumulates them:

| event_type | appended by | payload (JSON) |
|---|---|---|
| `ControlOccurrenceMaterialized` | §5 generation (calendar cadence) | `control_id`, `control_period_id` (nullable), `due_at` (ISO), `responsible_person_id` (nullable, snapshotted), `cadence_interval_months` (snapshotted, see 12.3) |
| `ControlOccurrenceRecordedManually` | §6 manual/event-driven creation | `control_id`, `control_period_id` (nullable), `due_at` (ISO, nullable — §3.3's original flexibility: non-null for a backfilled/known date, null for a pure event-driven entry with no schedule), `scope_note` |
| `ControlOccurrencePerformed` | §6 `/occurrences/{id}/perform` — every submission, including corrections | `performed_by_person_id`, `scope_note`, `evidence_note` (business timestamp is NOT a payload field — see 12.3: the claimed performance time is the event's own `occurred_at` column, set explicitly by the route, not duplicated into the JSON payload) |
| `ControlOccurrenceEvidenceLinked` | §6 `/perform` (optional) or a later separate link action | `evidence_snapshot_id` |

Exactly one of `Materialized`/`RecordedManually` occurs, always as
`aggregate_sequence == 1` (this is what `origin` reduces from — the
projector sets `origin="generated"` for `Materialized`, `"manual"` for
`RecordedManually`, and no later event ever changes it). `Performed` may
occur more than once (§7's "allow re-submission" policy is unchanged —
see 12.3). `EvidenceLinked` may occur zero or more times.

### 12.3 `occurred_at`/`recorded_at` and actor semantics

`recorded_at` (server time the event was appended, never caller-supplied)
directly replaces §7's `ControlOccurrence.updated_at`/`AuditEvent.created_at`
combination as the tamper-evident anchor for backdating detection — it is
*stronger*, since every correction gets its own immutable `recorded_at`,
not one mutable column overwritten each time.

`occurred_at` (business time) directly carries §3.3's `performed_at`: a
user claiming "I performed this last Tuesday" sets `occurred_at` to that
past date on the `ControlOccurrencePerformed` event, while `recorded_at`
is unconditionally today. **This is a correction to #22's own design
doc** (`docs/superpowers/specs/2026-08-15-issue22-event-store-design.md`
§5), which said occurred_at-backdating is only for "migration/import
facts" with `actor_type="migration"` — too narrow. A real user backdating
their own genuine claim is `actor_type="user"`, `occurred_at != recorded_at`,
and that's a normal, legitimate, non-migration case `app/events.py`
already supports correctly in code (nothing in `append_event` enforces
the migration-only framing — it was documentation guidance, not a runtime
check). #22's design doc should get a one-line correction to that effect
when convenient; not blocking for #11.

**Unambiguous wiring, since the payload table above deliberately does
NOT duplicate the claimed performance time as a JSON field** (adversarial
review flagged this as a real implementation trap — an implementer could
otherwise let `occurred_at` silently default to `recorded_at` and quietly
defeat the whole backdating story while still populating a `performed_at`
payload key that looks correct): the `/perform` route parses the
form's claimed performance date/time and passes it explicitly as
`occurred_at` to `append_and_project`, e.g.
`append_and_project(db, CONTROL_OCCURRENCE_PROJECTORS, aggregate_type="control_occurrence",
aggregate_id=occurrence_id, event_type="ControlOccurrencePerformed",
payload={"performed_by_person_id": ..., "scope_note": ..., "evidence_note": ...},
occurred_at=parsed_performed_at, actor_type="user", actor_id=request.state.user.id)`
— the projector then sets the projection's `performed_at` column from the
**event's `occurred_at`**, not from a payload field (there isn't one).

**Backdating plausibility bound.** §3.3/§7 never bounded how far
`performed_at` could diverge from submission time, and adversarial review
correctly flagged that wrapping an unbounded claim in an *immutable* event
raises the stakes versus the original mutable-row design (a permanently
recorded implausible claim, not a correctable one). Minimal, clearly
justified guard added here: reject `occurred_at` values in the future
(`occurred_at > utcnow()`) at the route level, before appending — "you
can't have performed something that hasn't happened yet" is universally
true and requires no product judgment call. Deliberately **not** adding a
lower bound (e.g. relative to the period's `starts_on` or the control's
own creation) — that's a genuine, debatable product policy question this
design doesn't have standing to invent unasked; flagged as an open
question in §10 rather than a rule guessed at here.

`actor_type="user"`, `actor_id=request.state.user.id` (the `User.id` UUID,
not the email `record_audit_event` uses) is set on every event appended
by an authenticated route. This **replaces** §7's `record_audit_event`
call for the occurrence's own history — the event's own `actor_type`/
`actor_id` is immutable (enforced by `app/events.py`'s update/delete
guards) and serves §7's "who actually submitted this claim" purpose with
a *stronger* guarantee than a parallel `AuditEvent` row would (an
`AuditEvent` has no hard mutation guard, only "no route exists to edit
it"). **Occurrence mutations do not additionally write `AuditEvent` rows**
— doing so would be exactly the parallel-mutable-history anti-pattern
`.agent/RULES.md` §1 item 3 warns against, and would raise an unanswerable
"which one is authoritative" question.

**Known, accepted consequence — stated precisely, not softened:**
control-occurrence activity will be **completely absent**, not merely
degraded, from two existing surfaces that read `AuditEvent` directly:
the `/admin/audit-log` page (`app/routers/audit_log.py`) *and* the admin
Dashboard's "recent activity" widget (`app/routers/dashboard.py:57-59`,
`select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(10)`) —
adversarial review correctly flagged the Dashboard widget as a second,
previously-unnamed instance of the same gap. As control operations
become a primary form of day-to-day activity, the Dashboard's "what just
happened" feed will look increasingly quiet for exactly the activity
that matters most. This is a real, accepted trade-off for this issue,
not a silently-discovered one: #11 does not patch either existing page
to also read `DomainEvent`, and does not ship any replacement view of an
occurrence's own event history — issue #45 ("auditor-facing historical
timeline", filed 2026-08-15) is the tracked follow-up for building a
general event-history view across event-backed domains. Shipping #11
without any interim mitigation for this gap is a product decision
worth a human's explicit sign-off given the magnitude (full loss of
this specific activity type from two visible admin surfaces, not a
partial degradation) — flagged in §10 as an item to confirm before
merge, not resolved unilaterally here.

### 12.4 Projection: `ControlOccurrence` (revises §3.3's table)

```python
CONTROL_OCCURRENCE_ORIGINS = ("generated", "manual")


class ControlOccurrence(Base):
    """Canonical projection over the "control_occurrence" DomainEvent
    aggregate (app/events.py) — NOT the source of truth. Rebuildable via
    rebuild_projection(session, CONTROL_OCCURRENCE_PROJECTORS,
    aggregate_type="control_occurrence", reset=...). id == aggregate_id.
    """

    __tablename__ = "control_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "control_id", "control_period_id", "due_at", name="uq_occurrence_control_period_due"
        ),
        CheckConstraint(f"origin IN {CONTROL_OCCURRENCE_ORIGINS}", name="ck_occurrence_origin"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id, no default=new_id
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    control_period_id: Mapped[str | None] = mapped_column(ForeignKey("control_periods.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)

    due_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    performed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    responsible_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    performed_by_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    scope_note: Mapped[str] = mapped_column(Text, default="")
    evidence_note: Mapped[str] = mapped_column(Text, default="")

    # Denormalized read-convenience timestamps, populated by the projector
    # from the first/latest event's recorded_at — not independently
    # authoritative; a full history is always available via DomainEvent.
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
```

`ControlOccurrenceEvidence` (§3.4) keeps its exact original shape
(`occurrence_id`, `evidence_snapshot_id`, `UNIQUE` pair) but is now a
*projection* populated by the `ControlOccurrenceEvidenceLinked` projector
rather than written directly by the route.

**Correction (adversarial review, 2026-08-17): generation idempotency
uses `app/events.py`'s `idempotency_key`, not an `IntegrityError`-catch
on the projection constraint.** The original version of this section
reasoned that `idempotency_key` "can't be used since a new occurrence has
no aggregate_id to key against in advance," and proposed catching
`IntegrityError` on this projection-layer `UniqueConstraint` instead,
"exactly like `add_mapping`'s pattern." That premise was wrong, and the
fallback it justified doesn't actually work as described:

- `DomainEvent.idempotency_key` is a **global** unique constraint
  (`app/events.py:74`), independent of `aggregate_id` —
  `_find_by_idempotency_key` looks up by the key string alone and hands
  back whichever aggregate already claimed it. A deterministic key
  derived from the business tuple, minted *before* the aggregate_id —
  `f"control_occurrence:materialize:{control_id}:{control_period_id or 'none'}:{due_at.isoformat()}"`
  — is exactly this repo's own existing convention (`Job.idempotency_key`,
  caller-namespaced the same way, cited already in #22's own design doc
  §9) and Just Works here: on a race, the second `append_and_project`
  call finds the first call's event via the key, returns it with
  `created=False`, and — critically — **does not re-run the projector**
  (`app/events.py:319`, by design). The projection insert for the
  duplicate is never attempted at all; there is no race to catch.
- The rejected `IntegrityError`-catch fallback would not have worked as
  described anyway: this app's session factory sets `autoflush=False`
  (`app/db.py:102`), and unlike `add_mapping`'s pattern (which calls
  `db.flush()` immediately after `db.add()`), nothing in the sketched
  generation loop forced an early flush — the projection's constraint
  violation would surface only at the final `session.commit()` in
  `get_db`'s teardown (`app/deps.py`), by which point the route handler
  has already returned with no `try/except` positioned to catch it, and
  — worse — the bulk `/control-periods/{id}/generate-occurrences` route
  (§6) generates across *every* calendar-cadence control in one
  transaction, so one collision would have discarded every other
  control's occurrences generated earlier in the same request, not just
  the colliding one.

**Fixed design:** every `ControlOccurrenceMaterialized` append passes
`idempotency_key=f"control_occurrence:materialize:{control.id}:{control_period_id or 'none'}:{due_date.isoformat()}"`.
The generation loop no longer needs a separate "does this due date
already exist" pre-check at all — `append_and_project`'s own idempotent
short-circuit *is* the check, and it's race-safe across concurrent
requests by construction (re-verified inside the retry loop, not just
once — `app/events.py:174-181`), not merely between sequential due dates
in one loop. The `UNIQUE(control_id, control_period_id, due_at)`
constraint on the projection table stays as **defense-in-depth only** —
under this design it should never actually fire via the generation path;
if it ever does, that indicates a real bug (e.g. a key-computation error),
and surfacing as an uncaught error is the correct behavior for a
should-never-happen case, not something requiring bespoke handling.

Manual creation (`ControlOccurrenceRecordedManually`, §6) intentionally
gets **no** `idempotency_key` — unlike generation, there's no natural
deterministic key for a user-directed single creation (a `due_at=NULL`
event-driven entry has nothing to dedupe against, and two separate manual
entries on the same day are often legitimately different facts). When a
manual entry's caller-supplied `due_at` happens to collide with an
existing occurrence's `(control_id, control_period_id, due_at)`, the
projection `UniqueConstraint` correctly rejects it — and because this
route handles exactly one occurrence per request (not a batch loop like
bulk generation), `add_mapping`'s catch-and-flash pattern genuinely does
apply here: call `db.flush()` immediately after the projector's insert,
inside a `try/except IntegrityError`, matching `app/routers/controls.py:85-102`
precisely. **Known, accepted, cross-backend limitation** (adversarial
review): under genuinely concurrent writers, SQLite may surface this as
an `OperationalError` ("database is locked") rather than `IntegrityError`,
depending on timing/journal mode, which this design does not specially
handle — the same rarity/scale judgment §5 already made about this app's
actual concurrency profile (single-process, infrequent admin-triggered
operations) applies equally here; not worth advisory-lock machinery for
an edge case this unlikely.

### 12.5 Generation and route changes (revises §5, §6)

**Signature correction (adversarial review):** §5 declared
`generate_occurrences(..., actor: str)`, matching `record_audit_event`'s
convention where `actor: str` means an email
(`request.state.user.email`). `app/events.py`'s `actor_id` column is a
32-char UUID, not an email — a plain `actor: str` forwarded unchanged
would silently mean two different things depending on call site. Fixed
signature: `generate_occurrences(session, control, *, period=None,
horizon_months=12, actor_type="user", actor_id=None)`. Call-site mapping,
stated explicitly since §5's CLI path (`python -m app.cli
generate-control-occurrences`) has no `request.state.user` to draw an id
from: the admin/self-serve HTTP routes pass `actor_type="user",
actor_id=request.state.user.id`; the CLI command passes `actor_type="system",
actor_id=None` (both are valid, already-supported `app/events.py`
defaults — no special-casing needed).

`generate_occurrences` keeps both period-scoped/period-less code paths
from §5 unchanged, but its body changes from
`session.add(ControlOccurrence(...))` to:

```python
for due_date in computed_due_dates:
    idempotency_key = (
        f"control_occurrence:materialize:{control.id}:{period_id or 'none'}:{due_date.isoformat()}"
    )
    append_and_project(
        session,
        CONTROL_OCCURRENCE_PROJECTORS,
        aggregate_type="control_occurrence",
        aggregate_id=new_id(),
        event_type="ControlOccurrenceMaterialized",
        payload={
            "control_id": control.id,
            "control_period_id": period_id,
            "due_at": due_date.isoformat(),
            "responsible_person_id": control.owner_person_id,
            "cadence_interval_months": control.cadence_interval_months,
        },
        idempotency_key=idempotency_key,
        actor_type=actor_type,
        actor_id=actor_id,
    )
```

No separate `_occurrence_already_exists` pre-check is needed — per
12.4's correction, `idempotency_key` makes the append itself the
existence check, race-safe across concurrent requests, not just within
one loop.

Every other route in §6's table (`/controls/{id}/occurrences` manual
creation, `/occurrences/{id}/perform`) follows the same shape: validate
and 404-guard exactly as §7 already specifies, then call
`append_and_project` instead of `session.add`/`record_audit_event`. Two
route-specific details worth stating explicitly (both flagged by
adversarial review as real implementation traps if left implicit):

- **`/occurrences/{id}/perform` must pre-fill `scope_note` from the
  current projection value**, not submit a blank default — since
  `ControlOccurrenceRecordedManually` and `ControlOccurrencePerformed`
  both project onto the same `scope_note` column, a naive "always
  overwrite from this submission" projector would silently blank out a
  manually-created occurrence's original scope note on first
  performance. This is the same "pre-fill an edit form with current
  state" behavior `update_assessment` already follows (§1's cited
  precedent) — not new machinery, just don't skip it here.
- **`/occurrences/{id}/perform` sets `occurred_at` from the parsed
  `performed_at` form field** (12.3) and rejects future values before
  appending — see 12.3 for the exact call shape and the future-date
  guard's justification.

The admin/login authorization split in §6 is unchanged — it governs who
may *call* the route, orthogonal to how the mutation is persisted.

### 12.6 Test strategy additions (extends §8)

All of §8's original test list still applies (generation determinism,
event-driven/manual pattern, missing/overdue detection, historical
stability, authorization split, invalid cross-scope references, Postgres
extension) — reframed against the projection table instead of a plain
mutable one, with these additions specific to the event-backed mechanism:

- **Projection rebuild determinism**: for a representative set of
  occurrences with multiple `Performed`/`EvidenceLinked` events each,
  wipe the projection tables and call `rebuild_projection`; assert the
  rebuilt state exactly matches the pre-wipe state (mirrors
  `tests/test_events.py`'s existing representative-flow test, applied to
  the real domain).
- **Re-performance preserves full history**: submitting `/perform` twice
  for the same occurrence appends two `ControlOccurrencePerformed`
  events (not one updated row); the projection reflects the latest
  submission; both events remain independently queryable via
  `DomainEvent`.
- **No `AuditEvent` row for occurrence mutations**: an explicit test
  asserting occurrence mutations do NOT also write to `audit_events`,
  documenting 12.3's decision as tested behavior, not an implicit gap.
- **Generation race is a clean, caught error, not a duplicate**:
  simulate two near-simultaneous generation calls for the same
  `(control_id, control_period_id, due_at)`; assert the second is caught
  and flashed, not a silently duplicated occurrence or an unhandled 500.

### 12.7 Migration (extends §4)

No bootstrap/migration events are needed for `#41`'s convention — unlike
#31's policy-history migration, `control_occurrences`/`control_periods`/
`control_occurrence_evidence` are brand-new tables with zero pre-existing
rows; #11 is the first domain built event-native from day one, not a
retrofit. `migrations/env.py` already imports `app.events` (added by
#22), so the new `ControlOccurrence`/`ControlOccurrenceEvidence`
projection tables are picked up by the same autogenerate path #22
established — no additional env.py change needed. `ControlPeriod` and
`InternalControl`'s 3 new columns migrate exactly as §4 already
describes (plain mutable table/columns, no event-store involvement).

## 13. Post-implementation adversarial review (2026-08-17)

An independent review agent (fresh context, full read of §12) was
dispatched after slices 1–5 were implemented and all targeted/full test
suites were green. It found six real, confirmed gaps — none touching the
event-sourcing core (idempotency, savepoint handling around the
`/perform` + evidence-link path, and the partial-index NULL semantics
were specifically distrusted and independently re-verified as correct)
— all fixed before this PR:

1. **(High, the most significant finding) No shipped path to configure a
   control's cadence.** §12.1 claimed `cadence_type`/
   `cadence_interval_months`/`owner_person_id` are "edited through the
   existing register-grid mechanism exactly like status/owner/
   review_frequency are today," but the implementation never actually
   added them to `CONTROLS_REGISTER_CONFIG` — the occurrence-generation
   machinery was correct but unreachable by a real user. Fixed:
   `cadence_type`/`cadence_interval_months` added as ordinary
   `FieldSpec`s (enum/number) to the register grid + Tabulator columns;
   `owner_person_id`, being a person-FK with no register-grid `FieldType`
   equivalent, got its own small dedicated form/route
   (`POST /controls/{id}/owner`), matching
   `app/routers/vendor_systems.py::link_roster_row`'s existing pattern
   for the same class of field.
2. **(High) Unbounded loop for a non-positive `cadence_interval_months`.**
   `not control.cadence_interval_months` only filters `None`/`0` — a
   negative value reaches `_compute_due_dates`, which walks backward
   forever. Fixed at two layers: a new
   `ck_control_cadence_interval_positive` CHECK constraint (`IS NULL OR >
   0`) as the authoritative guard, plus an explicit `<= 0` check in
   `generate_occurrences` as defense in depth for any future in-memory
   caller that bypasses the DB layer.
3. **(Medium) No PostgreSQL-specific coverage for the new tables**, despite
   §12.6/§8 calling for it — the partial unique index is this migration's
   single most dialect-sensitive piece. Fixed: extended the existing
   gated `test_migrations_apply_cleanly_against_postgres` with an
   ORM-level round trip proving the partial index's NULL-vs-NULL
   semantics and the new CHECK constraint against a real Postgres server
   (CI-only; no local Postgres in this sandbox, consistent with every
   other test in that function).
4. **(Medium) CLI crash on a non-active `--period-id`.**
   `generate_control_occurrences_command` let `generate_occurrences`'s
   `ValueError` propagate as an unhandled traceback instead of the clean
   `print(...); return 1` pattern its two sibling error paths already use.
   Fixed with a `try/except ValueError`.
5. **(Medium) `/perform`'s date field didn't pre-fill from an existing
   performance claim.** Every other field (`scope_note`, `evidence_note`,
   `performed_by_person_id`) correctly pre-fills from current projection
   state per §12.5's explicit requirement, but the date defaulted to
   today unconditionally — reopening the form only to fix a typo would
   silently shift `occurred_at` (an immutable event field) to today on
   resubmission. Fixed: defaults to `occurrence.performed_at.date()` when
   already performed.
6. **(Low) Missing negative-path test** for an unknown
   `evidence_snapshot_id` on `/perform` (the code already handled it
   correctly; only the parallel `performed_by_person_id` case had
   coverage).

**One implementation-discovered gap the review agent did not flag**,
found while fixing #2 above: `ControlOccurrence`'s
`UniqueConstraint("control_id", "control_period_id", "due_at")` does not
by itself prevent two period-less (`control_period_id IS NULL`)
occurrences from sharing a `due_at` — standard SQL treats `NULL != NULL`
for uniqueness on both SQLite and PostgreSQL, the same pitfall
`ControlOccurrenceEvidence`'s own docstring already named for a different
table. Closed with a companion partial unique index,
`uq_occurrence_control_due_no_period` (`UNIQUE(control_id, due_at) WHERE
control_period_id IS NULL`), added to both `app/models.py` and the
pending migration, with regression tests on both SQLite (direct) and
Postgres (gated).

**A genuine, reproducible Alembic/SQLite tooling issue surfaced while
building that migration**, unrelated to this domain's correctness:
`batch_alter_table`'s reflection-based `drop_constraint(name,
type_="check")` intermittently fails to find a second named CHECK
constraint on the same table when both are dropped via table-recreate in
this environment (reproduced directly against `ApplyBatchImpl`/
`get_check_constraints`; `Table(autoload_with=...)` reflection outside
that code path correctly finds both). Worked around by passing an
explicit `copy_from=<Table>` to `batch_alter_table` in the downgrade,
bypassing reflection entirely — the documented workaround for
`batch_alter_table`'s own noted CHECK-constraint limitations. Verified
with a full `upgrade head` → `downgrade base` → `upgrade head` cycle.
