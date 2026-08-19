"""Domain-level tests for issue #30's event-backed compliance scope
(app/compliance_scope.py). Router-level/authorization tests live in
tests/test_onboarding_routes.py; migration tests in
tests/test_compliance_scope_migration.py.
"""

from __future__ import annotations

import datetime
import json

import pytest
from sqlalchemy import select

from app.compliance_scope import (
    InvalidComplianceScopeError,
    define_or_revise_scope,
    get_compliance_scope,
    rebuild_compliance_scope_projection,
)
from app.events import DomainEvent
from app.models import ComplianceScope


def test_define_creates_scope_with_security_always_included(app):
    with app.state.session_factory() as session:
        scope = define_or_revise_scope(
            session, service_description="Our SaaS product", trust_service_categories=["availability"]
        )
        session.commit()

        assert scope.service_description == "Our SaaS product"
        assert scope.trust_service_categories == "security,availability"
        assert scope.revision == 1


def test_define_with_no_categories_still_includes_security(app):
    with app.state.session_factory() as session:
        scope = define_or_revise_scope(session, service_description="x")
        session.commit()
        assert scope.trust_service_categories == "security"


def test_unknown_category_is_rejected(app):
    with app.state.session_factory() as session:
        with pytest.raises(InvalidComplianceScopeError):
            define_or_revise_scope(session, trust_service_categories=["not-a-real-category"])


def test_audit_period_end_before_start_is_rejected(app):
    with app.state.session_factory() as session:
        with pytest.raises(InvalidComplianceScopeError):
            define_or_revise_scope(
                session,
                audit_period_starts_on=datetime.date(2026, 6, 1),
                audit_period_ends_on=datetime.date(2026, 1, 1),
            )


def test_second_call_revises_the_same_singleton_row(app):
    with app.state.session_factory() as session:
        first = define_or_revise_scope(session, service_description="v1")
        session.commit()
        first_id = first.id

        second = define_or_revise_scope(session, service_description="v2")
        session.commit()

        assert second.id == first_id
        assert second.revision == 2
        assert second.service_description == "v2"
        assert session.scalar(select(ComplianceScope).limit(1)).service_description == "v2"


def test_revising_does_not_rewrite_the_original_event_payload(app):
    """Historical scope declarations remain reconstructable from the event
    log even after a revision — RULES.md §1.6 (immutable approved/effective
    history)."""
    with app.state.session_factory() as session:
        define_or_revise_scope(session, service_description="original")
        session.commit()
        define_or_revise_scope(session, service_description="revised")
        session.commit()

        events = session.scalars(
            select(DomainEvent)
            .where(DomainEvent.aggregate_type == "compliance_scope")
            .order_by(DomainEvent.aggregate_sequence)
        ).all()
        assert len(events) == 2
        assert events[0].event_type == "ComplianceScopeDefined"
        assert json.loads(events[0].payload_json)["service_description"] == "original"
        assert events[1].event_type == "ComplianceScopeRevised"
        assert json.loads(events[1].payload_json)["service_description"] == "revised"


def test_get_compliance_scope_returns_none_before_any_definition(app):
    with app.state.session_factory() as session:
        assert get_compliance_scope(session) is None


def test_rebuild_projection_reproduces_current_state(app):
    with app.state.session_factory() as session:
        define_or_revise_scope(session, service_description="v1", trust_service_categories=["privacy"])
        session.commit()
        define_or_revise_scope(
            session, service_description="v2", trust_service_categories=["confidentiality"]
        )
        session.commit()
        scope_id = get_compliance_scope(session).id

        rebuild_compliance_scope_projection(session)
        session.commit()

        rebuilt = session.get(ComplianceScope, scope_id)
        assert rebuilt.service_description == "v2"
        assert rebuilt.trust_service_categories == "security,confidentiality"
        assert rebuilt.revision == 2
