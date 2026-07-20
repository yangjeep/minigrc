"""Trust Center domain service: settings singleton, publish lifecycle, staleness.

Kept separate from app/routers/trust_center.py so publication rules
(what "stale" means, what publishing/unpublishing actually mutates) are
testable without going through HTTP — see tests/test_trust_center.py.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import TrustCenterSection, TrustCenterSettings, User, utcnow


def get_or_create_settings(session: Session) -> TrustCenterSettings:
    settings = session.scalar(select(TrustCenterSettings).limit(1))
    if settings is None:
        settings = TrustCenterSettings()
        session.add(settings)
        session.flush()
    return settings


def publish_section(session: Session, section: TrustCenterSection, *, actor: User) -> TrustCenterSection:
    section.published_body_markdown = section.draft_body_markdown
    section.published_at = utcnow()
    section.published_by_user_id = actor.id
    section.status = "published"
    session.flush()
    record_audit_event(
        session,
        entity_type="trust_center_section",
        entity_id=section.id,
        action="publish",
        detail=f"Published Trust Center section '{section.title}'",
        actor=actor.email,
    )
    return section


def unpublish_section(session: Session, section: TrustCenterSection, *, actor: User) -> TrustCenterSection:
    section.status = "draft"
    session.flush()
    record_audit_event(
        session,
        entity_type="trust_center_section",
        entity_id=section.id,
        action="unpublish",
        detail=f"Unpublished Trust Center section '{section.title}'",
        actor=actor.email,
    )
    return section


def is_stale(section: TrustCenterSection, *, today: datetime.date | None = None) -> bool:
    if section.status != "published":
        return False
    today = today if today is not None else datetime.date.today()
    if section.expiry_date is not None and section.expiry_date <= today:
        return True
    if section.review_date is not None and section.review_date <= today:
        return True
    return False
