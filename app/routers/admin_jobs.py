"""Admin > Jobs: a read-only list over the existing Job queue.

Previously only visible embedded per-connection; this is the first
standalone view. Read-only — jobs are still enqueued/claimed by their
existing producers (connection tests, imports, connector syncs), not
created or edited here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.deps import require_admin
from app.models import Job
from app.registers.config import FieldSpec, RegisterConfig
from app.registers.router import build_register_router

router = APIRouter(prefix="/admin/jobs", tags=["admin"], dependencies=[Depends(require_admin)])


def _truncate(value: str | None, limit: int = 200) -> str:
    if not value:
        return ""
    return value if len(value) <= limit else f"{value[:limit]}…"


JOBS_REGISTER_CONFIG = RegisterConfig(
    name="admin_jobs",
    model=Job,
    entity_type="job",
    order_by=Job.available_at.desc(),
    creatable=False,
    deletable=False,
    bulk_enabled=False,
    require_admin_for=frozenset({"list", "edit"}),
    fields=(
        FieldSpec(name="job_type", type="text", read_only=True),
        FieldSpec(name="status", type="text", read_only=True),
        FieldSpec(name="attempts", type="number", read_only=True),
        FieldSpec(
            name="available_at",
            type="text",
            read_only=True,
            compute=lambda j: j.available_at.strftime("%Y-%m-%d %H:%M UTC") if j.available_at else None,
        ),
        FieldSpec(
            name="claimed_at",
            type="text",
            read_only=True,
            compute=lambda j: j.claimed_at.strftime("%Y-%m-%d %H:%M UTC") if j.claimed_at else None,
        ),
        FieldSpec(
            name="error_message", type="text", read_only=True, compute=lambda j: _truncate(j.error_message)
        ),
    ),
)

jobs_register_router = build_register_router(JOBS_REGISTER_CONFIG)


@router.get("")
def list_jobs(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/jobs/list.html", {})
