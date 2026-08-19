"""Audit/PBC package export routes (issue #34).

`require_admin` router-level — matches app/routers/audit_log.py's
"bulk/sensitive data leaves the system" convention, distinct from the
`require_write_access` used for ordinary single-object mutations
elsewhere in this app. See
docs/superpowers/specs/2026-08-19-issue34-audit-pbc-package-design.md §4.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.audit_package import AuditPackageError, generate_audit_package
from app.compliance_scope import get_compliance_scope
from app.deps import get_db, require_admin, verify_csrf
from app.flash import redirect_with_flash

router = APIRouter(
    prefix="/admin/audit-package", tags=["audit-package"], dependencies=[Depends(require_admin)]
)


@router.get("")
def audit_package_form(request: Request, db: Session = Depends(get_db)):
    scope = get_compliance_scope(db)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "audit_package/form.html", {"scope": scope})


@router.post("")
def create_audit_package(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    try:
        package_bytes, manifest_sha256 = generate_audit_package(
            db, request.app.state.settings, actor_email=admin.email
        )
    except AuditPackageError as exc:
        return redirect_with_flash("/admin/audit-package", str(exc), kind="error")

    record_audit_event(
        db,
        entity_type="audit_package",
        entity_id=manifest_sha256[:32],
        action="generate",
        detail=f"Generated audit/PBC package (manifest sha256 {manifest_sha256})",
        actor=admin.email,
    )

    filename = f"audit-package-{manifest_sha256[:12]}.zip"
    return Response(
        content=package_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
