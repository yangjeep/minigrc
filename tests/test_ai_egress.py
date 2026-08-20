"""Tests for issue #49's AI data-egress boundary (app/ai_egress.py).

These tests exist ahead of #15 (no BYOK provider/prompt-assembly code
exists yet — see the design doc's repository-reality check), so they
prove the boundary module itself: what a representative "summarize
evidence"/pre-fill call would and would not be able to send, and that
the audit trail of what was sent is both accurate and credential-free.
"""

from __future__ import annotations

import datetime
import json

import pytest

from app.ai_egress import (
    AiEgressOptIn,
    project_control_occurrence,
    project_evidence_artifact,
    project_finding,
    project_policy,
    record_ai_egress,
)
from app.control_occurrences import generate_occurrences, perform_occurrence
from app.models import (
    AuditEvent,
    EvidenceArtifact,
    EvidenceArtifactVersion,
    Finding,
    InternalControl,
    Person,
    Policy,
)

SENSITIVE_DESCRIPTION = "Confirmed with jane.doe@example.com over Slack; her employee id is EMP-4471."
CREDENTIAL_LIKE_SECRET = "sk-abcdefghijklmnopqrstuvwx1234567890"


def _make_control(session, **kwargs) -> InternalControl:
    defaults = {"name": "Access Review", "status": "implemented", "review_frequency": "annual"}
    defaults.update(kwargs)
    control = InternalControl(**defaults)
    session.add(control)
    session.flush()
    return control


def _make_evidence_artifact(session, *, description: str = "") -> EvidenceArtifact:
    artifact = EvidenceArtifact(title="Q3 Access Review Export", description=description)
    session.add(artifact)
    session.flush()
    version = EvidenceArtifactVersion(
        id="ev-" + artifact.id[:10],
        artifact_id=artifact.id,
        version_number=1,
        object_key="evidence/secret-internal-path/file.csv",
        original_filename="jane.doe-ssn-export.csv",
        media_type="text/csv",
        byte_size=2048,
        sha256="a" * 64,
        source_type="manual",
        source_object_id="drive-object-123",
        captured_at=datetime.datetime(2026, 1, 1),
        actor_type="user",
    )
    session.add(version)
    session.flush()
    return artifact


class TestEvidenceArtifactProjection:
    def test_default_projection_is_metadata_only(self, app):
        with app.state.session_factory() as session:
            artifact = _make_evidence_artifact(session, description=SENSITIVE_DESCRIPTION)
            context = project_evidence_artifact(artifact)

            assert context.entity_type == "evidence_artifact"
            assert context.included_free_text is False
            assert context.fields == {
                "title": "Q3 Access Review Export",
                "version_count": 1,
                "latest_version_number": 1,
                "media_type": "text/csv",
                "byte_size": 2048,
                "sha256": "a" * 64,
                "captured_at": "2026-01-01T00:00:00",
                "source_type": "manual",
            }
            # Tier 3 (never, by design): raw storage path and filename can
            # themselves carry sensitive/PII-shaped content and are never
            # projected by this module at all, opt-in or not.
            assert "object_key" not in context.fields
            assert "original_filename" not in context.fields
            assert "source_object_id" not in context.fields
            assert "description" not in context.fields

    def test_opt_in_adds_description_only(self, app):
        with app.state.session_factory() as session:
            artifact = _make_evidence_artifact(session, description=SENSITIVE_DESCRIPTION)
            opt_in = AiEgressOptIn(actor="ciso@example.com", reason="Drafting an evidence summary for PBC.")
            context = project_evidence_artifact(artifact, opt_in=opt_in)

            assert context.included_free_text is True
            assert context.fields["description"] == SENSITIVE_DESCRIPTION
            # Still never the raw file / storage path, even with opt-in.
            assert "object_key" not in context.fields
            assert "original_filename" not in context.fields


