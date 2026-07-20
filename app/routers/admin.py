"""Admin shell — landing page for the dedicated Admin product area.

Every route under `/admin` depends on `require_admin`: hiding the nav link
is not authorization, so this is enforced here regardless of what the
sidebar shows a non-admin user. Sub-areas (Users, Connections,
Authentication, Jobs, Audit Log, Settings) register themselves into
`ADMIN_NAV_ITEMS` as they ship, so this module doesn't need to change to
list them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.deps import require_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("")
def admin_index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/index.html", {})
