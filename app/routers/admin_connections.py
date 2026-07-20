"""Admin > Connections: a unified, Metabase-style index over every
connection type this app has (external DB connections, the AWS account,
the Google Drive connection) without replacing any of their existing
add/edit/test/detail routes or storage models — this is a presentation
layer only. Each card links to its type's existing detail/edit page.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin
from app.models import AwsConnection, ExternalConnection, GoogleDriveConnection

router = APIRouter(prefix="/admin/connections", tags=["admin"], dependencies=[Depends(require_admin)])
legacy_router = APIRouter()


@dataclasses.dataclass(frozen=True)
class ConnectionCard:
    name: str
    connection_type: str
    status_label: str
    status_class: str
    last_activity: str
    detail_url: str


def _external_connection_cards(db: Session) -> list[ConnectionCard]:
    rows = db.scalars(select(ExternalConnection).order_by(ExternalConnection.name)).all()
    cards = []
    for row in rows:
        status_class = {"success": "success", "failure": "danger"}.get(row.last_test_status, "secondary")
        label = f"Database ({row.db_type})" if row.enabled else f"Database ({row.db_type}) — disabled"
        cards.append(
            ConnectionCard(
                name=row.name,
                connection_type=label,
                status_label=row.last_test_status,
                status_class=status_class,
                last_activity=row.last_tested_at.isoformat() if row.last_tested_at else "Never tested",
                detail_url=f"/connections/{row.id}/edit",
            )
        )
    return cards


def _aws_connection_card(db: Session) -> ConnectionCard | None:
    row = db.scalar(select(AwsConnection).order_by(AwsConnection.created_at.desc()).limit(1))
    if row is None:
        return None
    status_class = "danger" if row.last_error_summary else "success" if row.last_check_at else "secondary"
    return ConnectionCard(
        name=row.account_label,
        connection_type="AWS account",
        status_label=row.last_error_summary or ("Healthy" if row.last_check_at else "Not yet checked"),
        status_class=status_class,
        last_activity=row.last_check_at.isoformat() if row.last_check_at else "Never checked",
        detail_url="/connectors/aws",
    )


def _google_drive_connection_card(db: Session) -> ConnectionCard | None:
    row = db.scalar(
        select(GoogleDriveConnection)
        .where(GoogleDriveConnection.revoked_at.is_(None))
        .order_by(GoogleDriveConnection.connected_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    last_sync = row.last_successful_sync_at.isoformat() if row.last_successful_sync_at else "Never synced"
    return ConnectionCard(
        name="Google Drive",
        connection_type="Google Drive (OAuth)",
        status_label="Connected",
        status_class="success",
        last_activity=last_sync,
        detail_url="/connectors/google-drive",
    )


@router.get("")
def list_connections(request: Request, db: Session = Depends(get_db)):
    cards = _external_connection_cards(db)
    aws_card = _aws_connection_card(db)
    if aws_card is not None:
        cards.append(aws_card)
    drive_card = _google_drive_connection_card(db)
    if drive_card is not None:
        cards.append(drive_card)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/connections/index.html", {"cards": cards})


@legacy_router.get("/connections")
def legacy_connections_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/connections", status_code=308)
