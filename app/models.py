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

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Framework(Base):
    """A compliance framework, e.g. an ISO 27001 catalogue.

    Modeled so more than one framework can exist side by side eventually
    (a second ISO revision, SOC 2, etc.) without schema changes — but this
    PR seeds exactly one, clearly labelled as sample/placeholder content.
    """

    __tablename__ = "frameworks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    is_placeholder_content: Mapped[bool] = mapped_column(default=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

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


class InternalControl(Base):
    """An internal control the organization actually operates.

    Distinct from FrameworkRequirement: a requirement is "what the
    framework asks for," a control is "what we actually do." One control
    can satisfy several requirements (even across frameworks later), hence
    the many-to-many mapping table rather than a foreign key here.
    """

    __tablename__ = "internal_controls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="not_started")
    review_frequency: Mapped[str] = mapped_column(String(32), default="annual")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    mappings: Mapped[list[ControlRequirementMapping]] = relationship(
        back_populates="control", cascade="all, delete-orphan"
    )


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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


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


USER_ROLES = ("user", "admin")


class User(Base):
    """A local application user.

    Email is the login identifier, normalized to lowercase and stored
    unique. Passwords are hashed with pwdlib (Argon2) — see app/security.py.
    `role` is a binary distinction (`"user"` or `"admin"`) — not general
    RBAC — used to gate integration configuration, credential connections,
    manual syncs, and destructive vendor operations. Every other
    authenticated action remains available to any logged-in user (see
    docs/decisions/architectural-decisions.md). `person_id` optionally links
    this login identity to the shared `Person` directory.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

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

    @property
    def latest_version(self) -> PolicyVersion | None:
        return self.versions[0] if self.versions else None


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
    """

    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("policy_id", "version_number", name="uq_policy_version_number"),)

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

    policy: Mapped[Policy] = relationship(back_populates="versions")
    approval_snapshots: Mapped[list[PolicyApprovalSnapshot]] = relationship(
        back_populates="policy_version", cascade="all, delete-orphan"
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
    dependency). Claiming uses a guarded UPDATE (WHERE status='pending')
    rather than SELECT...FOR UPDATE SKIP LOCKED, so it works identically
    on both dialects. See app/jobs.py for the claim/run/retry logic.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        CheckConstraint(f"status IN {JOB_STATUSES}", name="ck_job_status"),
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
