"""Framework completion percentage: implemented / applicable requirements."""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Framework


@dataclass(frozen=True)
class FrameworkProgress:
    total: int
    applicable: int
    implemented: int
    in_progress: int
    not_started: int
    not_applicable: int
    percent: int | None  # None when there are no applicable requirements yet


def compute_progress(framework: Framework) -> FrameworkProgress:
    assessments = [r.assessment for r in framework.requirements if r.assessment is not None]
    applicable = [a for a in assessments if a.applicable == "yes"]
    implemented = [a for a in applicable if a.implementation_state == "implemented"]
    in_progress = [a for a in applicable if a.implementation_state == "in_progress"]
    not_started = [a for a in applicable if a.implementation_state == "not_started"]
    not_applicable = [a for a in assessments if a.applicable == "no"]

    percent = round(100 * len(implemented) / len(applicable)) if applicable else None

    return FrameworkProgress(
        total=len(assessments),
        applicable=len(applicable),
        implemented=len(implemented),
        in_progress=len(in_progress),
        not_started=len(not_started),
        not_applicable=len(not_applicable),
        percent=percent,
    )
