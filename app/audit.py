"""Helper for writing AuditEvent rows alongside a mutation."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record_audit_event(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    detail: str = "",
    actor: str = "system",
) -> AuditEvent:
    event = AuditEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        detail=detail,
        actor=actor,
    )
    session.add(event)
    return event
