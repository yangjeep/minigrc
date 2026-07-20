"""External database connection administration.

Admin-only end to end (list, create, edit, delete, test) — this is a
credential-adjacent surface even though the credential itself is never
returned to the browser, matching the Google Drive/AWS connector
precedent (see docs/decisions/architectural-decisions.md #12).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.connections import run_connection_test
from app.deps import get_db, require_admin, verify_csrf
from app.flash import redirect_with_flash
from app.models import (
    CONNECTION_DB_TYPES,
    CONNECTION_TLS_MODES,
    ExternalConnection,
)
from app.registers.config import FieldSpec, RegisterConfig
from app.registers.router import build_register_router
from app.secrets import create_encrypted_secret

router = APIRouter(prefix="/connections", tags=["connections"], dependencies=[Depends(require_admin)])

CONNECTIONS_REGISTER_CONFIG = RegisterConfig(
    name="connections",
    model=ExternalConnection,
    entity_type="external_connection",
    order_by=ExternalConnection.name,
    creatable=False,
    deletable=False,
    bulk_enabled=False,
    fields=(
        FieldSpec(name="name", type="text", read_only=True),
        FieldSpec(name="db_type", type="text", read_only=True),
        FieldSpec(name="host", type="text", read_only=True),
        FieldSpec(name="database_name", type="text", read_only=True),
        FieldSpec(name="enabled", type="bool", read_only=True),
        FieldSpec(name="owner", type="text", read_only=True),
        FieldSpec(name="last_test_status", type="text", read_only=True),
        FieldSpec(
            name="last_tested_at",
            type="text",
            read_only=True,
            compute=lambda c: c.last_tested_at.isoformat() if c.last_tested_at else None,
        ),
    ),
)

connections_register_router = build_register_router(CONNECTIONS_REGISTER_CONFIG)


@router.get("")
def list_connections(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "connections/list.html", {})


@router.get("/new")
def new_connection_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "connections/form.html",
        {"connection": None, "db_types": CONNECTION_DB_TYPES, "tls_modes": CONNECTION_TLS_MODES},
    )


@router.post("")
def create_connection(
    request: Request,
    name: str = Form(...),
    db_type: str = Form(...),
    host: str = Form(""),
    port: str = Form(""),
    database_name: str = Form(""),
    sqlite_path: str = Form(""),
    username: str = Form(""),
    tls_mode: str = Form("prefer"),
    owner: str = Form(""),
    enabled: bool = Form(False),
    secret_value: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    name = name.strip()
    if not name:
        return redirect_with_flash("/connections/new", "Name is required.", kind="error")
    if db_type not in CONNECTION_DB_TYPES:
        return redirect_with_flash("/connections/new", "Invalid database type.", kind="error")
    if tls_mode not in CONNECTION_TLS_MODES:
        return redirect_with_flash("/connections/new", "Invalid TLS mode.", kind="error")

    settings = request.app.state.settings
    secret_id = None
    if secret_value.strip():
        secret = create_encrypted_secret(
            db,
            name=f"connection:{name}",
            plaintext=secret_value,
            actor=request.state.user.email,
            key=settings.encryption_key,
        )
        db.flush()
        secret_id = secret.id

    conn = ExternalConnection(
        name=name,
        db_type=db_type,
        host=host.strip() or None,
        port=int(port) if port.strip() else None,
        database_name=database_name.strip() or None,
        sqlite_path=sqlite_path.strip() or None,
        username=username.strip() or None,
        secret_id=secret_id,
        tls_mode=tls_mode,
        enabled=enabled,
        owner=owner.strip(),
        created_by=request.state.user.email,
    )
    db.add(conn)
    db.flush()
    record_audit_event(
        db,
        entity_type="external_connection",
        entity_id=conn.id,
        action="create",
        detail=f"Created connection '{conn.name}' ({conn.db_type})",
        actor=request.state.user.email,
    )
    return redirect_with_flash("/connections", "Connection created.")


@router.get("/{connection_id}/edit")
def edit_connection_form(connection_id: str, request: Request, db: Session = Depends(get_db)):
    conn = db.get(ExternalConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "connections/form.html",
        {"connection": conn, "db_types": CONNECTION_DB_TYPES, "tls_modes": CONNECTION_TLS_MODES},
    )


@router.post("/{connection_id}/edit")
def update_connection(
    connection_id: str,
    request: Request,
    name: str = Form(...),
    db_type: str = Form(...),
    host: str = Form(""),
    port: str = Form(""),
    database_name: str = Form(""),
    sqlite_path: str = Form(""),
    username: str = Form(""),
    tls_mode: str = Form("prefer"),
    owner: str = Form(""),
    enabled: bool = Form(False),
    secret_value: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    conn = db.get(ExternalConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    name = name.strip()
    if not name:
        return redirect_with_flash(f"/connections/{connection_id}/edit", "Name is required.", kind="error")
    if db_type not in CONNECTION_DB_TYPES or tls_mode not in CONNECTION_TLS_MODES:
        return redirect_with_flash(f"/connections/{connection_id}/edit", "Invalid value.", kind="error")

    settings = request.app.state.settings
    if secret_value.strip():
        secret = create_encrypted_secret(
            db,
            name=f"connection:{name}:{conn.id}:{conn.updated_at.timestamp()}",
            plaintext=secret_value,
            actor=request.state.user.email,
            key=settings.encryption_key,
        )
        db.flush()
        conn.secret_id = secret.id

    conn.name = name
    conn.db_type = db_type
    conn.host = host.strip() or None
    conn.port = int(port) if port.strip() else None
    conn.database_name = database_name.strip() or None
    conn.sqlite_path = sqlite_path.strip() or None
    conn.username = username.strip() or None
    conn.tls_mode = tls_mode
    conn.enabled = enabled
    conn.owner = owner.strip()
    record_audit_event(
        db,
        entity_type="external_connection",
        entity_id=conn.id,
        action="update",
        detail=f"Updated connection '{conn.name}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash("/connections", "Connection updated.")


@router.post("/{connection_id}/delete")
def delete_connection(
    connection_id: str, request: Request, db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)
):
    conn = db.get(ExternalConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    record_audit_event(
        db,
        entity_type="external_connection",
        entity_id=conn.id,
        action="delete",
        detail=f"Deleted connection '{conn.name}'",
        actor=request.state.user.email,
    )
    db.delete(conn)
    return redirect_with_flash("/connections", "Connection deleted.")


@router.post("/{connection_id}/test")
def test_connection_route(
    connection_id: str, request: Request, db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)
):
    conn = db.get(ExternalConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    settings = request.app.state.settings
    result = run_connection_test(db, conn, key=settings.encryption_key, actor=request.state.user.email)
    kind = "success" if result.status == "success" else "error"
    return redirect_with_flash("/connections", f"Test {result.status}: {result.message}", kind=kind)
