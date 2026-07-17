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


class User(Base):
    """A local application user.

    Email is the login identifier, normalized to lowercase and stored
    unique. Passwords are hashed with pwdlib (Argon2) — see app/security.py.
    All authenticated users share the same permissions in this MVP (see
    docs/decisions/architectural-decisions.md).
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


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


class Policy(Base):
    """A governance document tracked by this app (metadata only).

    The actual document bytes live under GRC_DATA_DIR/policies/<policy id>/
    (see app/storage.py) — this row and its PolicyVersion children hold
    metadata plus the checksum/path needed to serve them.
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

    versions: Mapped[list[PolicyVersion]] = relationship(
        back_populates="policy",
        cascade="all, delete-orphan",
        order_by="PolicyVersion.version_number.desc()",
    )

    @property
    def latest_version(self) -> PolicyVersion | None:
        return self.versions[0] if self.versions else None


class PolicyVersion(Base):
    """One immutable uploaded revision of a Policy.

    Versions are never overwritten or deleted by the application — a new
    upload always creates the next `version_number`. `stored_filename` is a
    server-generated name (never the user-supplied original filename) used
    to build the on-disk path; see app/storage.py.
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

    policy: Mapped[Policy] = relationship(back_populates="versions")


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