class TestControlOccurrenceProjection:
    def _make_occurrence(self, session, *, with_owner: bool = True):
        owner = None
        if with_owner:
            owner = Person(email="owner@example.com", display_name="Owner Person")
            session.add(owner)
            session.flush()
        control = _make_control(
            session,
            cadence_type="calendar",
            cadence_interval_months=12,
            owner_person_id=owner.id if owner else None,
        )
        session.commit()
        # generate_occurrences (not a direct row write) is the only
        # legitimate way to populate responsible_person_id — it snapshots
        # control.owner_person_id at generation time (issue #11).
        occurrence = generate_occurrences(session, control)[0]
        session.commit()
        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 1, 2),
            performed_by_person_id=owner.id if owner else None,
            scope_note="Reviewed access list for Q3.",
            evidence_note="Attached export; contact jane.doe@example.com for the raw log.",
        )
        session.commit()
        session.refresh(occurrence)
        return occurrence

    def test_default_excludes_notes_and_person_identity(self, app):
        with app.state.session_factory() as session:
            occurrence = self._make_occurrence(session)
            context = project_control_occurrence(occurrence)

            assert context.included_free_text is False
            assert context.fields["control_name"] == "Access Review"
            assert context.fields["has_responsible_owner"] is True
            assert "scope_note" not in context.fields
            assert "evidence_note" not in context.fields
            _assert_no_pii_leak(context.fields, email="owner@example.com", display_name="Owner Person")

    def test_opt_in_adds_notes_but_never_person_identity(self, app):
        with app.state.session_factory() as session:
            occurrence = self._make_occurrence(session)
            opt_in = AiEgressOptIn(actor="ciso@example.com", reason="Drafting a control-operation summary.")
            context = project_control_occurrence(occurrence, opt_in=opt_in)

            assert context.included_free_text is True
            assert context.fields["scope_note"] == "Reviewed access list for Q3."
            assert "jane.doe@example.com" in context.fields["evidence_note"]  # human-authored text, opted-in
            _assert_no_pii_leak(context.fields, email="owner@example.com", display_name="Owner Person")

    def test_no_owner_reports_false_not_none_leak(self, app):
        with app.state.session_factory() as session:
            occurrence = self._make_occurrence(session, with_owner=False)
            context = project_control_occurrence(occurrence)
            assert context.fields["has_responsible_owner"] is False


class TestFindingProjection:
    def _make_finding(self, session) -> Finding:
        owner = Person(email="finding-owner@example.com", display_name="Finding Owner")
        session.add(owner)
        session.flush()
        finding = Finding(
            id="fnd-test-0001",
            title="MFA not enforced for one contractor",
            description=SENSITIVE_DESCRIPTION,
            severity="high",
            status="open",
            owner_person_id=owner.id,
            due_date=datetime.date(2026, 3, 1),
            closure_note="",
        )
        session.add(finding)
        session.commit()
        return finding

    def test_default_excludes_description_and_owner_identity(self, app):
        with app.state.session_factory() as session:
            finding = self._make_finding(session)
            context = project_finding(finding)

            assert context.fields == {
                "title": "MFA not enforced for one contractor",
                "severity": "high",
                "status": "open",
                "due_date": "2026-03-01",
                "has_owner": True,
            }
            _assert_no_pii_leak(
                context.fields, email="finding-owner@example.com", display_name="Finding Owner"
            )

    def test_opt_in_adds_description_and_closure_note(self, app):
        with app.state.session_factory() as session:
            finding = self._make_finding(session)
            opt_in = AiEgressOptIn(actor="ciso@example.com", reason="Drafting a management response.")
            context = project_finding(finding, opt_in=opt_in)
            assert context.fields["description"] == SENSITIVE_DESCRIPTION
            assert context.fields["closure_note"] == ""
            _assert_no_pii_leak(
                context.fields, email="finding-owner@example.com", display_name="Finding Owner"
            )


class TestPolicyProjection:
    def test_default_excludes_description_and_owner_name(self, app):
        with app.state.session_factory() as session:
            policy = Policy(
                title="Access Control Policy",
                description=SENSITIVE_DESCRIPTION,
                owner="Jane Doe",
                status="effective",
                effective_date=datetime.date(2026, 1, 1),
                next_review_date=datetime.date(2027, 1, 1),
            )
            session.add(policy)
            session.commit()

            context = project_policy(policy)
            assert context.fields == {
                "title": "Access Control Policy",
                "status": "effective",
                "effective_date": "2026-01-01",
                "next_review_date": "2027-01-01",
                "has_named_owner": True,
            }
            # Policy.owner is a bare name string identifying a person —
            # Tier 3, never projected even with opt-in (see below).

    def test_opt_in_adds_description_but_never_owner_name(self, app):
        with app.state.session_factory() as session:
            policy = Policy(
                title="Access Control Policy", description=SENSITIVE_DESCRIPTION, owner="Jane Doe"
            )
            session.add(policy)
            session.commit()

            opt_in = AiEgressOptIn(actor="ciso@example.com", reason="Drafting a policy change summary.")
            context = project_policy(policy, opt_in=opt_in)
            assert context.fields["description"] == SENSITIVE_DESCRIPTION
            assert "owner" not in context.fields
            assert "Jane Doe" not in json.dumps(dict(context.fields))


