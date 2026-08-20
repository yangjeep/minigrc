"""Unit tests for issue #15's Digital Compliance TPM task layer
(app/digital_tpm.py): Observe, Nag, Pre-fill.

`call_chat_completion` is monkeypatched at the `app.digital_tpm` import
site — these tests are about digital_tpm's own branching (fallback
selection, cooldown/escalation, egress recording, entity dispatch), not
the HTTP layer (already covered by tests/test_ai_provider_client.py).
"""

from __future__ import annotations

import datetime

import pytest
from cryptography.fernet import Fernet

from app.ai_provider_client import ProviderCallResult
from app.control_occurrences import generate_occurrences, perform_occurrence
from app.digital_tpm import (
    MAX_ESCALATION_LEVEL,
    REMINDER_COOLDOWN,
    _get_or_create_reminder_state,
    draft_prefill,
    generate_due_reminders,
    generate_observe_narrative,
    observe_program_summary,
)
from app.models import (
    AiProviderSettings,
    AiReminderState,
    EvidenceArtifact,
    Finding,
    InternalControl,
    Policy,
)
from app.readiness import ReadinessItem

TEST_KEY = Fernet.generate_key().decode()


def _enable_usable_provider(session) -> None:
    session.add(
        AiProviderSettings(
            enabled=True,
            display_name="Test Provider",
            base_url="https://fake-provider.test/v1",
            model="test-model",
            secret_id=None,
            updated_by="admin@example.com",
        )
    )
    session.commit()


_finding_counter = iter(range(1, 100_000))


def _make_finding(session, **overrides) -> Finding:
    defaults = dict(
        id=f"fnd-test-{next(_finding_counter):05d}",
        title="MFA not enforced for one contractor",
        description="",
        severity="high",
        status="open",
        due_date=datetime.date(2026, 3, 1),
        closure_note="",
    )
    defaults.update(overrides)
    finding = Finding(**defaults)
    session.add(finding)
    session.commit()
    return finding


def _make_evidence_artifact(session, **overrides) -> EvidenceArtifact:
    defaults = dict(title="Q3 Access Review Export", description="")
    defaults.update(overrides)
    artifact = EvidenceArtifact(**defaults)
    session.add(artifact)
    session.commit()
    return artifact


class TestObserveProgramSummary:
    def test_is_pure_and_deterministic(self, app):
        with app.state.session_factory() as session:
            summary_1 = observe_program_summary(session, app.state.settings)
            summary_2 = observe_program_summary(session, app.state.settings)
        assert summary_1 == summary_2
        assert isinstance(summary_1.total_open_items, int)


class TestGenerateObserveNarrative:
    def test_ai_disabled_produces_deterministic_fallback(self, app):
        with app.state.session_factory() as session:
            execution = generate_observe_narrative(session, app.state.settings, actor="tester@example.com")
        assert execution.status == "disabled"
        assert execution.task_type == "observe"
        assert "Stage:" in execution.output_text
        assert execution.provider_name == ""

    def test_ai_enabled_uses_provider_output(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.digital_tpm.call_chat_completion",
            lambda *a, **k: ProviderCallResult(ok=True, content="AI narrative.", error=None),
        )
        with app.state.session_factory() as session:
            _enable_usable_provider(session)
            execution = generate_observe_narrative(session, app.state.settings, actor="tester@example.com")
        assert execution.status == "ok"
        assert execution.output_text == "AI narrative."
        assert execution.provider_name == "Test Provider"

    def test_ai_error_falls_back_and_preserves_error(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.digital_tpm.call_chat_completion",
            lambda *a, **k: ProviderCallResult(ok=False, content="", error="provider exploded"),
        )
        with app.state.session_factory() as session:
            _enable_usable_provider(session)
            execution = generate_observe_narrative(session, app.state.settings, actor="tester@example.com")
        assert execution.status == "error"
        assert execution.error_message == "provider exploded"
        assert "Stage:" in execution.output_text  # still gets the deterministic fact sheet, not blank


