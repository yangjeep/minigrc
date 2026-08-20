"""Unit tests for issue #45's history/point-in-time reconstruction module
(app/history.py).

`reconstruct_policy_as_of` is exercised against a known, explicitly
constructed sequence of DomainEvent rows (via app.events.append_and_project
directly, with explicit occurred_at timestamps) rather than through
app.policy_lifecycle's own command functions, which don't expose an
occurred_at override for every transition — this test's job is to prove
the pure in-memory replay is correct against a known timeline, not to
re-test policy_lifecycle's own command validation (already covered by
tests/test_policy_lifecycle.py).
"""

from __future__ import annotations

import datetime

from app.events import append_and_project
from app.history import get_event_history, reconstruct_policy_as_of
from app.models import Policy, PolicyVersion
from app.policy_lifecycle import POLICY_VERSION_PROJECTORS

T0 = datetime.datetime(2026, 1, 1)
T1 = datetime.datetime(2026, 1, 5)
T2 = datetime.datetime(2026, 1, 10)
T3 = datetime.datetime(2026, 2, 1)
T4 = datetime.datetime(2026, 2, 5)
T5 = datetime.datetime(2026, 2, 10)


def _make_version(session, policy, *, version_number, created_at):
    version = PolicyVersion(
        id=f"pv{version_number}" + "0" * 28,
        policy_id=policy.id,
        version_number=version_number,
        original_filename="p.pdf",
        stored_filename=f"s{version_number}.pdf",
        media_type="application/pdf",
        byte_size=1,
        sha256="a" * 64,
        uploader="admin@example.com",
        created_at=created_at,
    )
    session.add(version)
    session.flush()
    return version


def _append(session, version, event_type, payload, *, occurred_at, actor_id=None):
    return append_and_project(
        session,
        POLICY_VERSION_PROJECTORS,
        aggregate_type="policy_version",
        aggregate_id=version.id,
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at,
        actor_type="user",
        actor_id=actor_id,
    )


def test_get_event_history_lists_events_in_chronological_order(app):
    with app.state.session_factory() as session:
        policy = Policy(title="Access Control Policy")
        session.add(policy)
        session.flush()
        v1 = _make_version(session, policy, version_number=1, created_at=T0)

        _append(session, v1, "PolicyVersionSubmittedForReview", {}, occurred_at=T1)
        _append(session, v1, "PolicyVersionReviewed", {"decision": "approved"}, occurred_at=T2)
        _append(session, v1, "PolicyVersionMadeEffective", {}, occurred_at=T3)
        session.commit()

        history = get_event_history(session, aggregate_type="policy_version", aggregate_id=v1.id)

    assert [e.event_type for e in history] == [
        "PolicyVersionSubmittedForReview",
        "PolicyVersionReviewed",
        "PolicyVersionMadeEffective",
    ]
    assert [e.occurred_at for e in history] == [T1, T2, T3]


def test_reconstruct_policy_as_of_before_any_event_shows_draft(app):
    with app.state.session_factory() as session:
        policy = Policy(title="Access Control Policy")
        session.add(policy)
        session.flush()
        v1 = _make_version(session, policy, version_number=1, created_at=T0)
        _append(session, v1, "PolicyVersionSubmittedForReview", {}, occurred_at=T1)
        session.commit()

        snapshots = reconstruct_policy_as_of(session, policy, as_of=datetime.datetime(2026, 1, 2))

    assert len(snapshots) == 1
    assert snapshots[0].lifecycle_status == "draft"
    assert snapshots[0].submitted_at is None


def test_reconstruct_policy_as_of_matches_state_between_two_events(app):
    """The issue's own required verification: reconstruct state at a
    timestamp strictly between two known events and confirm it matches
    what was actually true at that moment — not before, not after."""
    with app.state.session_factory() as session:
        policy = Policy(title="Access Control Policy")
        session.add(policy)
        session.flush()
        v1 = _make_version(session, policy, version_number=1, created_at=T0)

        _append(session, v1, "PolicyVersionSubmittedForReview", {}, occurred_at=T1)
        _append(session, v1, "PolicyVersionReviewed", {"decision": "approved"}, occurred_at=T2)
        _append(session, v1, "PolicyVersionMadeEffective", {}, occurred_at=T3)
        session.commit()

        # Between "reviewed/approved" (T2) and "made effective" (T3).
        as_of = datetime.datetime(2026, 1, 20)
        snapshots = reconstruct_policy_as_of(session, policy, as_of=as_of)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.lifecycle_status == "approved"
    assert snapshot.reviewed_at == T2
    assert snapshot.effective_at is None  # not yet effective as of this timestamp