class TestOptInRequiresDeliberateReason:
    @pytest.mark.parametrize("reason", ["", "   "])
    def test_blank_reason_rejected(self, reason):
        with pytest.raises(ValueError):
            AiEgressOptIn(actor="ciso@example.com", reason=reason)

    def test_missing_actor_type_error(self):
        with pytest.raises(TypeError):
            AiEgressOptIn(reason="A real reason")  # actor is required, no silent default


class TestRecordAiEgress:
    def test_persisted_detail_reproduces_exactly_what_was_sent(self, app):
        with app.state.session_factory() as session:
            artifact = _make_evidence_artifact(session)
            context = project_evidence_artifact(artifact)
            event = record_ai_egress(
                session,
                context,
                task_type="summarize_evidence",
                provider_name="stub-openai-compatible",
                model="stub-model-1",
                actor="ciso@example.com",
            )
            session.commit()

            stored = session.get(AuditEvent, event.id)
            assert stored.entity_type == "ai_egress:evidence_artifact"
            assert stored.entity_id == artifact.id
            assert stored.action == "prompt_sent"
            assert stored.actor == "ciso@example.com"

            payload = json.loads(stored.detail)
            assert payload["task_type"] == "summarize_evidence"
            assert payload["provider_name"] == "stub-openai-compatible"
            assert payload["model"] == "stub-model-1"
            assert payload["included_free_text"] is False
            assert payload["fields_sent"] == context.fields

    def test_default_vs_opt_in_calls_are_distinguishable_after_the_fact(self, app):
        with app.state.session_factory() as session:
            artifact = _make_evidence_artifact(session, description=SENSITIVE_DESCRIPTION)

            default_context = project_evidence_artifact(artifact)
            default_event = record_ai_egress(session, default_context, task_type="summarize_evidence")

            opt_in = AiEgressOptIn(actor="ciso@example.com", reason="Include full narrative for PBC.")
            opted_in_context = project_evidence_artifact(artifact, opt_in=opt_in)
            opted_in_event = record_ai_egress(
                session, opted_in_context, task_type="summarize_evidence", actor=opt_in.actor
            )
            session.commit()

            default_payload = json.loads(session.get(AuditEvent, default_event.id).detail)
            opted_in_payload = json.loads(session.get(AuditEvent, opted_in_event.id).detail)

            assert default_payload["included_free_text"] is False
            assert "description" not in default_payload["fields_sent"]
            assert opted_in_payload["included_free_text"] is True
            assert opted_in_payload["fields_sent"]["description"] == SENSITIVE_DESCRIPTION

    def test_credential_like_content_is_scrubbed_from_the_log(self, app):
        """Defense-in-depth: even if a human pastes something credential-shaped
        into a free-text field that was legitimately opted in, the persisted
        audit record does not retain it verbatim. This does not rely on the
        allowlists (which never include a real secret-shaped column) — it is
        a belt-and-suspenders check on the logging path itself."""
        with app.state.session_factory() as session:
            artifact = _make_evidence_artifact(
                session, description=f"Rotated the key: {CREDENTIAL_LIKE_SECRET}"
            )
            opt_in = AiEgressOptIn(actor="ciso@example.com", reason="Summarize rotation evidence.")
            context = project_evidence_artifact(artifact, opt_in=opt_in)
            event = record_ai_egress(session, context, task_type="summarize_evidence")
            session.commit()

            stored_detail = session.get(AuditEvent, event.id).detail
            assert CREDENTIAL_LIKE_SECRET not in stored_detail
            assert "[redacted-credential-like-value]" in stored_detail

    def test_record_ai_egress_has_no_credential_parameter(self):
        """Structural guarantee: this function cannot leak a provider
        credential because its signature has no way to receive one."""
        import inspect

        params = set(inspect.signature(record_ai_egress).parameters)
        assert not any("key" in p or "secret" in p or "token" in p or "credential" in p for p in params)


class TestModuleHasNoPersonProjector:
    def test_no_project_person_function_exists(self):
        import app.ai_egress as ai_egress_module

        assert not hasattr(ai_egress_module, "project_person")


class TestPromptContextFieldsAreImmutable:
    def test_fields_cannot_be_widened_after_projection(self, app):
        """#15's future prompt-assembly code must go through a project_*
        allowlist to send anything — it cannot just add a key to an
        already-built context to smuggle more data out."""
        with app.state.session_factory() as session:
            artifact = _make_evidence_artifact(session)
            context = project_evidence_artifact(artifact)

            with pytest.raises(TypeError):
                context.fields["object_key"] = "evidence/leaked/path"


def _assert_no_pii_leak(fields, *, email: str, display_name: str) -> None:
    serialized = json.dumps(dict(fields))
    assert email not in serialized
    assert display_name not in serialized
