"""SQLAlchemy models for the GRC/ISMS domain foundation.

Scope for this PR (see docs/domain/domain-model.md for the research behind
these choices): frameworks, framework requirements, internal controls, the
requirement<->control mapping, risks, and audit events. Policies, evidence,
connectors, actions, and the trust center are intentionally NOT modeled yet
— they remain placeholder pages until a real product need justifies a table
(see docs/product-scope.md).

IDs are ULIDs (lexicographically sortable, generated in application code)
stored as TEXT primary keys — no autoincrement integers, so ids are stable
across export/import and safe to reference from external systems later.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text
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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    requirements: Mapped[list[FrameworkRequirement]] = relationship(
        back_populates="framework",
        cascade="all, delete-orphan",
        order_by="FrameworkRequirement.reference_code",
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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    framework_id: Mapped[str] = mapped_column(ForeignKey("frameworks.id"), nullable=False)
    reference_code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    framework: Mapped[Framework] = relationship(back_populates="requirements")
    mappings: Mapped[list[ControlRequirementMapping]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan"
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
    """Join table: which internal control satisfies which requirement."""

    __tablename__ = "control_requirement_mappings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    control_id: Mapped[str] = mapped_column(ForeignKey("internal_controls.id"), nullable=False)
    requirement_id: Mapped[str] = mapped_column(ForeignKey("framework_requirements.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    control: Mapped[InternalControl] = relationship(back_populates="mappings")
    requirement: Mapped[FrameworkRequirement] = relationship(back_populates="mappings")


class Risk(Base):
    """A structured risk register entry.

    Likelihood/impact use a plain 1-5 scale rather than a configurable risk
    matrix — the simplest thing that supports sorting and a visible score.
    Risk treatment is a free-text field for now; a dedicated treatment/
    exception workflow is future scope (see docs/product-scope.md).
    """

    __tablename__ = "risks"

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
