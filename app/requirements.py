"""Shared helper for creating a FrameworkRequirement with its assessment.

Every requirement always has exactly one RequirementAssessment — created
here so the three call sites (seed data, manual add, CSV import) can't
drift out of sync and leave a requirement with no assessment row.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Framework, FrameworkRequirement, RequirementAssessment


def add_requirement(
    session: Session,
    framework: Framework,
    *,
    reference_code: str,
    title: str,
    summary: str = "",
    display_order: int = 0,
) -> FrameworkRequirement:
    requirement = FrameworkRequirement(
        framework_id=framework.id,
        reference_code=reference_code,
        title=title,
        summary=summary,
        display_order=display_order,
    )
    session.add(requirement)
    session.flush()
    session.add(RequirementAssessment(requirement_id=requirement.id))
    return requirement
