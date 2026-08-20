"""Unit/integration tests for issue #20's OIDC identity/role population
evidence capture (app/oidc_identity_evidence.py)."""

from __future__ import annotations

import datetime
import json

from sqlalchemy import select

from app.control_occurrences import link_evidence, perform_occurrence, record_occurrence_manually
from app.models import ControlOccurrenceEvidence, ExternalIdentity, InternalControl, User
from app.oidc_identity_evidence import capture_oidc_identity_population_snapshot
from app.readiness import compute_readiness_queue
from app.security import hash_password

EXPECTED_KEYS = {"issuer", "subject", "user_id", "email", "role", "role_source", "user_status", "linked_at"}


def _make_linked_user(session, *, email, issuer="https://idp.example.test", subject, role="operator"):
    user = User(email=email, password_hash=hash_password("x"), role=role)
    session.add(user)
    session.flush()
    identity = ExternalIdentity(user_id=user.id, issuer=issuer, subject=subject)
    session.add(identity)
    session.flush()
    return user, identity


def test_capture_snapshot_with_no_linked_identities(app):
    with app.state.session_factory() as session:
        snapshot = capture_oidc_identity_population_snapshot(session)
        payload = json.loads(snapshot.normalized_payload_json)

        assert snapshot.source_type == "oidc_identity_population"
        assert payload["population"] == []
        assert "0 " in snapshot.title


def test_capture_snapshot_includes_only_expected_fields(app):
    with app.state.session_factory() as session:
        user, _identity = _make_linked_user(session, email="alice@example.com", subject="sub-alice")
        session.commit()

        snapshot = capture_oidc_identity_population_snapshot(session)
        payload = json.loads(snapshot.normalized_payload_json)

    assert len(payload["population"]) == 1
    entry = payload["population"][0]
    assert set(entry.keys()) == EXPECTED_KEYS
    assert entry["email"] == "alice@example.com"
    assert entry["role"] == "operator"
    assert entry["subject"] == "sub-alice"
    assert entry["issuer"] == "https://idp.example.test"


def test_capture_snapshot_never_includes_secrets_or_tokens(app):
    with app.state.session_factory() as session:
        _make_linked_user(session, email="bob@example.com", subject="sub-bob")
        session.commit()

        snapshot = capture_oidc_identity_population_snapshot(session)

    serialized = snapshot.normalized_payload_json.lower()
    for forbidden in ("password", "token", "secret", "client_secret", "id_token", "access_token"):
        assert forbidden not in serialized


def test_capture_snapshot_hash_is_deterministic_for_same_state(app):
    with app.state.session_factory() as session:
        _make_linked_user(session, email="carol@example.com", subject="sub-carol")
        session.commit()

        first = capture_oidc_identity_population_snapshot(session)
        second = capture_oidc_identity_population_snapshot(session)

    assert first.raw_payload_sha256 == second.raw_payload_sha256
    assert first.normalized_payload_json == second.normalized_payload_json


def test_historical_snapshot_unaffected_by_later_role_change(app):
    """The issue's own required behavior: historical evidence must not
    silently change when current IdP/role state changes."""
    with app.state.session_factory() as session:
        user, _identity = _make_linked_user(
            session, email="dave@example.com", subject="sub-dave", role="reader"
        )
        session.commit()

        snapshot = capture_oidc_identity_population_snapshot(session)
        session.add(snapshot)
        session.commit()
        snapshot_id = snapshot.id
        original_payload = snapshot.normalized_payload_json
        original_hash = snapshot.raw_payload_sha256

        # Role changes after the snapshot was captured.
        user.role = "admin"
        session.commit()

        from app.models import EvidenceSnapshot

        reloaded = session.get(EvidenceSnapshot, snapshot_id)
        assert reloaded.normalized_payload_json == original_payload
        assert reloaded.raw_payload_sha256 == original_hash
        assert '"role": "reader"' in reloaded.normalized_payload_json


def test_snapshot_can_be_linked_to_occurrence_without_marking_it_performed(app):
    with app.state.session_factory() as session:
        _make_linked_user(session, email="erin@example.com", subject="sub-erin")
        control = InternalControl(name="Quarterly Access Review")
        session.add(control)
        session.flush()
        occurrence = record_occurrence_manually(session, control, actor_type="system")
        session.commit()

        snapshot = capture_oidc_identity_population_snapshot(session)
        session.add(snapshot)
        session.flush()
        link_evidence(session, occurrence, snapshot.id, actor_type="user")
        session.commit()

        link = session.scalar(
            select(ControlOccurrenceEvidence).where(ControlOccurrenceEvidence.occurrence_id == occurrence.id)
        )
        assert link is not None
        assert occurrence.performed_at is None  # linking evidence never performs the occurrence


def test_readiness_flags_access_review_performed_without_linked_evidence(app):
    """Proves the design doc's "comes for free" claim rather than
    assuming it: app/readiness.py's existing occurrence_missing_evidence
    check already covers an access-review occurrence that used an
    identity snapshot, with zero new readiness code."""
    with app.state.session_factory() as session:
        control = InternalControl(name="Quarterly Access Review")
        session.add(control)
        session.flush()
        occurrence = record_occurrence_manually(session, control, actor_type="system")
        session.commit()

        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
            performed_by_person_id=None,
            scope_note="",
            evidence_note="",
            actor_type="user",
        )
        session.commit()

        queue = compute_readiness_queue(session)
        assert any(
            item.category == "occurrence_missing_evidence" and item.control_id == control.id for item in queue
        )

        # Now link an identity snapshot as evidence -- the gap disappears.
        snapshot = capture_oidc_identity_population_snapshot(session)
        session.add(snapshot)
        session.flush()
        link_evidence(session, occurrence, snapshot.id, actor_type="user")
        session.commit()

        queue_after = compute_readiness_queue(session)
        assert not any(
            item.category == "occurrence_missing_evidence" and item.control_id == control.id
            for item in queue_after
        )