def test_reconstruct_policy_as_of_reflects_supersession_across_versions(app):
    """Cross-version reconstruction: v1 effective, then v2 supersedes it.
    "As of" a date between those two facts must show v1 still effective,
    and a version created after the cutoff must not appear at all."""
    with app.state.session_factory() as session:
        policy = Policy(title="Access Control Policy")
        session.add(policy)
        session.flush()
        v1 = _make_version(session, policy, version_number=1, created_at=T0)
        _append(session, v1, "PolicyVersionSubmittedForReview", {}, occurred_at=T1)
        _append(session, v1, "PolicyVersionReviewed", {"decision": "approved"}, occurred_at=T2)
        _append(session, v1, "PolicyVersionMadeEffective", {}, occurred_at=T3)
        session.commit()

        # v2 doesn't exist yet as of a date between T3 (v1 made
        # effective) and T4 (v2 created).
        as_of_before_v2 = datetime.datetime(2026, 2, 3)
        snapshots_before = reconstruct_policy_as_of(session, policy, as_of=as_of_before_v2)
        assert len(snapshots_before) == 1
        assert snapshots_before[0].lifecycle_status == "effective"

        v2 = _make_version(session, policy, version_number=2, created_at=T4)
        _append(session, v2, "PolicyVersionSubmittedForReview", {}, occurred_at=T4)
        _append(session, v2, "PolicyVersionReviewed", {"decision": "approved"}, occurred_at=T4)
        _append(session, v1, "PolicyVersionSuperseded", {"superseded_by_version_id": v2.id}, occurred_at=T5)
        _append(session, v2, "PolicyVersionMadeEffective", {}, occurred_at=T5)
        session.commit()

        # As of a date between v1's original effective date (T3) and the
        # supersession (T5): v1 is still effective, v2 exists (created at
        # T4) but isn't effective yet.
        as_of_mid = datetime.datetime(2026, 2, 7)
        mid_snapshots = {
            s.version_number: s for s in reconstruct_policy_as_of(session, policy, as_of=as_of_mid)
        }
        assert mid_snapshots[1].lifecycle_status == "effective"
        assert mid_snapshots[2].lifecycle_status == "approved"

        # As of "now" (after everything): v1 superseded, v2 effective.
        final_snapshots = {s.version_number: s for s in reconstruct_policy_as_of(session, policy, as_of=T5)}
        assert final_snapshots[1].lifecycle_status == "superseded"
        assert final_snapshots[1].superseded_by_version_id == v2.id
        assert final_snapshots[2].lifecycle_status == "effective"


def test_reconstruct_policy_as_of_never_writes_to_the_real_projection(app):
    """Reconstruction must be read-only — the live PolicyVersion row's
    own lifecycle_status must be completely unaffected by any as-of
    query, regardless of the cutoff used."""
    with app.state.session_factory() as session:
        policy = Policy(title="Access Control Policy")
        session.add(policy)
        session.flush()
        v1 = _make_version(session, policy, version_number=1, created_at=T0)
        _append(session, v1, "PolicyVersionSubmittedForReview", {}, occurred_at=T1)
        _append(session, v1, "PolicyVersionReviewed", {"decision": "approved"}, occurred_at=T2)
        _append(session, v1, "PolicyVersionMadeEffective", {}, occurred_at=T3)
        session.commit()

        real_status_before = v1.lifecycle_status
        assert real_status_before == "effective"

        # Reconstructing as of a much earlier date must not touch the
        # real row at all.
        reconstruct_policy_as_of(session, policy, as_of=T0)
        session.refresh(v1)

        assert v1.lifecycle_status == real_status_before == "effective"
