"""Tests for the Trust Center domain/service layer (Feature 11).

See the umbrella issue #5 architecture checkpoint and the "Kubernetes"
checkpoint comment on PR #6 for what preceded this feature.
"""

from __future__ import annotations

import datetime

from app.models import AuditEvent, TrustCenterSection, TrustCenterSettings, User
from app.security import hash_password
from app.trust_center import get_or_create_settings, is_stale, publish_section, unpublish_section


def _make_user(session, email="publisher@example.com"):
    user = User(email=email, password_hash=hash_password("correct horse battery staple"))
    session.add(user)
    session.flush()
    return user


def test_get_or_create_settings_creates_exactly_one_row(app):
    with app.state.session_factory() as session:
        first = get_or_create_settings(session)
        session.flush()
        second = get_or_create_settings(session)
        session.commit()
        assert first.id == second.id
        assert session.query(TrustCenterSettings).count() == 1


def test_publish_section_snapshots_draft_and_stamps_metadata(app):
    with app.state.session_factory() as session:
        user = _make_user(session)
        section = TrustCenterSection(title="Security overview", draft_body_markdown="# Hello")
        session.add(section)
        session.flush()

        publish_section(session, section, actor=user)
        session.commit()

        assert section.status == "published"
        assert section.published_body_markdown == "# Hello"
        assert section.published_at is not None
        assert section.published_by_user_id == user.id


def test_publish_section_records_audit_event(app):
    with app.state.session_factory() as session:
        user = _make_user(session)
        section = TrustCenterSection(title="Security overview", draft_body_markdown="# Hello")
        session.add(section)
        session.flush()

        publish_section(session, section, actor=user)
        session.commit()

        events = session.query(AuditEvent).filter_by(entity_type="trust_center_section").all()
        assert any(e.action == "publish" and e.entity_id == section.id for e in events)


def test_unpublish_keeps_last_published_snapshot(app):
    with app.state.session_factory() as session:
        user = _make_user(session)
        section = TrustCenterSection(title="Security overview", draft_body_markdown="# Hello")
        session.add(section)
        session.flush()
        publish_section(session, section, actor=user)
        session.flush()

        unpublish_section(session, section, actor=user)
        session.commit()

        assert section.status == "draft"
        assert section.published_body_markdown == "# Hello"

        events = session.query(AuditEvent).filter_by(entity_type="trust_center_section").all()
        assert any(e.action == "unpublish" and e.entity_id == section.id for e in events)


def test_draft_section_is_never_stale():
    section = TrustCenterSection(title="x", status="draft", expiry_date=datetime.date(2000, 1, 1))
    assert is_stale(section, today=datetime.date(2026, 1, 1)) is False


def test_published_section_stale_when_expiry_date_passed():
    section = TrustCenterSection(title="x", status="published", expiry_date=datetime.date(2026, 1, 1))
    assert is_stale(section, today=datetime.date(2026, 6, 1)) is True


def test_published_section_stale_when_review_date_passed():
    section = TrustCenterSection(title="x", status="published", review_date=datetime.date(2026, 1, 1))
    assert is_stale(section, today=datetime.date(2026, 6, 1)) is True


def test_published_section_not_stale_before_dates():
    section = TrustCenterSection(
        title="x",
        status="published",
        review_date=datetime.date(2027, 1, 1),
        expiry_date=datetime.date(2027, 1, 1),
    )
    assert is_stale(section, today=datetime.date(2026, 6, 1)) is False
