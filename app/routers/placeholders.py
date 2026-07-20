"""Honest placeholder pages for product areas not built yet.

Each entry states current status, intended responsibility, and whether the
system of record will be internal or an external tool — see
docs/product-scope.md for the reasoning behind each.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.deps import require_login

router = APIRouter(dependencies=[Depends(require_login)])

PLACEHOLDERS = {
    "actions": {
        "title": "Actions",
        "status": "Not implemented. Asana remains the source of truth.",
        "responsibility": (
            "Corrective actions and exceptions will link out to Asana tasks, not a duplicate tracker here."
        ),
        "source_of_truth": "External — Asana",
    },
    "connectors": {
        "title": "Connectors",
        "status": "Google Drive (policy sources + approvals) implemented — see /connectors/google-drive. "
        "Optional Google Workspace Directory sync implemented — see People. "
        "AWS CloudTrail/IAM evidence implemented — see /connectors/aws and Evidence. "
        "GitHub/Azure/Asana connectors not built in this PR.",
        "responsibility": (
            "Each connector is a small module with a connection test, supported checks, and "
            "evidence output — built one at a time, not a generic connector SDK."
        ),
        "source_of_truth": "External systems; this app stores results",
    },
}


@router.get("/{slug}")
def placeholder_page(slug: str, request: Request):
    if slug not in PLACEHOLDERS:
        raise HTTPException(status_code=404, detail="Not found")

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "placeholder.html", PLACEHOLDERS[slug])
