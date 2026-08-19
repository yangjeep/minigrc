"""Domain-level tests for issue #31's event-backed policy-version
approval lifecycle (app/policy_lifecycle.py). Router-level/authorization
tests live in tests/test_policy_lifecycle_routes.py; migration tests in
tests/test_policy_lifecycle_migration.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.events import DomainEvent
from app.models import Policy, PolicyVersion
from app.policy_lifecycle import (
    make_version_effective,
    rebuild_policy_version_lifecycle_projection,
    review_version,
    submit_for_review,
    withdraw_version,
)


def _make_policy_version(session, *, policy=None, version_number=1, **kwargs):
    if policy is None:
        policy = Policy(title="Test Policy")
        session.add(policy)
        session.flush()
    defaults = {
        "policy_id": policy.id,
        "version_number": version_number,
        "original_filename": "policy.pdf",
        "stored_filename": "stored.pdf",
        "media_type": "application/pdf",
        "byte_size": 100,
        "sha256": "a" * 64,
        "uploader": "author@example.com",
    }
    defaults.update(kwargs)
    version = PolicyVersion(**defaults)
    session.add(version)
    session.flush()
    return version


def test_submit_for_review_transitions_from_draft(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_type="user", actor_id="user-1")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "in_review"
        assert version.submitted_by == "user-1"
        assert version.submitted_at is not None


def test_submit_for_review_rejects_non_draft(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_id="user-1")
        session.flush()

        with pytest.raises(ValueError, match="draft"):
            submit_for_review(session, version, actor_id="user-1")


def test_review_approve_transitions_to_approved(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", comment="looks good", actor_id="user-2")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "approved"
        assert version.reviewed_by == "user-2"
        assert version.review_decision == "approved"
        assert version.review_comment == "looks good"


def test_review_reject_returns_to_draft(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="rejected", comment="needs work", actor_id="user-2")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "draft"
        assert version.review_decision == "rejected"
        assert version.review_comment == "needs work"


def test_review_rejects_without_comment(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_id="user-1")
        with pytest.raises(ValueError, match="comment"):
            review_version(session, version, decision="rejected", comment="", actor_id="user-2")


def test_review_rejects_invalid_decision(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_id="user-1")
        with pytest.raises(ValueError, match="Invalid review decision"):
            review_version(session, version, decision="maybe", actor_id="user-2")


def test_review_rejects_from_non_in_review_state(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        with pytest.raises(ValueError, match="in_review"):
            review_version(session, version, decision="approved", actor_id="user-2")


def test_make_effective_requires_approved(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        with pytest.raises(ValueError, match="approved"):
            make_version_effective(session, version, actor_id="user-2")


def test_make_effective_sets_effective_state(app):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", actor_id="user-2")
        make_version_effective(session, version, actor_id="user-2")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "effective"
        assert version.effective_at is not None


def test_make_effective_supersedes_previous_effective_version(app):
    with app.state.session_factory() as session:
        policy = Policy(title="Multi-version Policy")
        session.add(policy)
        session.flush()

        v1 = _make_policy_version(session, policy=policy, version_number=1)
        submit_for_review(session, v1, actor_id="user-1")
        review_version(session, v1, decision="approved", actor_id="user-2")
        make_version_effective(session, v1, actor_id="user-2")
        session.flush()

        v2 = _make_policy_version(session, policy=policy, version_number=2)
        submit_for_review(session, v2, actor_id="user-1")
        review_version(session, v2, decision="approved", actor_id="user-2")
        make_version_effective(session, v2, actor_id="user-2")
        session.commit()

        session.refresh(v1)
        session.refresh(v2)
        assert v2.lifecycle_status == "effective"
        assert v1.lifecycle_status == "superseded"
        assert v1.superseded_by_version_id == v2.id
        assert v1.superseded_at is not None

        # Both aggregates have real events, not just projected columns.
        events = session.scalars(
            select(DomainEvent).where(
                DomainEvent.aggregate_type == "policy_version",
                DomainEvent.aggregate_id.in_([v1.id, v2.id]),
            )
        ).all()
        event_types_by_aggregate: dict[str, list[str]] = {}
        for e in events:
            event_types_by_aggregate.setdefault(e.aggregate_id, []).append(e.event_type)
        assert "PolicyVersionSuperseded" in event_types_by_aggregate[v1.id]
        assert "PolicyVersionMadeEffective" in event_types_by_aggregate[v2.id]


def test_making_first_version_effective_has_no_supersede_side_effect(app):
    """No prior effective version exists — must not fabricate a
    PolicyVersionSuperseded event against anything."""
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        submit_for_review(session, version, actor_id="user-1")
        review_version(session, version, decision="approved", actor_id="user-2")
        make_version_effective(session, version, actor_id="user-2")
        session.commit()

        events = session.scalars(
            select(DomainEvent).where(DomainEvent.event_type == "PolicyVersionSuperseded")
        ).all()
        assert events == []


@pytest.mark.parametrize("lifecycle_status", ["draft", "in_review", "approved", "effective"])
def test_withdraw_valid_from_non_terminal_states(app, lifecycle_status):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        if lifecycle_status in ("in_review", "approved", "effective"):
            submit_for_review(session, version, actor_id="user-1")
        if lifecycle_status in ("approved", "effective"):
            review_version(session, version, decision="approved", actor_id="user-2")
        if lifecycle_status == "effective":
            make_version_effective(session, version, actor_id="user-2")

        withdraw_version(session, version, reason="no longer needed", actor_id="user-3")
        session.commit()

        session.refresh(version)
        assert version.lifecycle_status == "withdrawn"
        assert version.withdrawn_reason == "no longer needed"
        assert version.withdrawn_at is not None


@pytest.mark.parametrize("lifecycle_status", ["superseded", "withdrawn"])
def test_withdraw_rejects_already_terminal_states(app, lifecycle_status):
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        if lifecycle_status == "withdrawn":
            withdraw_version(session, version, actor_id="user-3")
        else:
            policy = session.get(Policy, version.policy_id)
            other = _make_policy_version(session, policy=policy, version_number=2)
            submit_for_review(session, version, actor_id="user-1")
            review_version(session, version, decision="approved", actor_id="user-2")
            make_version_effective(session, version, actor_id="user-2")
            submit_for_review(session, other, actor_id="user-1")
            review_version(session, other, decision="approved", actor_id="user-2")
            make_version_effective(session, other, actor_id="user-2")  # supersedes `version`
        session.flush()

        with pytest.raises(ValueError, match="already"):
            withdraw_version(session, version, actor_id="user-3")


def test_effective_version_immutable_file_facts_survive_lifecycle(app):
    """The whole point of separating file-capture columns from lifecycle
    columns (design doc §2/§4): approving/making effective/superseding a
    version must never touch its bytes/hash/filename/uploader."""
    with app.state.session_factory() as session:
        policy = Policy(title="Immutability Policy")
        session.add(policy)
        session.flush()
        v1 = _make_policy_version(
            session, policy=policy, version_number=1, sha256="b" * 64, uploader="alice@example.com"
        )
        original_sha256, original_uploader, original_filename = (
            v1.sha256,
            v1.uploader,
            v1.original_filename,
        )

        submit_for_review(session, v1, actor_id="user-1")
        review_version(session, v1, decision="approved", actor_id="user-2")
        make_version_effective(session, v1, actor_id="user-2")

        v2 = _make_policy_version(session, policy=policy, version_number=2)
        submit_for_review(session, v2, actor_id="user-1")
        review_version(session, v2, decision="approved", actor_id="user-2")
        make_version_effective(session, v2, actor_id="user-2")  # supersedes v1
        session.commit()

        session.refresh(v1)
        assert v1.lifecycle_status == "superseded"
        assert v1.sha256 == original_sha256
        assert v1.uploader == original_uploader
        assert v1.original_filename == original_filename


def test_policy_effective_version_property(app):
    with app.state.session_factory() as session:
        policy = Policy(title="Effective Version Property Policy")
        session.add(policy)
        session.flush()
        assert policy.effective_version is None

        v1 = _make_policy_version(session, policy=policy, version_number=1)
        submit_for_review(session, v1, actor_id="user-1")
        review_version(session, v1, decision="approved", actor_id="user-2")
        make_version_effective(session, v1, actor_id="user-2")
        session.flush()
        session.refresh(policy)
        assert policy.effective_version.id == v1.id


def test_rebuild_policy_version_lifecycle_projection_reproduces_state(app):
    with app.state.session_factory() as session:
        policy = Policy(title="Rebuild Policy")
        session.add(policy)
        session.flush()

        v1 = _make_policy_version(session, policy=policy, version_number=1)
        submit_for_review(session, v1, actor_id="user-1")
        review_version(session, v1, decision="approved", actor_id="user-2")
        make_version_effective(session, v1, actor_id="user-2")

        v2 = _make_policy_version(session, policy=policy, version_number=2)
        submit_for_review(session, v2, actor_id="user-1")
        review_version(session, v2, decision="approved", actor_id="user-2")
        make_version_effective(session, v2, actor_id="user-2")  # supersedes v1

        v3 = _make_policy_version(session, policy=policy, version_number=3)
        withdraw_version(session, v3, reason="abandoned", actor_id="user-1")
        session.commit()
        # Refresh from the DB before snapshotting "before" — otherwise
        # in-memory tz-aware datetimes (set directly by the projector in
        # this same session) would be compared against SQLite's
        # tz-naive round-tripped values captured after the rebuild,
        # which is a storage-format artifact, not a real difference.
        for version in (v1, v2, v3):
            session.refresh(version)

        before = {
            v.id: (v.lifecycle_status, v.effective_at, v.superseded_at, v.superseded_by_version_id)
            for v in (v1, v2, v3)
        }

        rebuild_policy_version_lifecycle_projection(session)
        session.commit()

        for version in (v1, v2, v3):
            session.refresh(version)
        after = {
            v.id: (v.lifecycle_status, v.effective_at, v.superseded_at, v.superseded_by_version_id)
            for v in (v1, v2, v3)
        }
        assert before == after


def test_rebuild_resets_a_version_with_no_events_to_draft_default(app):
    """A version that never had any lifecycle action still exists (file
    capture is not event-sourced) — rebuild must not delete or corrupt
    it, just leave its lifecycle columns at the default."""
    with app.state.session_factory() as session:
        version = _make_policy_version(session)
        session.commit()
        version_id = version.id

        rebuild_policy_version_lifecycle_projection(session)
        session.commit()

        rebuilt = session.get(PolicyVersion, version_id)
        assert rebuilt is not None
        assert rebuilt.lifecycle_status == "draft"
        assert rebuilt.effective_at is None


def test_db_constraint_rejects_two_effective_versions_for_same_policy(app):
    """Proves uq_policy_version_one_effective_per_policy itself, at the
    schema level — independent of make_version_effective's own ordering
    (see its docstring for why that ordering alone is not the real
    guard, only a best-effort convenience)."""
    with app.state.session_factory() as session:
        policy = Policy(title="Constraint Policy")
        session.add(policy)
        session.flush()
        _make_policy_version(session, policy=policy, version_number=1, lifecycle_status="effective")
        session.commit()

        with pytest.raises(IntegrityError):
            _make_policy_version(session, policy=policy, version_number=2, lifecycle_status="effective")
