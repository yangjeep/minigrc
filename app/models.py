"""SQLAlchemy models for the GRC/ISMS domain foundation.

Scope for this PR (see docs/domain/domain-model.md for the research behind
these choices): frameworks, framework requirements, internal controls, the
requirement<->control mapping, risks, and audit events. Policies, evidence,
connectors, actions, and the trust center are intentionally NOT modeled yet
— they remain placeholder pages until a real product need justifies a table
(see docs/product-scope.md).

IDs are 32-character hex-encoded UUID4 strings (NOT ULIDs — they are not
lexicographically sortable by creation time; see
docs/decisions/architectural-decisions.md #3), stored as TEXT primary keys.
No autoincrement integers, so ids are stable across export/import and safe
to reference from external systems later. Rows that need creation-order
sorting use `created_at`, not id order.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Framework(Base):
    """A compliance framework, e.g. an ISO 27001 catalogue.

    Modeled so more than one framework can exist side by side (ISO 27001,
    SOC 2, etc.) without schema changes.
    """

    __tablename__ = "frameworks"
    __table_args__ = (UniqueConstraint("catalog_key", name="uq_framework_catalog_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    is_placeholder_content: Mapped[bool] = mapped_column(default=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Issue #12: identifies "this row is the canonical instance of system
    # catalog X" (e.g. "iso27001-2022-sample", "soc2-2017-sample"),
    # independent of the user-editable name/version — see
    # app/framework_catalog.py. NULL for every user-created framework;
    # never exposed on any writable route/register field.
    catalog_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Minimal default/primary-framework flag — no enforced singleton, no
    # toggle route yet (see docs/superpowers/specs/2026-08-17-issue12-
    # soc2-primary-framework-design.md §11).
    is_primary: Mapped[bool] = mapped_column(default=False)
    # Issue #43: "these Framework rows are alternate versions of the same
    # conceptual catalog" (e.g. "soc2"), distinct from catalog_key, which
    # conflates family+version into one opaque string. NULL for every
    # user-created framework, same convention as catalog_key. Set by
    # app/framework_catalog.py for every SYSTEM_CATALOGS entry; never
    # mutated once set. See app/framework_adoption.py for what consumes
    # this.
    catalog_family: Mapped[str | None] = mapped_column(String(32), nullable=True)

    requirements: Mapped[list[FrameworkRequirement]] = relationship(
        back_populates="framework",
        cascade="all, delete-orphan",
        order_by="FrameworkRequirement.display_order, FrameworkRequirement.reference_code",
    )


class FrameworkRequirement(Base):
    """One requirement/clause within a framework.

    `reference_code` (e.g. "A.5.1") and `summary` are placeholders unless
    the organization has supplied its own licensed text — see the
    disclaimer in docs/domain/domain-model.md. This is deliberately NOT a
    generic "requirement engine": it is one table shaped for the one
    relationship (framework -> requirements) this PR needs.
    """

    __tablename__ = "framework_requirements"
    __table_args__ = (
        UniqueConstraint("framework_id", "reference_code", name="uq_requirement_framework_code"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    framework_id: Mapped[str] = mapped_column(ForeignKey("frameworks.id"), nullable=False)
    reference_code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    display_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    framework: Mapped[Framework] = relationship(back_populates="requirements")
    mappings: Mapped[list[ControlRequirementMapping]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan"
    )
    assessment: Mapped[RequirementAssessment | None] = relationship(
        back_populates="requirement", cascade="all, delete-orphan", uselist=False
    )
    notes: Mapped[list[RequirementNote]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan", order_by="RequirementNote.created_at"
    )


CADENCE_TYPES = ("calendar", "event_driven")


class InternalControl(Base):
    """An internal control the organization actually operates.

    Distinct from FrameworkRequirement: a requirement is "what the
    framework asks for," a control is "what we actually do." One control
    can satisfy several requirements (even across frameworks later), hence
    the many-to-many mapping table rather than a foreign key here.
    """

    __tablename__ = "internal_controls"
    __table_args__ = (
        CheckConstraint(f"cadence_type IN {CADENCE_TYPES}", name="ck_control_cadence_type"),
        # NULL passes (nothing configured yet); a non-positive interval
        # would otherwise send app/control_occurrences.py's due-date loop
        # backward forever (adversarial review finding, issue #11).
        CheckConstraint(
            "cadence_interval_months IS NULL OR cadence_interval_months > 0",
            name="ck_control_cadence_interval_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="not_started")
    review_frequency: Mapped[str] = mapped_column(String(32), default="annual")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Operational cadence tracking (issue #11) — all nullable so every
    # existing control keeps its current "design/status only" behavior
    # (cadence_type=NULL) unless an admin/owner explicitly opts it into
    # occurrence tracking. `review_frequency` above is deliberately left
    # untouched: it means "how often we re-review this control's written
    # definition," a distinct concept from "how often it operates."
    cadence_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cadence_interval_months: Mapped[int | None] = mapped_column(nullable=True)
    owner_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)

    mappings: Mapped[list[ControlRequirementMapping]] = relationship(
        back_populates="control", cascade="all, delete-orphan"
    )
    # No cascade here, deliberately (unlike `mappings` above): occurrences
    # are compliance history, not disposable child rows. Under
    # PRAGMA foreign_keys=ON, deleting a control with occurrences is
    # rejected (RESTRICT-like NO ACTION) rather than silently destroying
    # that history — see docs/superpowers/specs/2026-08-15-issue11-
    # control-operations-lifecycle-design.md §3.3.
    occurrences: Mapped[list[ControlOccurrence]] = relationship(viewonly=True)
    owner_person: Mapped[Person | None] = relationship(viewonly=True)
    # Issue #42: the control's own governed *definition* history —
    # distinct from `occurrences` above (the control's recurring
    # *operational* history). `InternalControl`'s own plain columns
    # above are left untouched by this relationship/its lifecycle
    # module — same precedent as `Policy.title`/`description`, which
    # #31's PolicyVersion lifecycle never syncs back onto Policy either.
    versions: Mapped[list[InternalControlVersion]] = relationship(
        back_populates="control",
        cascade="all, delete-orphan",
        order_by="InternalControlVersion.version_number.desc()",
    )

    @property
    def effective_version(self) -> InternalControlVersion | None:
        """The control-definition version this app currently treats as
        operative (issue #42) — the version-level analog of
        Policy.effective_version. At most one version is ever
        "effective" at a time, enforced by
        uq_internal_control_version_one_effective_per_control."""
        return next((v for v in self.versions if v.lifecycle_status == "effective"), None)


CONTROL_STATUSES = ("not_started", "in_progress", "implemented", "needs_review")
REVIEW_FREQUENCIES = ("monthly", "quarterly", "semiannual", "annual")


class ControlRequirementMapping(Base):
    """Join table: which internal control satisfies which requirement.

    Unique on (control_id, requirement_id) so a duplicate mapping attempt is
    impossible at the database level and resubmitting the same mapping form
    is a safe, idempotent no-op rather than a duplicate row.
    """

    __tablename__ = "control_requirement_mappings"
    __table_args__ = (UniqueConstraint("control_id", "requirement_id", name="uq_control_requirement"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    requirement_id: Mapped[str] = mapped_column(ForeignKey("framework_requirements.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    control: Mapped[InternalControl] = relationship(back_populates="mappings")
    requirement: Mapped[FrameworkRequirement] = relationship(back_populates="mappings")


# issue #42: the control-definition-level analog of
# POLICY_VERSION_LIFECYCLE_STATUSES (#31) — kept as a separate constant
# even though the values coincide, matching this codebase's existing
# convention of not sharing a literal tuple object across otherwise-
# independent domains, so the two lifecycles can evolve independently.
CONTROL_DEFINITION_LIFECYCLE_STATUSES = (
    "draft",
    "in_review",
    "approved",
    "effective",
    "superseded",
    "withdrawn",
)


class InternalControlVersion(Base):
    """One immutable, point-in-time snapshot of an InternalControl's own
    *definition* (issue #42) — distinct from ControlOccurrence (#11),
    which is the control's recurring *operational* history.

    Fully event-sourced from creation over the
    "internal_control_version" DomainEvent aggregate (see
    app/control_definition_lifecycle.py) — unlike PolicyVersion (#31),
    there is no pre-existing plain-insert use to preserve for this
    brand-new table (same reasoning EvidenceArtifactVersion's docstring
    already gives for its own table, #32). Never written to directly
    outside its projector functions.

    `InternalControl`'s own plain columns (name/description/status/
    review_frequency) are never synced from this table — they remain
    the control's own simple identity/display fields, the same
    precedent Policy.title/description already set relative to
    PolicyVersion.
    """

    __tablename__ = "internal_control_versions"
    __table_args__ = (
        UniqueConstraint("control_id", "version_number", name="uq_internal_control_version_number"),
        CheckConstraint(
            f"lifecycle_status IN {CONTROL_DEFINITION_LIFECYCLE_STATUSES}",
            name="ck_internal_control_version_lifecycle_status",
        ),
        # At most one effective version per control — same partial-
        # unique-index guard as uq_policy_version_one_effective_per_policy.
        Index(
            "uq_internal_control_version_one_effective_per_control",
            "control_id",
            unique=True,
            sqlite_where=text("lifecycle_status = 'effective'"),
            postgresql_where=text("lifecycle_status = 'effective'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(nullable=False)

    # The snapshot itself.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    review_frequency_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    mapped_requirement_ids_json: Mapped[str] = mapped_column(Text, default="[]")

    # Event-sourced lifecycle — same shape/state machine as PolicyVersion.
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    drafted_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    drafted_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    effective_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("internal_control_versions.id"), nullable=True
    )
    withdrawn_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    withdrawn_reason: Mapped[str] = mapped_column(Text, default="")

    control: Mapped[InternalControl] = relationship(back_populates="versions")
    superseded_by_version: Mapped[InternalControlVersion | None] = relationship(
        remote_side="InternalControlVersion.id", foreign_keys=[superseded_by_version_id]
    )


CONTROL_PERIOD_STATUSES = ("planned", "active", "closed")


class ControlPeriod(Base):
    """An optional, framework-neutral time window controls operate within
    (e.g. "SOC 2 Type II — H1 2026").

    Deliberately not named "AssessmentPeriod" (collides with
    RequirementAssessment) or "AuditPeriod" (imports SOC2-specific
    vocabulary an ISO-only deployment has no equivalent for). No FK to
    Framework — periods are framework-neutral, matching the epic's
    principle that frameworks map onto controls, not the other way round.

    Optional infrastructure, not a mandatory gate: `generate_occurrences`
    (app/control_occurrences.py) has a period-less rolling-generation path
    that never requires one of these to exist. A plain mutable row plus
    `record_audit_event` (not an event-backed aggregate, unlike
    ControlOccurrence below) — this is scheduling metadata that gates
    generation, not itself the material compliance fact; see
    docs/superpowers/specs/2026-08-15-issue11-control-operations-
    lifecycle-design.md §12.1 for the considered alternative.

    `status` has enforced meaning: "planned" periods accept no occurrence
    generation/association; "active" periods accept both; "closed"
    periods reject further generation/association and (enforced at the
    route layer, not here) become immutable on starts_on/ends_on, with
    `closed_at` recording the transition separately from `updated_at` so
    a later rename doesn't erase when it actually closed.
    """

    __tablename__ = "control_periods"
    __table_args__ = (
        CheckConstraint("ends_on > starts_on", name="ck_control_period_dates"),
        CheckConstraint(f"status IN {CONTROL_PERIOD_STATUSES}", name="ck_control_period_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_on: Mapped[datetime.date] = mapped_column(nullable=False)
    ends_on: Mapped[datetime.date] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


CONTROL_OCCURRENCE_ORIGINS = ("generated", "manual")


class ControlOccurrence(Base):
    """Canonical projection over the "control_occurrence" DomainEvent
    aggregate (app/events.py) — NOT the source of truth for history.

    One row per occurrence, `id` is the aggregate_id events are appended
    against (see app/control_occurrences.py). Rebuildable from scratch via
    `rebuild_control_occurrence_projection` — never write to this table
    directly outside a projector function; always go through
    `append_and_project`/`app/control_occurrences.py`.

    "Missing/overdue" is a computed predicate
    (`due_at < now() AND performed_at IS NULL`), never a stored column —
    matches this app's existing `app/vendor_flags.py::compute_flags`
    precedent of computing operational warnings live rather than storing
    something that can drift.
    """

    __tablename__ = "control_occurrences"
    __table_args__ = (
        # Covers every occurrence WITH a control_period_id. Standard SQL
        # treats NULL != NULL for uniqueness purposes (both SQLite and
        # Postgres — same pitfall ControlOccurrenceEvidence's docstring
        # calls out), so this constraint alone does NOT stop two
        # period-less occurrences from sharing a due_at; the partial index
        # below closes that gap for the nullable-control_period_id case.
        UniqueConstraint(
            "control_id", "control_period_id", "due_at", name="uq_occurrence_control_period_due"
        ),
        Index(
            "uq_occurrence_control_due_no_period",
            "control_id",
            "due_at",
            unique=True,
            sqlite_where=text("control_period_id IS NULL"),
            postgresql_where=text("control_period_id IS NULL"),
        ),
        CheckConstraint(f"origin IN {CONTROL_OCCURRENCE_ORIGINS}", name="ck_occurrence_origin"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    control_period_id: Mapped[str | None] = mapped_column(ForeignKey("control_periods.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)

    due_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    performed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    responsible_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    performed_by_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    scope_note: Mapped[str] = mapped_column(Text, default="")
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    # issue #42: the control-definition version effective at the moment
    # this occurrence was generated/recorded — NULL for occurrences
    # created before this feature existed (never retroactively
    # populated; see app/control_occurrences.py). Once set, this never
    # changes: a later edit to the control's definition creates a new
    # InternalControlVersion, it never mutates this reference.
    control_definition_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("internal_control_versions.id"), nullable=True
    )

    # Denormalized read-convenience timestamps, populated by the projector
    # from the relevant event's recorded_at — not independently
    # authoritative; full history is always in DomainEvent.
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)

    control: Mapped[InternalControl] = relationship(viewonly=True)
    control_period: Mapped[ControlPeriod | None] = relationship(viewonly=True)
    responsible_person: Mapped[Person | None] = relationship(
        foreign_keys=[responsible_person_id], viewonly=True
    )
    performed_by: Mapped[Person | None] = relationship(foreign_keys=[performed_by_person_id], viewonly=True)
    control_definition_version: Mapped[InternalControlVersion | None] = relationship(viewonly=True)


class ControlOccurrenceEvidence(Base):
    """Projection: which EvidenceSnapshot rows are linked to which
    occurrence, populated by the "ControlOccurrenceEvidenceLinked"
    projector — not written directly by routes.

    A new, minimal join table rather than adding occurrence_id to the
    existing EvidenceControlMapping — that table's
    UNIQUE(evidence_snapshot_id, control_id) would need widening to
    include occurrence_id, and because NULL != NULL for uniqueness in
    both dialects, that would silently weaken today's "one mapping per
    (evidence, control)" guarantee for existing all-NULL-occurrence rows
    without a dialect-specific partial index this repo has deliberately
    avoided elsewhere. Scoped to the AWS-connector-sourced EvidenceSnapshot
    only — the only evidence storage that exists today; #13 will likely
    need its own link to a future general evidence-upload mechanism.
    """

    __tablename__ = "control_occurrence_evidence"
    __table_args__ = (
        UniqueConstraint("occurrence_id", "evidence_snapshot_id", name="uq_occurrence_evidence"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("control_occurrences.id"), nullable=False)
    evidence_snapshot_id: Mapped[str] = mapped_column(ForeignKey("evidence_snapshots.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class ControlOccurrenceEvidenceArtifact(Base):
    """Projection: which EvidenceArtifactVersion rows are linked to which
    occurrence, populated by the "ControlOccurrenceEvidenceArtifactLinked"
    projector — not written directly by routes.

    The general evidence-upload counterpart
    ControlOccurrenceEvidence's own docstring anticipated (issue #32) —
    a separate table for the same reason: EvidenceControlMapping-style
    widening would silently weaken an existing uniqueness guarantee for
    all-NULL-occurrence rows (see that docstring). Mirrors
    ControlOccurrenceEvidence exactly, just against
    EvidenceArtifactVersion instead of EvidenceSnapshot.
    """

    __tablename__ = "control_occurrence_evidence_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_id", "evidence_artifact_version_id", name="uq_occurrence_evidence_artifact"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("control_occurrences.id"), nullable=False)
    evidence_artifact_version_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_artifact_versions.id"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class ControlTestPopulation(Base):
    """A frozen, reproducible set of ControlOccurrence rows to sample from
    (issue #13).

    Plain row, not event-sourced — matches ControlPeriod's precedent
    ("scheduling/scoping metadata that gates testing, not itself the
    material compliance fact") rather than ControlOccurrence's continuously
    projected shape: nothing in this domain ever updates a population after
    creation (no edit route exists), so there is no multi-step lifecycle to
    replay. `criteria_note` is a free-text human explanation of how this
    population was scoped — never a query DSL or an implied universal rule.
    """

    __tablename__ = "control_test_populations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    control_period_id: Mapped[str | None] = mapped_column(ForeignKey("control_periods.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    criteria_note: Mapped[str] = mapped_column(Text, default="")
    frozen_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    items: Mapped[list[ControlTestPopulationItem]] = relationship(
        back_populates="population", cascade="all, delete-orphan"
    )


class ControlTestPopulationItem(Base):
    """One ControlOccurrence frozen into a population at `freeze_population`
    time. Never updated or deleted afterward — copying the occurrence id in
    at freeze time (rather than a live query) is what keeps a later new
    occurrence, or a correction to an already-frozen occurrence, from
    silently changing the historical sample basis."""

    __tablename__ = "control_test_population_items"
    __table_args__ = (
        UniqueConstraint("population_id", "control_occurrence_id", name="uq_population_item_occurrence"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    population_id: Mapped[str] = mapped_column(ForeignKey("control_test_populations.id"), nullable=False)
    control_occurrence_id: Mapped[str] = mapped_column(ForeignKey("control_occurrences.id"), nullable=False)

    population: Mapped[ControlTestPopulation] = relationship(back_populates="items")


class ControlTestSample(Base):
    """A selection drawn from a ControlTestPopulation (issue #13).

    Plain row, same reasoning as ControlTestPopulation — created once, never
    edited. `selection_method`/`selection_rationale` are free text (e.g.
    "haphazard, 3 of 12 monthly occurrences") — no sample-size algorithm is
    encoded anywhere in this domain, per the issue's explicit instruction
    not to imply false audit certainty.
    """

    __tablename__ = "control_test_samples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    population_id: Mapped[str] = mapped_column(ForeignKey("control_test_populations.id"), nullable=False)
    selection_method: Mapped[str] = mapped_column(String(255), default="")
    selection_rationale: Mapped[str] = mapped_column(Text, default="")
    selected_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    items: Mapped[list[ControlTestSampleItem]] = relationship(
        back_populates="sample", cascade="all, delete-orphan"
    )


class ControlTestSampleItem(Base):
    """One population item selected into a sample. Never updated/deleted."""

    __tablename__ = "control_test_sample_items"
    __table_args__ = (
        UniqueConstraint("sample_id", "population_item_id", name="uq_sample_item_population_item"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sample_id: Mapped[str] = mapped_column(ForeignKey("control_test_samples.id"), nullable=False)
    population_item_id: Mapped[str] = mapped_column(
        ForeignKey("control_test_population_items.id"), nullable=False
    )

    sample: Mapped[ControlTestSample] = relationship(back_populates="items")


TEST_RESULTS = ("no_exceptions", "exceptions_noted")


class ControlTest(Base):
    """Canonical projection over the "control_test" DomainEvent aggregate
    (issue #13) — NOT the source of truth for history.

    Grows over time exactly like ControlOccurrence: "ControlTestRecorded"
    creates the row (+ its ControlTestException children, from the same
    payload); a later "ControlTestEvidenceLinked" event can attach evidence
    afterward. A test is never corrected in place — a retest is a NEW row
    with `retest_of_test_id` pointing back, per the issue's explicit
    "retest results are new facts, not edits of prior failures."
    `sample_id` is nullable: a direct-observation test (e.g. inspecting one
    current configuration) needs no formal sample.
    """

    __tablename__ = "control_tests"
    __table_args__ = (CheckConstraint(f"result IN {TEST_RESULTS}", name="ck_control_test_result"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    control_period_id: Mapped[str | None] = mapped_column(ForeignKey("control_periods.id"), nullable=True)
    sample_id: Mapped[str | None] = mapped_column(ForeignKey("control_test_samples.id"), nullable=True)
    procedure_description: Mapped[str] = mapped_column(Text, default="")
    tester_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=False)
    performed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    retest_of_test_id: Mapped[str | None] = mapped_column(ForeignKey("control_tests.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ControlTestException(Base):
    """One deviation/exception noted during a ControlTest. `sample_item_id`
    is nullable — a process-level exception isn't always tied to one
    specific sample item."""

    __tablename__ = "control_test_exceptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    test_id: Mapped[str] = mapped_column(ForeignKey("control_tests.id"), nullable=False)
    sample_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("control_test_sample_items.id"), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, default="")


class ControlTestEvidenceArtifact(Base):
    """Projection: which EvidenceArtifactVersion rows are linked to which
    ControlTest, populated by the "ControlTestEvidenceLinked" projector.
    Mirrors ControlOccurrenceEvidenceArtifact exactly."""

    __tablename__ = "control_test_evidence_artifacts"
    __table_args__ = (
        UniqueConstraint("test_id", "evidence_artifact_version_id", name="uq_test_evidence_artifact"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    test_id: Mapped[str] = mapped_column(ForeignKey("control_tests.id"), nullable=False)
    evidence_artifact_version_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_artifact_versions.id"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


FINDING_SEVERITIES = ("low", "medium", "high", "critical")
FINDING_STATUSES = ("open", "remediating", "retesting", "closed")
FINDING_CLOSURE_DECISIONS = ("remediated_and_retested", "risk_accepted", "false_positive", "other")


class Finding(Base):
    """Canonical projection over the "finding" DomainEvent aggregate (issue
    #13) — NOT the source of truth for history.

    Deliberately un-prefixed (not "ControlFinding"): a finding can arise
    from a ControlTest (`source_test_id` set) or be opened directly (e.g. a
    manual auditor observation), and stays reusable by non-control-test
    origins (ISO internal audit) per the issue's explicit reuse
    instruction. Full lifecycle (open/remediation-update/retest/close) is
    event-sourced, matching PolicyVersion's precedent for a rich state
    machine whose history must survive later transitions — closing a
    finding appends an event; it never rewrites the original
    "FindingOpened" payload.
    """

    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(f"severity IN {FINDING_SEVERITIES}", name="ck_finding_severity"),
        CheckConstraint(f"status IN {FINDING_STATUSES}", name="ck_finding_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    control_id: Mapped[str | None] = mapped_column(ForeignKey("internal_controls.id"), nullable=True)
    source_test_id: Mapped[str | None] = mapped_column(ForeignKey("control_tests.id"), nullable=True)
    owner_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    due_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    closure_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    closure_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class FindingRemediationUpdate(Base):
    """One append-only remediation-progress note on a Finding, populated by
    the "FindingRemediationUpdateRecorded" projector. Same shape as the
    existing RequirementNote thread pattern, scoped to Finding instead."""

    __tablename__ = "finding_remediation_updates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    linked_evidence_artifact_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_artifact_versions.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class FindingRetest(Base):
    """Links a Finding to the ControlTest that retested it, populated by
    the "FindingRetestRecorded" projector."""

    __tablename__ = "finding_retests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), nullable=False)
    test_id: Mapped[str] = mapped_column(ForeignKey("control_tests.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


APPLICABILITY_VALUES = ("yes", "no")
IMPLEMENTATION_STATES = ("not_started", "in_progress", "implemented")


class RequirementAssessment(Base):
    """The organization's assessment of one FrameworkRequirement.

    One-to-one with FrameworkRequirement (created alongside it). Separate
    table rather than columns on FrameworkRequirement so the requirement's
    catalogue content (reference code, title) and the org's assessment of it
    stay independently updatable/auditable.
    """

    __tablename__ = "requirement_assessments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(
        ForeignKey("framework_requirements.id"), nullable=False, unique=True
    )
    applicable: Mapped[str] = mapped_column(String(8), default="yes")
    implementation_state: Mapped[str] = mapped_column(String(32), default="not_started")
    owner: Mapped[str] = mapped_column(String(255), default="")
    last_reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_reviewed_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    requirement: Mapped[FrameworkRequirement] = relationship(back_populates="assessment")


class RequirementNote(Base):
    """Append-only note explaining a requirement's status or a decision.

    No update/delete path is exposed by the application — corrections are
    made by adding a new note, preserving the full history.
    """

    __tablename__ = "requirement_notes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(ForeignKey("framework_requirements.id"), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    requirement: Mapped[FrameworkRequirement] = relationship(back_populates="notes")


class Risk(Base):
    """A structured risk register entry.

    Likelihood/impact use a plain 1-5 scale rather than a configurable risk
    matrix — the simplest thing that supports sorting and a visible score.
    Risk treatment is a free-text field for now; a dedicated treatment/
    exception workflow is future scope (see docs/product-scope.md).
    """

    __tablename__ = "risks"
    __table_args__ = (
        CheckConstraint("likelihood BETWEEN 1 AND 5", name="ck_risk_likelihood_range"),
        CheckConstraint("impact BETWEEN 1 AND 5", name="ck_risk_impact_range"),
        CheckConstraint("length(trim(title)) > 0", name="ck_risk_title_not_blank"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="general")
    likelihood: Mapped[int] = mapped_column(default=1)
    impact: Mapped[int] = mapped_column(default=1)
    owner: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="open")
    treatment_plan: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def score(self) -> int:
        return self.likelihood * self.impact


RISK_STATUSES = ("open", "mitigating", "accepted", "closed")


TRUST_SERVICE_CATEGORIES = ("security", "availability", "confidentiality", "processing_integrity", "privacy")


class ComplianceScope(Base):
    """The org's current declared compliance-program scope (issue #30).

    Singleton per deployment (one org per deployment, RULES.md §1.11) — one
    row, fully event-sourced (`aggregate_type="compliance_scope"`): this row
    IS the current-state projection, updated in place by
    `ComplianceScopeDefined`/`ComplianceScopeRevised` events, the same shape
    `ControlOccurrence` uses (one long-lived aggregate whose projection is
    mutated by each event) rather than `EvidenceArtifactVersion`'s
    "every change is a new browsable row" shape — nothing here asks a user
    to browse distinct past scope versions (that is #45's future job); it
    only needs the change history to exist and be reconstructable from the
    event log, which an append-only log already gives for free. See
    docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-
    design.md §2.5 for the full reasoning.

    `id` deliberately has no `default=new_id` — matches the event-sourced-
    aggregate-id convention (`ControlOccurrence`): the domain layer
    generates it once, on first definition, not the ORM.

    `trust_service_categories` is a comma-separated subset of
    TRUST_SERVICE_CATEGORIES; "security" is always included (mandatory in
    every SOC 2 report) — enforced by `app/compliance_scope.py`, never
    trusted from caller input alone. Deliberately not a separate join
    table: SOC 2's category list is fixed and small, and this is
    framework-neutral storage for a fact that is currently only meaningful
    under SOC 2 (see design doc §2.2 for why it isn't used to filter
    starter-control generation yet).
    """

    __tablename__ = "compliance_scopes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    service_description: Mapped[str] = mapped_column(Text, default="")
    trust_service_categories: Mapped[str] = mapped_column(String(255), default="security")
    audit_period_starts_on: Mapped[datetime.date | None] = mapped_column(nullable=True)
    audit_period_ends_on: Mapped[datetime.date | None] = mapped_column(nullable=True)
    data_categories: Mapped[str] = mapped_column(Text, default="")
    locations: Mapped[str] = mapped_column(Text, default="")
    exclusions: Mapped[str] = mapped_column(Text, default="")
    exclusions_rationale: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


EMPLOYMENT_STATUSES = ("active", "suspended", "departed", "unknown")
PERSON_SOURCES = ("manual", "google_workspace", "csv")


class Person(Base):
    """A shared identity reference for anyone relevant to this org's ISMS.

    One row per human, referenced (optionally) by a MiniGRC `User`, a
    vendor system's admin/owner fields, and vendor roster snapshot rows.
    Not a full HRIS — just enough to answer "is this still an employee?"
    and "who owns this vendor relationship?" `employment_status` starts
    `"unknown"` (not `"active"`) because nothing has confirmed it yet; it
    only changes on explicit source data (manual edit or a Workspace
    Directory sync), never inferred or deleted on a missing sync record.
    """

    __tablename__ = "people"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    employment_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Issue #30: whether this person is currently declared in the compliance
    # program's scope. An ordinary mutable register field, not a separately
    # event-sourced fact — see docs/superpowers/specs/2026-08-19-issue30-
    # compliance-scope-onboarding-design.md §2.4 for why (every existing edit
    # route for this model already calls record_audit_event on update).
    in_scope: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # Issue #47: set once app.pii_redaction.redact_person_pii has run
    # for this person — makes redaction idempotent (a second call is a
    # safe no-op) without inferring it from the email's own shape.
    pii_redacted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


VENDOR_LIFECYCLE_STATUSES = ("trial", "active", "cancelling", "cancelled")
BILLING_FREQUENCIES = ("monthly", "annual", "other")
TRI_STATE_VALUES = ("yes", "no", "unknown")


class VendorSystem(Base):
    """One system the company actually purchases or uses (GitHub, Slack, AWS...).

    Deliberately one model, not separate Vendor/Application tables — see
    CLAUDE.md's "no generic abstraction without a second caller" and this
    branch's product boundary. Never stores a shared credential itself,
    only a reference URL to wherever it actually lives (e.g. 1Password).
    Cost is one authoritative amount + frequency (`annualized_cost_minor`
    is computed, not a separate manually-entered field), so a monthly and
    an annual total can never silently disagree.
    """

    __tablename__ = "vendor_systems"
    __table_args__ = (
        CheckConstraint("length(trim(system_name)) > 0", name="ck_vendor_system_name_not_blank"),
        CheckConstraint("length(trim(vendor_name)) > 0", name="ck_vendor_vendor_name_not_blank"),
        CheckConstraint("currency = upper(currency)", name="ck_vendor_currency_uppercase"),
        CheckConstraint("length(currency) = 3", name="ck_vendor_currency_length"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)

    # Identity and ownership
    system_name: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    access_url: Mapped[str] = mapped_column(String(2048), default="")
    admin_console_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    primary_department: Mapped[str] = mapped_column(String(255), default="")
    business_owner_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    # Issue #30: see Person.in_scope above — same rationale, same convention.
    in_scope: Mapped[bool] = mapped_column(default=False)

    # Access continuity
    uses_shared_login: Mapped[bool] = mapped_column(default=False)
    shared_credential_reference_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    primary_admin_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    secondary_admin_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    roster_last_confirmed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    # Cost — one authoritative amount + frequency; annualized cost is computed
    billing_frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="other")
    billing_amount_minor: Mapped[int | None] = mapped_column(nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    seat_count: Mapped[int | None] = mapped_column(nullable=True)
    cost_per_seat_minor: Mapped[int | None] = mapped_column(nullable=True)

    # Contract and renewal
    contract_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    contract_start_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    renewal_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    auto_renew: Mapped[str] = mapped_column(String(8), nullable=False, default="unknown")
    cancellation_notice_days: Mapped[int | None] = mapped_column(nullable=True)
    renewal_owner_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)

    # Support and escalation
    support_portal_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    support_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    support_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_manager_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_manager_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    emergency_escalation_instructions: Mapped[str] = mapped_column(Text, default="")
    customer_account_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    business_owner: Mapped[Person | None] = relationship(foreign_keys=[business_owner_person_id])
    primary_admin: Mapped[Person | None] = relationship(foreign_keys=[primary_admin_person_id])
    secondary_admin: Mapped[Person | None] = relationship(foreign_keys=[secondary_admin_person_id])
    renewal_owner: Mapped[Person | None] = relationship(foreign_keys=[renewal_owner_person_id])

    @property
    def annualized_cost_minor(self) -> int | None:
        """Computed from the one authoritative amount + frequency — never a separate field."""
        if self.billing_amount_minor is None:
            return None
        if self.billing_frequency == "monthly":
            return self.billing_amount_minor * 12
        if self.billing_frequency == "annual":
            return self.billing_amount_minor
        return None  # "other" frequency: not automatically annualizable

    @property
    def cancellation_deadline(self) -> datetime.date | None:
        if self.renewal_date is None or self.cancellation_notice_days is None:
            return None
        return self.renewal_date - datetime.timedelta(days=self.cancellation_notice_days)


class VendorUserSnapshot(Base):
    """One immutable, append-only capture of a vendor's reported user roster.

    Never edited or deleted by the application — a new import always
    creates a new snapshot; the most recent one (by `imported_at`)
    represents the vendor's current reported roster. See
    `app/vendor_roster_import.py` for the validate-everything-before-
    writing-anything import pipeline.
    """

    __tablename__ = "vendor_user_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    vendor_system_id: Mapped[str] = mapped_column(ForeignKey("vendor_systems.id"), nullable=False)
    imported_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    imported_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False)

    vendor_system: Mapped[VendorSystem] = relationship()
    rows: Mapped[list[VendorUserSnapshotRow]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class VendorUserSnapshotRow(Base):
    """One reported user row within a VendorUserSnapshot — immutable.

    `imported_*` columns preserve exactly what the vendor's export said,
    even after `matched_person_id` is set — linking an identity to a
    `Person` never rewrites the historical imported values.
    """

    __tablename__ = "vendor_user_snapshot_rows"
    __table_args__ = (UniqueConstraint("snapshot_id", "normalized_email", name="uq_snapshot_row_email"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("vendor_user_snapshots.id"), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(255), nullable=False)
    imported_email: Mapped[str] = mapped_column(String(255), nullable=False)
    imported_name: Mapped[str] = mapped_column(String(255), default="")
    imported_role: Mapped[str] = mapped_column(String(64), default="")
    imported_status: Mapped[str] = mapped_column(String(64), default="")
    imported_last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    matched_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)

    snapshot: Mapped[VendorUserSnapshot] = relationship(back_populates="rows")
    matched_person: Mapped[Person | None] = relationship()


USER_ROLES = ("admin", "operator", "reader", "auditor")
# admin: everything below plus every require_admin-gated route
#   (program/connector/auth/user/config administration).
# operator: every material compliance mutation not already admin-only —
#   the 1:1 rename of the old binary role's "user" value (issue #37).
# reader / auditor: read-only for material compliance state (see
#   app/deps.py::require_write_access). Identical server-side access in
#   this slice — no object/period-scoping mechanism exists yet (#30) to
#   express the audit-scope distinction the product eventually wants
#   between them; staged explicitly rather than faked.
USER_STATUSES = ("active", "disabled", "pending")

ROLE_SOURCES = ("local", "oidc_mapped")


class User(Base):
    """A local application user.

    Email is the login identifier, normalized to lowercase and stored
    unique. Passwords are hashed with pwdlib (Argon2) — see app/security.py.
    `role` is one of `USER_ROLES` (`admin`/`operator`/`reader`/`auditor`,
    issue #37) gating material-state mutation (see
    `app/deps.py::require_write_access`) and admin-only integration
    configuration, credential connections, manual syncs, and destructive
    vendor operations (see `app/deps.py::require_admin`). `person_id`
    optionally links this login identity to the shared `Person` directory.

    `status` gates login (see `app/deps.py::require_login`): `"active"` can
    sign in normally, `"disabled"` is rejected outright, `"pending"` is an
    account awaiting administrator approval (only reachable today via the
    Google OAuth first-login policy — see `app/google_oidc.py`). `role` and
    `status` are independent: an admin can be disabled, a pending user has
    no role significance until approved.

    `google_subject` is Google's stable, non-reassignable account
    identifier (`sub` claim) — persisted once a Google sign-in links to
    this user, so a later email change or another account claiming the
    same email address can be told apart from the original identity
    (see `app/routers/google_oidc.py`).
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"role IN {USER_ROLES}", name="ck_user_role"),
        CheckConstraint(f"status IN {USER_STATUSES}", name="ck_user_status"),
        CheckConstraint(f"role_source IN {ROLE_SOURCES}", name="ck_user_role_source"),
        UniqueConstraint("google_subject", name="uq_user_google_subject"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # "local": an admin's explicit decision (Admin > Users) or the
    # bootstrap first-admin grant — never touched by OIDC login.
    # "oidc_mapped": owned by the generic-OIDC claim/role mapping flow
    # (issue #18) — recomputed from current claims on every login once
    # an admin configures at least one OidcRoleMapping row; a no-op
    # until then. See app/oidc_role_mapping.py.
    role_source: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    google_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # Issue #47: set once app.pii_redaction.redact_user_pii has run for
    # this user — makes redaction idempotent, same convention as
    # Person.pii_redacted_at.
    pii_redacted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    person: Mapped[Person | None] = relationship()


class UserSession(Base):
    """Server-side session record.

    Only a SHA-256 hash of the opaque session token is stored here — the raw
    token lives solely in the browser's HttpOnly cookie. See app/security.py
    for token issuance/verification.
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship()


POLICY_STATUSES = ("draft", "approved", "retired")
ALLOWED_POLICY_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
POLICY_SOURCE_TYPES = ("manual", "drive")

# issue #31: per-version approval workflow, independent of Policy.status
# above (which stays a document-identity-level "is this policy still
# maintained at all" flag — see docs/superpowers/specs/2026-08-19-issue31-
# policy-lifecycle-design.md §2). "superseded" is the automatic side
# effect of a newer version becoming effective; "withdrawn" is the
# manual terminal action, usable from any non-terminal state (an
# abandoned draft that never reached approval, or a formerly-effective
# version retired with no replacement, are the same action from this
# app's perspective).
POLICY_VERSION_LIFECYCLE_STATUSES = (
    "draft",
    "in_review",
    "approved",
    "effective",
    "superseded",
    "withdrawn",
)
POLICY_VERSION_REVIEW_DECISIONS = ("approved", "rejected")


class Policy(Base):
    """A governance document tracked by this app (metadata only).

    The actual document bytes live under GRC_DATA_DIR/policies/<policy id>/
    (see app/storage.py) — this row and its PolicyVersion children hold
    metadata plus the checksum/path needed to serve them.

    `source_type` distinguishes a manually uploaded policy from one
    associated with a Google Drive file (`drive_*` fields). Association
    with Drive is metadata only — it never makes Drive the archival
    record; captured `PolicyVersion` bytes remain the authoritative
    evidence (see docs/decisions/architectural-decisions.md).
    """

    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    effective_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    next_review_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    drive_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drive_web_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    drive_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    drive_last_seen_revision_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drive_last_synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    versions: Mapped[list[PolicyVersion]] = relationship(
        back_populates="policy",
        cascade="all, delete-orphan",
        order_by="PolicyVersion.version_number.desc()",
    )
    control_mappings: Mapped[list[PolicyControlMapping]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )

    @property
    def latest_version(self) -> PolicyVersion | None:
        return self.versions[0] if self.versions else None

    @property
    def effective_version(self) -> PolicyVersion | None:
        """The version this app currently treats as operative — the
        version-level analog of the old single Policy.status field
        (issue #31). At most one version is ever "effective" at a time
        (enforced by app/policy_lifecycle.py's make_version_effective,
        never by a DB constraint — see its docstring)."""
        return next((v for v in self.versions if v.lifecycle_status == "effective"), None)


class PolicyVersion(Base):
    """One immutable revision of a Policy — uploaded manually or captured
    from Google Drive.

    Versions are never overwritten or deleted by the application — a new
    capture always creates the next `version_number`. `stored_filename` is a
    server-generated name (never the user-supplied/Drive filename) used to
    build the on-disk path; see app/storage.py. `sha256` (of the actually
    stored bytes) remains the authoritative integrity check regardless of
    `source_type`; `source_revision_id`/`source_modified_at` are preserved
    provenance, not a substitute for it.

    The file-capture columns above are a plain, immutable-by-convention
    fact (never event-sourced) — the same category as `EvidenceSnapshot`'s
    captured bytes/hash. `lifecycle_status` and the columns below it
    (issue #31) are different: they are a real event-sourced projection
    over the "policy_version" DomainEvent aggregate (see
    app/policy_lifecycle.py) and must never be written to directly
    outside its projector functions. A rebuild resets only these
    lifecycle columns back to their defaults and replays events on top —
    it never touches the row's own existence or its file-capture columns.
    """

    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version_number", name="uq_policy_version_number"),
        CheckConstraint(
            f"lifecycle_status IN {POLICY_VERSION_LIFECYCLE_STATUSES}",
            name="ck_policy_version_lifecycle_status",
        ),
        # At most one effective version per policy — the final guard
        # against two concurrent requests each making a different
        # already-approved version of the same policy effective (each
        # request's own read of "is another version already effective"
        # can't see the other's uncommitted write; this constraint is
        # what actually stops it, not the application-level check in
        # app/policy_lifecycle.py::make_version_effective, which is only
        # a best-effort convenience for the common single-actor case).
        # Same partial-index pattern as ControlOccurrence's
        # uq_occurrence_control_due_no_period.
        Index(
            "uq_policy_version_one_effective_per_policy",
            "policy_id",
            unique=True,
            sqlite_where=text("lifecycle_status = 'effective'"),
            postgresql_where=text("lifecycle_status = 'effective'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    policy_id: Mapped[str] = mapped_column(ForeignKey("policies.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploader: Mapped[str] = mapped_column(String(255), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    source_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_revision_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_modified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    captured_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    # Event-sourced lifecycle (issue #31) — see app/policy_lifecycle.py.
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    submitted_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    effective_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_versions.id"), nullable=True
    )
    withdrawn_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    withdrawn_reason: Mapped[str] = mapped_column(Text, default="")

    policy: Mapped[Policy] = relationship(back_populates="versions")
    approval_snapshots: Mapped[list[PolicyApprovalSnapshot]] = relationship(
        back_populates="policy_version", cascade="all, delete-orphan"
    )
    superseded_by_version: Mapped[PolicyVersion | None] = relationship(
        remote_side="PolicyVersion.id", foreign_keys=[superseded_by_version_id]
    )


class PolicyApprovalSnapshot(Base):
    """Append-only mirror of one Google Drive Approvals API record.

    Optional capability — tenant availability, permissions, and API
    behavior may vary, so an unavailable Approvals API never fails a
    policy sync (see app/google_drive_approvals.py); the UI shows
    "Approval data unavailable" instead. Never mutated: a changed
    approval status is captured as a new snapshot row (deduplicated only
    on exact-unchanged re-syncs via `raw_payload_sha256`), preserving the
    full history of what changed and when, associated with the exact
    immutable `PolicyVersion` it was mirrored against.
    """

    __tablename__ = "policy_approval_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    policy_version_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"), nullable=False)
    external_approval_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="")
    initiator: Mapped[str] = mapped_column(String(255), default="")
    reviewer_responses_json: Mapped[str] = mapped_column(Text, default="[]")
    create_time: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    modify_time: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    complete_time: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    due_time: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    file_content_change_behavior: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    raw_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    policy_version: Mapped[PolicyVersion] = relationship(back_populates="approval_snapshots")


class PolicyControlMapping(Base):
    """Join table: which internal control this policy governs (issue #31).

    Mirrors ControlRequirementMapping exactly. Frameworks are reached
    transitively via the control's own FrameworkRequirement mappings —
    matching PRD §4.1's "one shared internal control maps to multiple
    framework criteria" invariant — rather than a duplicated direct
    Policy-to-Framework link.
    """

    __tablename__ = "policy_control_mappings"
    __table_args__ = (UniqueConstraint("policy_id", "control_id", name="uq_policy_control"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    policy_id: Mapped[str] = mapped_column(ForeignKey("policies.id"), nullable=False)
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    policy: Mapped[Policy] = relationship(back_populates="control_mappings")
    control: Mapped[InternalControl] = relationship()


class GoogleDriveConnection(Base):
    """One historical record of an org-level Google Drive OAuth connection.

    Append-only like PolicyVersion: connecting again always creates a new
    row rather than mutating a past one, so "who connected this and when"
    stays a real history. The *active* connection is the most recent row
    with `revoked_at IS NULL`. `encrypted_refresh_token` is ciphertext
    (see app/crypto.py) — the plaintext token is never stored, logged, or
    exposed in any template, audit payload, or error message. Disconnect
    clears it to `""` and stamps `revoked_at`/`revoked_by_user_id`, but
    keeps the row so the connection's history remains visible.
    """

    __tablename__ = "google_drive_connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    connected_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    connected_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    granted_scopes: Mapped[str] = mapped_column(String(512), default="")
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    last_successful_sync_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    connected_by: Mapped[User] = relationship(foreign_keys=[connected_by_user_id])
    revoked_by: Mapped[User | None] = relationship(foreign_keys=[revoked_by_user_id])


class AwsConnection(Base):
    """Configuration for one AWS account this app collects evidence from.

    Never stores long-lived AWS access keys — only an optional role ARN
    for `AssumeRole` (ambient workload credentials are used otherwise).
    `external_id` is encrypted at rest (see app/crypto.py) since it's a
    piece of the assume-role trust boundary, even though it isn't a
    bearer credential the way an access key would be. Unlike
    `GoogleDriveConnection`, this is plain configuration (no OAuth grant
    lifecycle), so it's updated in place — audited like any other
    settings change, not append-only.
    """

    __tablename__ = "aws_connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    account_label: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_account_id: Mapped[str] = mapped_column(String(32), default="")
    role_arn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    encrypted_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    regions: Mapped[str] = mapped_column(String(512), default="us-east-1")
    configured_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_check_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_summary: Mapped[str] = mapped_column(Text, default="")

    configured_by: Mapped[User] = relationship()


EVIDENCE_STATUSES = ("pass", "fail", "warning", "unknown")


class EvidenceSnapshot(Base):
    """An immutable, point-in-time capture of evidence from an external
    source (AWS CloudTrail/IAM today; Google Drive/Workspace could feed
    this table too later). No edit/delete route exists — a correction is
    always a new snapshot, never a mutation. `normalized_payload_json` is
    a bounded, secret-free summary (never raw credentials, tokens, or full
    API responses); `raw_payload_sha256` lets an auditor confirm this
    snapshot corresponds to a specific underlying collection without this
    app needing to retain the raw payload itself.
    """

    __tablename__ = "evidence_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_connection_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    check_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    collected_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    collector_version: Mapped[str] = mapped_column(String(32), default="1")
    normalized_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    raw_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    requirement_mappings: Mapped[list[EvidenceRequirementMapping]] = relationship(
        back_populates="evidence_snapshot", cascade="all, delete-orphan"
    )
    control_mappings: Mapped[list[EvidenceControlMapping]] = relationship(
        back_populates="evidence_snapshot", cascade="all, delete-orphan"
    )


class EvidenceRequirementMapping(Base):
    """Join table: which evidence snapshot supports which framework requirement."""

    __tablename__ = "evidence_requirement_mappings"
    __table_args__ = (
        UniqueConstraint("evidence_snapshot_id", "requirement_id", name="uq_evidence_requirement"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    evidence_snapshot_id: Mapped[str] = mapped_column(ForeignKey("evidence_snapshots.id"), nullable=False)
    requirement_id: Mapped[str] = mapped_column(ForeignKey("framework_requirements.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    evidence_snapshot: Mapped[EvidenceSnapshot] = relationship(back_populates="requirement_mappings")
    requirement: Mapped[FrameworkRequirement] = relationship()


class EvidenceControlMapping(Base):
    """Join table: which evidence snapshot supports which internal control."""

    __tablename__ = "evidence_control_mappings"
    __table_args__ = (UniqueConstraint("evidence_snapshot_id", "control_id", name="uq_evidence_control"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    evidence_snapshot_id: Mapped[str] = mapped_column(ForeignKey("evidence_snapshots.id"), nullable=False)
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    evidence_snapshot: Mapped[EvidenceSnapshot] = relationship(back_populates="control_mappings")
    control: Mapped[InternalControl] = relationship()


class EvidenceArtifact(Base):
    """A logical, durable evidence artifact miniGRC owns independent of
    any external source (issue #32) — the container for its immutable
    captured versions. A plain row, not event-sourced (same category as
    Policy itself); the material fact is each captured version, not the
    container's own existence.

    Complementary to, not a replacement for, EvidenceSnapshot: that
    table answers "what did a connector observe" (a bytes-free
    normalized fact); this answers "here is the actual durable file
    that constitutes evidence." A connector capturing a real file may
    populate both for the same collection run (see
    EvidenceArtifactVersion.linked_evidence_snapshot_id), but neither
    requires the other to exist.
    """

    __tablename__ = "evidence_artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    versions: Mapped[list[EvidenceArtifactVersion]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        order_by="EvidenceArtifactVersion.version_number.desc()",
    )

    @property
    def latest_version(self) -> EvidenceArtifactVersion | None:
        return self.versions[0] if self.versions else None


EVIDENCE_ARTIFACT_SOURCE_TYPES = ("manual", "connector")


class EvidenceArtifactVersion(Base):
    """One immutable captured version of an EvidenceArtifact — a
    canonical projection over the "evidence_artifact_version"
    DomainEvent aggregate (app/events.py), never written to directly
    outside app/evidence_repository.py's projector. Unlike
    PolicyVersion (#31), the row's own existence is event-sourced too,
    not just its workflow status — this is a brand-new table with no
    pre-existing rows to preserve, so there is no reason to keep it a
    plain insert (see design doc §2).

    `object_key` is entirely server-generated
    (`evidence/<artifact_id>/<version_id>/...`) — the original filename
    is kept only for display/Content-Disposition on download, never as
    a path/key component (mirrors app/storage.py's
    sanitize_original_filename precedent).
    """

    __tablename__ = "evidence_artifact_versions"
    __table_args__ = (UniqueConstraint("artifact_id", "version_number", name="uq_evidence_artifact_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    artifact_id: Mapped[str] = mapped_column(ForeignKey("evidence_artifacts.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(nullable=False)

    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    source_connection_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_object_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_revision_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_modified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    linked_evidence_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_snapshots.id"), nullable=True
    )

    captured_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False, default="system")
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    artifact: Mapped[EvidenceArtifact] = relationship(back_populates="versions")
    linked_evidence_snapshot: Mapped[EvidenceSnapshot | None] = relationship()


SECRET_KINDS = ("encrypted", "env_ref")


class Secret(Base):
    """A named credential, stored either app-encrypted or as an external reference.

    Shared foundation for external database connections (Feature 6) and
    future integration credentials — never returns its stored value
    through a serializer, never logs it, and its __repr__ deliberately
    omits `ciphertext`/`env_var_name`'s resolved value. See
    app/secrets.py for the create/resolve service functions and
    app/crypto.py for the underlying Fernet encryption. Two modes:
    `encrypted` (ciphertext set, resolved via GRC_ENCRYPTION_KEY) or
    `env_ref` (env_var_name set, resolved from the process environment
    at use time — for Kubernetes Secret-mounted-as-env-var deployments).
    """

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("name", name="uq_secret_name"),
        CheckConstraint(f"kind IN {SECRET_KINDS}", name="ck_secret_kind"),
        CheckConstraint(
            "(kind = 'encrypted' AND ciphertext IS NOT NULL AND env_var_name IS NULL) OR "
            "(kind = 'env_ref' AND env_var_name IS NOT NULL AND ciphertext IS NULL)",
            name="ck_secret_kind_fields",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_var_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"Secret(id={self.id!r}, name={self.name!r}, kind={self.kind!r})"


class GoogleOidcSettings(Base):
    """Admin-configured Google OAuth login settings — the single most
    recently updated row is the active configuration (same "plain config
    updated in place" shape as `AwsConnection`, not an OAuth grant
    history like `GoogleDriveConnection`). The client secret is never
    stored directly here: `secret_id` references a `Secret` (encrypted via
    `app/crypto.py`), so the plaintext is resolved server-side only and
    never redisplayed after save. When no row exists (or the row is
    disabled), `app/google_oidc_config.py` falls back to the legacy
    `GRC_GOOGLE_OIDC_*` environment variables, preserving existing
    env-var-only deployments.
    """

    __tablename__ = "google_oidc_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    enabled: Mapped[bool] = mapped_column(default=False)
    client_id: Mapped[str] = mapped_column(String(255), default="")
    secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)
    allowed_domains: Mapped[str] = mapped_column(Text, default="")
    auto_provision_enabled: Mapped[bool] = mapped_column(default=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    secret: Mapped[Secret | None] = relationship()

    def __repr__(self) -> str:
        return f"GoogleOidcSettings(id={self.id!r}, enabled={self.enabled!r})"


class OidcProviderSettings(Base):
    """Admin-configured generic OIDC login settings (issue #17) — the
    single most recently updated row is the active configuration, same
    shape as `GoogleOidcSettings`. `issuer` drives discovery
    (`{issuer}/.well-known/openid-configuration`); no provider-specific
    endpoints are ever hardcoded, so Authentik/Keycloak/Okta/Entra/etc. all
    configure through the same fields. Distinct from `GoogleOidcSettings`,
    which keeps its own hardcoded Google-only flow (see app/google_oidc.py)
    — the two coexist as separate login paths. The client secret is never
    stored directly here: `secret_id` references a `Secret` (encrypted via
    app/crypto.py), resolved server-side only and never redisplayed after
    save. When no row exists (or the row is disabled),
    `app/oidc_config.py` falls back to `GRC_OIDC_*` environment variables.
    """

    __tablename__ = "oidc_provider_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    enabled: Mapped[bool] = mapped_column(default=False)
    display_name: Mapped[str] = mapped_column(String(255), default="SSO")
    issuer: Mapped[str] = mapped_column(String(500), default="")
    client_id: Mapped[str] = mapped_column(String(255), default="")
    secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)
    allowed_domains: Mapped[str] = mapped_column(Text, default="")
    auto_provision_enabled: Mapped[bool] = mapped_column(default=False)
    # Which verified ID token claim carries group/role membership (issue
    # #18) — read as-is: a list claim is used item-by-item, a string
    # claim is treated as one value, never split on a guessed delimiter.
    # See app/oidc_role_mapping.py.
    role_claim_name: Mapped[str] = mapped_column(String(255), default="groups")
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    secret: Mapped[Secret | None] = relationship()

    def __repr__(self) -> str:
        return f"OidcProviderSettings(id={self.id!r}, enabled={self.enabled!r})"


class ExternalIdentity(Base):
    """A provider-neutral `(issuer, subject)` -> user link for the generic
    OIDC login path (issue #17) — deterministic external identity
    authority per PRD §5.11. Email is never the link key; a first-time
    `(issuer, subject)` whose asserted email collides with an existing
    local user is rejected rather than auto-linked (see
    app/routers/oidc.py). Google's own login keeps using
    `User.google_subject` instead of this table — it is a separate,
    single-provider mechanism that predates this one and is left as-is.
    """

    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_external_identity_issuer_subject"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship()

    def __repr__(self) -> str:
        return f"ExternalIdentity(id={self.id!r}, issuer={self.issuer!r})"


class OidcRoleMapping(Base):
    """Admin-configured claim-value -> miniGRC role mapping for the
    generic OIDC login path (issue #18). One row per distinct claim
    value — unique so there is never an ambiguous "which mapping wins"
    question for a single value; when a user's claims match more than
    one row, `app/oidc_role_mapping.py::compute_mapped_role` picks the
    highest-precedence role using `USER_ROLES`'s existing order.
    """

    __tablename__ = "oidc_role_mappings"
    __table_args__ = (
        UniqueConstraint("claim_value", name="uq_oidc_role_mapping_claim_value"),
        CheckConstraint(f"role IN {USER_ROLES}", name="ck_oidc_role_mapping_role"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    claim_value: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"OidcRoleMapping(id={self.id!r}, claim_value={self.claim_value!r}, role={self.role!r})"


CONNECTOR_EXECUTION_STATUSES = ("success", "failure", "error")
CONNECTOR_EXECUTION_TRIGGERS = ("manual", "system")


class ConnectorInstance(Base):
    """One configured instance of a manifest-declared connector
    (app.connectors.manifest.ConnectorManifest, issue #24) — issue #25's
    installation/configuration/health state. Mutable, updated in place
    (like AwsConnection) — plain deployment configuration, not a
    credential-grant history. `connector_id` matches a
    ConnectorManifest.connector_id; not a DB foreign key, since
    manifests are Python objects, not rows.

    `config_json` holds only non-secret config field values.
    `secret_ref_json` holds `{field_name: secret_id}` for every field
    the manifest marks `secret=True` — resolved just-in-time via
    app.secrets.resolve_secret, never stored or returned as plaintext.
    """

    __tablename__ = "connector_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    connector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_version: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    secret_ref_json: Mapped[str] = mapped_column(Text, default="{}")
    last_test_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_test_error: Mapped[str] = mapped_column(Text, default="")
    last_success_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_summary: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"ConnectorInstance(id={self.id!r}, connector_id={self.connector_id!r})"


class ConnectorExecution(Base):
    """One invocation attempt of a `ConnectorInstance` — append-only
    execution history and idempotency ledger (issue #25). A repeated
    `idempotency_key` for the same instance returns the existing row
    rather than re-invoking the connector or re-ingesting a fact.
    """

    __tablename__ = "connector_executions"
    __table_args__ = (
        UniqueConstraint(
            "connector_instance_id", "idempotency_key", name="uq_connector_execution_instance_key"
        ),
        CheckConstraint(f"status IN {CONNECTOR_EXECUTION_STATUSES}", name="ck_connector_execution_status"),
        CheckConstraint(
            f"triggered_by IN {CONNECTOR_EXECUTION_TRIGGERS}", name="ck_connector_execution_triggered_by"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    connector_instance_id: Mapped[str] = mapped_column(ForeignKey("connector_instances.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    error_summary: Mapped[str] = mapped_column(Text, default="")
    triggered_by: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"ConnectorExecution(id={self.id!r}, status={self.status!r})"


CONNECTION_DB_TYPES = ("postgres", "mysql", "sqlite", "generic")
CONNECTION_TLS_MODES = ("disable", "prefer", "require", "verify_full")
CONNECTION_TEST_STATUSES = ("untested", "success", "failure")


class ExternalConnection(Base):
    """A read-only connection to a customer/organization database, for
    future evidence/inventory collection — separate from this app's own
    database (see app/db.py). Credentials are never stored directly here;
    `secret_id` references a Secret (app/secrets.py) holding either an
    encrypted password (postgres/mysql) or an encrypted full URL
    (generic). sqlite connections need no secret (local file access).
    `__repr__` deliberately excludes every credential-adjacent field.
    """

    __tablename__ = "external_connections"
    __table_args__ = (
        UniqueConstraint("name", name="uq_external_connection_name"),
        CheckConstraint(f"db_type IN {CONNECTION_DB_TYPES}", name="ck_connection_db_type"),
        CheckConstraint(f"tls_mode IN {CONNECTION_TLS_MODES}", name="ck_connection_tls_mode"),
        CheckConstraint(f"last_test_status IN {CONNECTION_TEST_STATUSES}", name="ck_connection_test_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    db_type: Mapped[str] = mapped_column(String(16), nullable=False)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sqlite_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)
    tls_mode: Mapped[str] = mapped_column(String(16), default="prefer")
    enabled: Mapped[bool] = mapped_column(default=True)
    owner: Mapped[str] = mapped_column(String(255), default="")
    last_tested_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_status: Mapped[str] = mapped_column(String(16), default="untested")
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    secret: Mapped[Secret | None] = relationship()

    def __repr__(self) -> str:
        return f"ExternalConnection(id={self.id!r}, name={self.name!r}, db_type={self.db_type!r})"


JOB_STATUSES = ("pending", "running", "succeeded", "failed")


class Job(Base):
    """A persisted, claimable unit of background work (Feature 7's worker
    foundation). Database-backed rather than a separate broker (Redis) —
    see ADR #24. `payload_json`/`result_json` are plain JSON-encoded text
    (portable across SQLite/Postgres without a JSON column type
    dependency). Claiming uses a guarded UPDATE (WHERE status='pending',
    or the full stale-running predicate for reclaim) rather than
    SELECT...FOR UPDATE SKIP LOCKED, so it works on both dialects without
    a dialect-specific locking clause. See app/jobs.py for the claim/run/
    retry logic and its regression test for the TOCTOU fix on the
    stale-running reclaim path.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        CheckConstraint(f"status IN {JOB_STATUSES}", name="ck_job_status"),
        Index("ix_jobs_status_available_at", "status", "available_at"),
        Index("ix_jobs_status_claimed_at", "status", "claimed_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    available_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


IMPORT_SOURCES = ("web", "cli", "watched_directory")
IMPORT_JOB_STATUSES = ("pending", "validating", "importing", "completed", "rejected")


class ImportJob(Base):
    """A record of one attempted import — web upload, CLI, or (Feature 9)
    watched-directory — through app/imports.py's shared importer registry.

    Always all-or-nothing: `records_created` is 0 whenever `status` is
    "rejected" (see app/imports.py::run_import, which never writes a
    partial import). `checksum_sha256` makes re-running the exact same
    file a safe no-op (see the idempotency check in run_import) rather
    than a duplicate import.
    """

    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(f"source IN {IMPORT_SOURCES}", name="ck_import_job_source"),
        CheckConstraint(f"status IN {IMPORT_JOB_STATUSES}", name="ck_import_job_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    importer_name: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), default="")
    file_size: Mapped[int] = mapped_column(default=0)
    checksum_sha256: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    target_json: Mapped[str] = mapped_column(Text, default="{}")
    records_discovered: Mapped[int] = mapped_column(default=0)
    records_created: Mapped[int] = mapped_column(default=0)
    records_updated: Mapped[int] = mapped_column(default=0)
    records_skipped: Mapped[int] = mapped_column(default=0)
    records_rejected: Mapped[int] = mapped_column(default=0)
    validation_errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


TRUST_CENTER_SECTION_VISIBILITY = ("public", "restricted", "internal")
TRUST_CENTER_SECTION_STATUSES = ("draft", "published")


class TrustCenterSettings(Base):
    """Single-row configuration for the Trust Center feature.

    Deliberately one row, not a generic app-settings table (see
    CLAUDE.md's "no generic abstraction without a second caller") —
    `app/trust_center.py::get_or_create_settings` enforces the
    singleton. `enabled` gates whether the public route (a later
    feature) responds at all; everything else is branding/contact copy
    shown alongside published sections.
    """

    __tablename__ = "trust_center_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    enabled: Mapped[bool] = mapped_column(default=False)
    title: Mapped[str] = mapped_column(String(255), default="Trust Center")
    intro_markdown: Mapped[str] = mapped_column(Text, default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    support_url: Mapped[str] = mapped_column(String(2048), default="")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class TrustCenterSection(Base):
    """One ordered block of public-facing Trust Center content.

    Holds both the current draft (`draft_body_markdown`, always
    editable) and a snapshot of what was last published
    (`published_body_markdown`/`published_at`/`published_by_user_id`).
    Publishing copies draft -> published; unpublishing flips `status`
    back to "draft" without clearing the last published snapshot, so
    re-publishing is a one-click restore and the history stays visible
    via AuditEvent rows (see app/audit.py) rather than a second
    versioned table — this content is lightweight CMS copy, not
    immutable evidence like PolicyVersion.

    `linked_framework_id`/`linked_policy_id` are optional references
    into existing internal records (never a copy of their data) so a
    section can point at "our ISO 27001 program" or a specific
    approved policy without duplicating fields — a later public route
    is responsible for rendering only the safe subset (e.g. a policy's
    title and an approved-version download, never internal notes).

    `visibility` includes "restricted" in the schema now even though no
    NDA/gated-access workflow exists yet, so a future feature can add
    one without a migration; only "public" sections are ever meant to
    reach an unauthenticated visitor.
    """

    __tablename__ = "trust_center_sections"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="ck_trust_center_section_title_not_blank"),
        CheckConstraint(
            f"visibility IN {TRUST_CENTER_SECTION_VISIBILITY}", name="ck_trust_center_section_visibility"
        ),
        CheckConstraint(f"status IN {TRUST_CENTER_SECTION_STATUSES}", name="ck_trust_center_section_status"),
        Index("ix_trust_center_sections_visibility_status", "visibility", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    display_order: Mapped[int] = mapped_column(default=0)

    draft_body_markdown: Mapped[str] = mapped_column(Text, default="")
    published_body_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    published_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    review_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    expiry_date: Mapped[datetime.date | None] = mapped_column(nullable=True)

    linked_framework_id: Mapped[str | None] = mapped_column(ForeignKey("frameworks.id"), nullable=True)
    linked_policy_id: Mapped[str | None] = mapped_column(ForeignKey("policies.id"), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    linked_framework: Mapped[Framework | None] = relationship(foreign_keys=[linked_framework_id])
    linked_policy: Mapped[Policy | None] = relationship(foreign_keys=[linked_policy_id])
    published_by: Mapped[User | None] = relationship(foreign_keys=[published_by_user_id])


class AuditEvent(Base):
    """Append-only record of who changed what, for auditor-facing history.

    Written by application code alongside a mutation (see
    app/audit.py) rather than derived from a generic ORM hook, so the
    `action`/`detail` text stays deliberate and readable instead of a raw
    diff dump.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(255), default="system")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class FrameworkAdoption(Base):
    """Canonical projection over the "framework_adoption" DomainEvent
    aggregate (issue #43) — NOT the source of truth for history. One row
    per `Framework.catalog_family`: which version of that catalog family
    the organization currently treats as its active, adopted version.

    Never touches ControlRequirementMapping/FrameworkRequirement/
    ControlPeriod rows — those already stay permanently pinned to
    whichever Framework row they were created against, since nothing in
    this codebase ever repoints an existing FK (see
    docs/superpowers/specs/2026-08-20-issue43-catalog-version-pinning-design.md
    §"Repository-reality check"). This table only tracks which version is
    "current" going forward.
    """

    __tablename__ = "framework_adoptions"
    __table_args__ = (UniqueConstraint("catalog_family", name="uq_framework_adoption_catalog_family"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # == aggregate_id; no default=new_id
    catalog_family: Mapped[str] = mapped_column(String(32), nullable=False)
    framework_id: Mapped[str] = mapped_column(ForeignKey("frameworks.id"), nullable=False)
    previous_framework_id: Mapped[str | None] = mapped_column(ForeignKey("frameworks.id"), nullable=True)
    adopted_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    upgraded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    framework: Mapped[Framework] = relationship(foreign_keys="FrameworkAdoption.framework_id")
    previous_framework: Mapped[Framework | None] = relationship(
        foreign_keys="FrameworkAdoption.previous_framework_id"
    )
