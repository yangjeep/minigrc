"""Headless UAT: Digital Compliance TPM (issue #15) — a CISO's real
journey: a readiness gap exists, the TPM console surfaces/prioritizes it,
a bounded nag reminder is recorded, and two representative pre-fill
drafts are generated from grounded facts, with AI left disabled
throughout (no real/fake provider dependency needed to prove the
deterministic degrade-gracefully guarantee end to end over real HTTP).
See tests/uat/harness.py and
docs/superpowers/specs/2026-08-20-issue15-digital-compliance-tpm-design.md.
"""

from __future__ import annotations

import datetime

import pytest

from app.models import AiTaskExecution, EvidenceArtifact, Finding
from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field

pytestmark = pytest.mark.uat

OPERATOR_EMAIL = "uat-tpm-operator@example.com"
READER_EMAIL = "uat-tpm-reader@example.com"


def _login(client, email):
    login_page = client.get("/login")
    csrf_token = extract_csrf_field(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": UAT_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_tpm_console_surfaces_a_real_blocker_and_bounded_nag_and_prefill(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    # 1. Create a real, concrete readiness blocker: an open finding with
    # no owner assigned (app.readiness's "finding_missing_owner" category)
    # and an evidence artifact with no description yet.
    with session_factory() as session:
        session.add(
            Finding(
                id="fnd-uat-tpm-0001",
                title="MFA not enforced for one contractor",
                description="",
                severity="high",
                status="open",
                due_date=datetime.date(2026, 3, 1),
                closure_note="",
            )
        )
        session.add(EvidenceArtifact(id="evd-uat-tpm-0001", title="Q3 Access Review Export", description=""))
        session.commit()

    # 2. AI is left unconfigured throughout this scenario. Observe/
    # Prioritize/Nag/Pre-fill must all still work and remain useful.
    console_page = client.get("/ai/tpm")
    assert console_page.status_code == 200
    # The blocker is prioritized/listed via a link to the real finding.
    assert "/findings/fnd-uat-tpm-0001" in console_page.text

    # 3. Generate the Observe program summary (bounded, explicit action).
    csrf_token = extract_csrf_field(console_page.text)
    observe_response = client.post("/ai/tpm/observe", data={"csrf_token": csrf_token}, follow_redirects=True)
    assert observe_response.status_code == 200
    assert "not been submitted anywhere" in observe_response.text  # draft disclaimer, not a mutation

    # 4. Run one bounded Nag scan; it must record a reminder for the real
    # blocker without ever touching the Finding row itself.
    console_page = client.get("/ai/tpm")
    csrf_token = extract_csrf_field(console_page.text)
    nag_response = client.post("/ai/tpm/nag-scan", data={"csrf_token": csrf_token}, follow_redirects=True)
    assert nag_response.status_code == 200
    assert "reminder(s) recorded" in nag_response.text

    with session_factory() as session:
        finding = session.get(Finding, "fnd-uat-tpm-0001")
        assert finding.status == "open"  # untouched by the nag pass
        assert finding.description == ""  # untouched
        nag_executions = session.query(AiTaskExecution).filter(AiTaskExecution.task_type == "nag").all()
        assert len(nag_executions) >= 1
        assert all(e.status == "disabled" for e in nag_executions)  # AI absent -> deterministic path

    # Running the scan again immediately must not spam a second reminder
    # for the same still-open gap (bounded/cooldown behavior).
    console_page = client.get("/ai/tpm")
    csrf_token = extract_csrf_field(console_page.text)
    client.post("/ai/tpm/nag-scan", data={"csrf_token": csrf_token}, follow_redirects=True)
    with session_factory() as session:
        nag_executions_after = (
            session.query(AiTaskExecution).filter(AiTaskExecution.task_type == "nag").count()
        )
    assert nag_executions_after == len(nag_executions)  # no new reminder within the cooldown window

    # 5. Two representative, grounded pre-fill workflows.
    evidence_detail = client.get("/evidence-artifacts/evd-uat-tpm-0001")
    csrf_token = extract_csrf_field(evidence_detail.text)
    evidence_draft_response = client.post(
        "/evidence-artifacts/evd-uat-tpm-0001/ai-draft-description",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert evidence_draft_response.status_code == 200
    assert "MISSING" in evidence_draft_response.text  # the empty description is exposed, not invented

    finding_detail = client.get("/findings/fnd-uat-tpm-0001")
    csrf_token = extract_csrf_field(finding_detail.text)
    finding_draft_response = client.post(
        "/findings/fnd-uat-tpm-0001/ai-draft-response",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert finding_draft_response.status_code == 200
    assert "MFA not enforced for one contractor" in finding_draft_response.text  # grounded in the real fact

    with session_factory() as session:
        # Both drafts persisted; the source rows remain byte-for-byte
        # unchanged — pre-fill never auto-applies anything.
        artifact = session.get(EvidenceArtifact, "evd-uat-tpm-0001")
        assert artifact.description == ""
        finding = session.get(Finding, "fnd-uat-tpm-0001")
        assert finding.status == "open"
        assert finding.closure_note == ""
        prefill_executions = (
            session.query(AiTaskExecution).filter(AiTaskExecution.task_type == "prefill").all()
        )
        assert len(prefill_executions) >= 2


def test_reader_can_view_console_but_not_trigger_actions(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=READER_EMAIL, role="reader")
    client = _login(uat_client, READER_EMAIL)

    console_page = client.get("/ai/tpm")
    assert console_page.status_code == 200

    csrf_token = extract_csrf_field(console_page.text)
    response = client.post("/ai/tpm/nag-scan", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert response.status_code == 403