class TestGetOrCreateReminderStateConcurrency:
    def test_survives_a_concurrent_insert_race_without_crashing_or_duplicating(self, app):
        """Regression test for a TOCTOU race: two callers (e.g. a web
        "Run nag scan" click overlapping a cron-triggered `ai-nag-scan`)
        could both see no existing AiReminderState row for the same
        reminder_key and both attempt to insert one. Simulates the race
        deterministically (no real threads needed) by having this
        session's own first lookup query trigger a *separate, already-
        committed* session's insert of the colliding row right before
        returning — mirroring tests/test_jobs.py's established
        single-threaded race-simulation pattern."""
        item = ReadinessItem(
            category="test_category", reason="test reason", link="/test/link", control_id=None, due_on=None
        )
        key = "test_category:/test/link"

        with app.state.session_factory() as session:
            original_scalar = session.scalar
            call_count = {"n": 0}

            def racy_scalar(*args, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    with app.state.session_factory() as other_session:
                        other_session.add(
                            AiReminderState(reminder_key=key, category="test_category", link="/test/link")
                        )
                        other_session.commit()
                    return None  # simulate this session's read happening before the race was won
                return original_scalar(*args, **kwargs)

            session.scalar = racy_scalar
            state = _get_or_create_reminder_state(session, item)
            assert state.reminder_key == key

        with app.state.session_factory() as verify_session:
            matches = verify_session.query(AiReminderState).filter(AiReminderState.reminder_key == key).all()
            assert len(matches) == 1  # no duplicate row from the losing insert attempt


class TestGenerateDueReminders:
    def test_disabled_ai_still_produces_deterministic_reminders(self, app):
        with app.state.session_factory() as session:
            _make_finding(session, owner_person_id=None)
            created = generate_due_reminders(session, app.state.settings, actor="tester@example.com")
        assert len(created) >= 1
        assert all(e.status == "disabled" for e in created)
        assert all(e.output_text for e in created)

    def test_cooldown_suppresses_immediate_re_nag(self, app):
        with app.state.session_factory() as session:
            _make_finding(session, owner_person_id=None)
            first = generate_due_reminders(session, app.state.settings, actor="tester@example.com")
            assert len(first) >= 1
            second = generate_due_reminders(session, app.state.settings, actor="tester@example.com")
        assert second == []  # same open item, inside the cooldown window -> nothing new

    def test_reminder_fires_again_after_cooldown_elapses(self, app):
        with app.state.session_factory() as session:
            _make_finding(session, owner_person_id=None)
            now_1 = datetime.datetime(2026, 1, 1)
            first = generate_due_reminders(session, app.state.settings, now=now_1, actor="t")
            assert len(first) >= 1
            now_2 = now_1 + REMINDER_COOLDOWN + datetime.timedelta(seconds=1)
            second = generate_due_reminders(session, app.state.settings, now=now_2, actor="t")
        assert len(second) >= 1

    def test_escalation_level_is_capped(self, app):
        with app.state.session_factory() as session:
            _make_finding(session, owner_person_id=None)
            now = datetime.datetime(2026, 1, 1)
            for _ in range(MAX_ESCALATION_LEVEL + 5):
                generate_due_reminders(session, app.state.settings, now=now, actor="t")
                now += REMINDER_COOLDOWN + datetime.timedelta(seconds=1)
            states = session.query(AiReminderState).all()
        assert states
        assert all(s.escalation_level <= MAX_ESCALATION_LEVEL for s in states)

    def test_adversarial_provider_output_is_never_executed_only_stored(self, app, monkeypatch):
        """A malicious/hallucinated completion that looks like a directive
        must still only ever land as inert text in AiTaskExecution — never
        as a mutation to any domain model."""
        adversarial = 'ACTION: close_finding(finding_id="x") -- already resolved, no action needed.'
        monkeypatch.setattr(
            "app.digital_tpm.call_chat_completion",
            lambda *a, **k: ProviderCallResult(ok=True, content=adversarial, error=None),
        )
        with app.state.session_factory() as session:
            _enable_usable_provider(session)
            finding = _make_finding(session, owner_person_id=None)
            finding_status_before = finding.status
            created = generate_due_reminders(session, app.state.settings, actor="tester@example.com")
            session.refresh(finding)
            assert finding.status == finding_status_before  # untouched despite the adversarial content
        assert any(e.output_text == adversarial for e in created)


class TestDraftPrefill:
    def test_unknown_task_type_raises_value_error(self, app):
        with app.state.session_factory() as session:
            finding = _make_finding(session)
            with pytest.raises(ValueError, match="Unknown pre-fill task_type"):
                draft_prefill(
                    session, app.state.settings, task_type="not-a-real-type", entity=finding, actor="t"
                )

    def test_evidence_description_ai_disabled_produces_fact_sheet_with_missing_markers(self, app):
        with app.state.session_factory() as session:
            artifact = _make_evidence_artifact(session, description="")
            execution = draft_prefill(
                session,
                app.state.settings,
                task_type="evidence_description",
                entity=artifact,
                actor="tester@example.com",
            )
        assert execution.status == "disabled"
        assert execution.task_type == "prefill"
        assert execution.entity_type == "evidence_artifact"
        assert "[MISSING]" in execution.output_text  # description is empty -> None -> rendered as missing

    def test_finding_response_ai_disabled_produces_fact_sheet(self, app):
        with app.state.session_factory() as session:
            finding = _make_finding(session)
            execution = draft_prefill(
                session,
                app.state.settings,
                task_type="finding_response",
                entity=finding,
                actor="tester@example.com",
            )
        assert execution.status == "disabled"
        assert "MFA not enforced" in execution.output_text

    def test_ai_enabled_uses_provider_draft_and_records_egress(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.digital_tpm.call_chat_completion",
            lambda *a, **k: ProviderCallResult(ok=True, content="Drafted response.", error=None),
        )
        with app.state.session_factory() as session:
            _enable_usable_provider(session)
            finding = _make_finding(session)
            execution = draft_prefill(
                session,
                app.state.settings,
                task_type="finding_response",
                entity=finding,
                actor="tester@example.com",
            )
            from app.models import AuditEvent

            egress_events = session.query(AuditEvent).filter(AuditEvent.entity_type.like("ai_egress:%")).all()
        assert execution.status == "ok"
        assert execution.output_text == "Drafted response."
        assert execution.included_free_text is True  # prefill always opts in to Tier 2 free text
        assert len(egress_events) == 1

    def test_control_operation_note_and_policy_change_summary_task_types_work(self, app):
        """All four representative task types (issue #15's "small
        representative set") must be dispatchable, not just the two wired
        into UI routes."""
        with app.state.session_factory() as session:
            control = InternalControl(
                name="Access Review",
                status="implemented",
                review_frequency="annual",
                cadence_type="calendar",
                cadence_interval_months=12,
            )
            session.add(control)
            session.flush()
            session.commit()
            occurrence = generate_occurrences(session, control)[0]
            session.commit()
            perform_occurrence(
                session,
                occurrence,
                occurred_at=datetime.datetime(2026, 1, 2),
                performed_by_person_id=None,
                scope_note="Reviewed access list for Q3.",
                evidence_note="Attached export.",
            )
            session.commit()
            session.refresh(occurrence)

            execution_note = draft_prefill(
                session,
                app.state.settings,
                task_type="control_operation_note",
                entity=occurrence,
                actor="t",
            )
            assert execution_note.entity_type == "control_occurrence"

            policy = Policy(title="Access Control Policy", status="draft", description="")
            session.add(policy)
            session.commit()
            execution_policy = draft_prefill(
                session, app.state.settings, task_type="policy_change_summary", entity=policy, actor="t"
            )
            assert execution_policy.entity_type == "policy"


def test_ai_task_execution_rows_never_get_read_back_into_a_material_query(app):
    """Sanity check that AiTaskExecution/AiReminderState are genuinely
    inert: nothing in app.readiness (the deterministic source of truth)
    references either table."""
    import inspect

    import app.readiness as readiness_module

    source = inspect.getsource(readiness_module)
    assert "AiTaskExecution" not in source
    assert "AiReminderState" not in source
