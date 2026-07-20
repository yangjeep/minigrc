"""Generic JSON API router factory for a RegisterConfig.

See docs/superpowers/specs/2026-07-20-feature2-register-grid-design.md for
the endpoint contract (list/create/patch/delete/bulk, optimistic
concurrency via expected_updated_at, all-or-nothing bulk updates).
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_login, verify_csrf_header
from app.models import User
from app.registers.config import RegisterConfig

_MISSING = object()


def _iso(dt: datetime.datetime) -> str:
    """Normalize to a UTC-aware ISO string regardless of the DB driver's tz handling.

    SQLite round-trips DateTime columns as naive (see app/deps.py's
    require_login for the same normalization on UserSession.expires_at).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.isoformat()


def _validate(config: RegisterConfig, payload: dict[str, Any], *, partial: bool) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    editable_names = {spec.name for spec in config.fields if not spec.read_only}
    if config.scope_field is not None:
        editable_names = editable_names | {config.scope_field}
    for key in payload:
        if key not in editable_names:
            errors.setdefault(key, []).append("unknown or read-only field")
    for spec in config.fields:
        if spec.read_only:
            continue
        value = payload.get(spec.name, _MISSING)
        if value is _MISSING:
            if not partial and spec.required:
                errors.setdefault(spec.name, []).append("required")
            continue
        if spec.required and (value is None or (isinstance(value, str) and not value.strip())):
            errors.setdefault(spec.name, []).append("required")
        if spec.max_length is not None and isinstance(value, str) and len(value) > spec.max_length:
            errors.setdefault(spec.name, []).append(f"must be at most {spec.max_length} characters")
        if spec.choices is not None and value is not None and value not in spec.choices:
            errors.setdefault(spec.name, []).append(f"must be one of {', '.join(spec.choices)}")
    return errors


def _check_permission(config: RegisterConfig, action: str, user: User) -> None:
    if action in config.require_admin_for and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")


def build_register_router(config: RegisterConfig) -> APIRouter:
    router = APIRouter(prefix=f"/api/registers/{config.name}", tags=[f"register:{config.name}"])

    def serialize(row: Any) -> dict[str, Any]:
        data: dict[str, Any] = {"id": row.id, "updated_at": _iso(row.updated_at)}
        for spec in config.fields:
            data[spec.name] = spec.compute(row) if spec.compute is not None else getattr(row, spec.name)
        return data

    @router.get("")
    def list_rows(
        request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)
    ) -> list[dict[str, Any]]:
        _check_permission(config, "list", user)
        query = select(config.model).order_by(config.order_by)
        if config.scope_field is not None:
            scope_value = request.query_params.get(config.scope_field)
            if not scope_value:
                raise HTTPException(status_code=400, detail=f"{config.scope_field} query param is required")
            query = query.where(getattr(config.model, config.scope_field) == scope_value)
        rows = db.scalars(query).all()
        return [serialize(row) for row in rows]

    if config.creatable:

        @router.post("", status_code=201)
        def create_row(
            payload: dict[str, Any] = Body(...),
            db: Session = Depends(get_db),
            user: User = Depends(require_login),
            _csrf: None = Depends(verify_csrf_header),
        ) -> dict[str, Any]:
            _check_permission(config, "create", user)
            if config.scope_field is not None and not payload.get(config.scope_field):
                raise HTTPException(status_code=422, detail={config.scope_field: ["required"]})
            errors = _validate(config, payload, partial=False)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            if config.create_fn is not None:
                row = config.create_fn(db, payload)
            else:
                row = config.model(
                    **{spec.name: payload.get(spec.name) for spec in config.fields if not spec.read_only}
                )
                db.add(row)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                raise HTTPException(
                    status_code=422, detail={"__all__": ["duplicate or constraint violation"]}
                ) from None
            record_audit_event(
                db,
                entity_type=config.entity_type,
                entity_id=row.id,
                action="create",
                detail=f"Created {config.entity_type} '{row.id}'",
                actor=user.email,
            )
            return serialize(row)

    @router.patch("/{row_id}")
    def update_row(
        row_id: str,
        payload: dict[str, Any] = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(require_login),
        _csrf: None = Depends(verify_csrf_header),
    ) -> dict[str, Any]:
        _check_permission(config, "edit", user)
        row = db.get(config.model, row_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Row not found")
        expected = payload.get("expected_updated_at")
        if expected != _iso(row.updated_at):
            raise HTTPException(status_code=409, detail={"current": serialize(row)})
        fields = payload.get("fields", {})
        errors = _validate(config, fields, partial=True)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        record_audit_event(
            db,
            entity_type=config.entity_type,
            entity_id=row.id,
            action="update",
            detail=f"Updated fields {sorted(fields)} on {config.entity_type} '{row.id}'",
            actor=user.email,
        )
        return serialize(row)

    if config.deletable:

        @router.delete("/{row_id}", status_code=204)
        def delete_row(
            row_id: str,
            db: Session = Depends(get_db),
            user: User = Depends(require_login),
            _csrf: None = Depends(verify_csrf_header),
        ) -> None:
            _check_permission(config, "delete", user)
            row = db.get(config.model, row_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Row not found")
            record_audit_event(
                db,
                entity_type=config.entity_type,
                entity_id=row.id,
                action="delete",
                detail=f"Deleted {config.entity_type} '{row.id}'",
                actor=user.email,
            )
            db.delete(row)

    if config.bulk_enabled:

        @router.post("/bulk")
        def bulk_update(
            payload: dict[str, Any] = Body(...),
            db: Session = Depends(get_db),
            user: User = Depends(require_login),
            _csrf: None = Depends(verify_csrf_header),
        ) -> list[dict[str, Any]]:
            _check_permission(config, "edit", user)
            updates = payload.get("updates", [])
            planned: list[tuple[Any, dict[str, Any]]] = []
            for update in updates:
                row = db.get(config.model, update.get("id"))
                if row is None:
                    raise HTTPException(status_code=404, detail=f"Row {update.get('id')} not found")
                if update.get("expected_updated_at") != _iso(row.updated_at):
                    raise HTTPException(status_code=409, detail={"id": row.id, "current": serialize(row)})
                fields = update.get("fields", {})
                errors = _validate(config, fields, partial=True)
                if errors:
                    raise HTTPException(status_code=422, detail={"id": row.id, "errors": errors})
                planned.append((row, fields))

            for row, fields in planned:
                for key, value in fields.items():
                    setattr(row, key, value)
            db.flush()
            record_audit_event(
                db,
                entity_type=config.entity_type,
                entity_id="bulk",
                action="bulk_update",
                detail=f"Bulk updated {len(planned)} {config.entity_type} rows",
                actor=user.email,
            )
            return [serialize(row) for row, _ in planned]

    return router
